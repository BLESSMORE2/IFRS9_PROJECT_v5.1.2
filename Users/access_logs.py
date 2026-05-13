from django.db import DatabaseError
from django.utils import timezone

from .models import UserAccessLog


ACCESS_LOG_SESSION_ID_KEY = "users_access_log_id"
SESSION_STARTED_AT_KEY = "users_session_started_at"
LAST_ACTIVITY_AT_KEY = "users_last_activity_at"


def _get_client_ip(request):
    forwarded_for = (request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return (request.META.get("REMOTE_ADDR") or "").strip() or None


def begin_user_session_log(request, user):
    if not request.session.session_key:
        request.session.save()

    session_key = request.session.session_key or ""
    now = timezone.now()
    request.session[SESSION_STARTED_AT_KEY] = now.isoformat()
    request.session[LAST_ACTIVITY_AT_KEY] = now.isoformat()
    try:
        access_log = UserAccessLog.objects.create(
            user=user,
            session_key=session_key,
            login_time=now,
            ip_address=_get_client_ip(request),
            user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:255],
        )
    except DatabaseError:
        request.session.pop(ACCESS_LOG_SESSION_ID_KEY, None)
        return None

    request.session[ACCESS_LOG_SESSION_ID_KEY] = access_log.pk
    return access_log


def close_user_session_log(request, end_reason):
    access_log_id = request.session.get(ACCESS_LOG_SESSION_ID_KEY)
    if not access_log_id:
        return None

    try:
        access_log = (
            UserAccessLog.objects.filter(pk=access_log_id)
            .select_related("user")
            .first()
        )
    except DatabaseError:
        request.session.pop(ACCESS_LOG_SESSION_ID_KEY, None)
        return None

    if access_log is None or access_log.logout_time:
        return access_log

    ended_at = timezone.now()
    access_log.logout_time = ended_at
    access_log.end_reason = end_reason
    access_log.session_duration_seconds = max(
        int((ended_at - access_log.login_time).total_seconds()),
        0,
    )
    try:
        access_log.save(
            update_fields=[
                "logout_time",
                "end_reason",
                "session_duration_seconds",
            ]
        )
    except DatabaseError:
        request.session.pop(ACCESS_LOG_SESSION_ID_KEY, None)
        return None
    return access_log
