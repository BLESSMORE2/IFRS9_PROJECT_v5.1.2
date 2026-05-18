import ipaddress

from django.contrib.sessions.models import Session
from django.db import DatabaseError
from django.utils import timezone

from .models import UserAccessLog


ACCESS_LOG_SESSION_ID_KEY = "users_access_log_id"
SESSION_STARTED_AT_KEY = "users_session_started_at"
LAST_ACTIVITY_AT_KEY = "users_last_activity_at"


def _normalize_ip_candidate(raw_value):
    candidate = (raw_value or "").strip()
    if not candidate or candidate.lower() == "unknown":
        return None

    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _get_client_ip(request):
    header_order = (
        "HTTP_X_FORWARDED_FOR",
        "HTTP_X_REAL_IP",
        "HTTP_X_CLIENT_IP",
        "HTTP_CF_CONNECTING_IP",
        "HTTP_TRUE_CLIENT_IP",
        "REMOTE_ADDR",
    )
    fallback_ip = None

    for header_name in header_order:
        raw_value = request.META.get(header_name) or ""
        for ip_value in raw_value.split(","):
            normalized_ip = _normalize_ip_candidate(ip_value)
            if not normalized_ip:
                continue

            parsed_ip = ipaddress.ip_address(normalized_ip)
            if not parsed_ip.is_loopback:
                return normalized_ip
            if fallback_ip is None:
                fallback_ip = normalized_ip

    return fallback_ip


def _close_access_log(access_log, end_reason, ended_at=None):
    if access_log is None or access_log.logout_time:
        return access_log

    ended_at = ended_at or timezone.now()
    if ended_at < access_log.login_time:
        ended_at = access_log.login_time

    access_log.logout_time = ended_at
    access_log.end_reason = end_reason
    access_log.session_duration_seconds = max(
        int((ended_at - access_log.login_time).total_seconds()),
        0,
    )
    access_log.save(
        update_fields=[
            "logout_time",
            "end_reason",
            "session_duration_seconds",
        ]
    )
    return access_log


def reconcile_stale_user_session_logs(user, ended_at=None):
    ended_at = ended_at or timezone.now()
    try:
        active_logs = list(
            UserAccessLog.objects.filter(user=user, logout_time__isnull=True)
            .only("id", "session_key", "login_time", "logout_time")
            .order_by("login_time", "id")
        )
    except DatabaseError:
        return 0

    if not active_logs:
        return 0

    session_keys = {
        access_log.session_key
        for access_log in active_logs
        if access_log.session_key
    }
    try:
        live_session_keys = set(
            Session.objects.filter(
                session_key__in=session_keys,
                expire_date__gt=ended_at,
            ).values_list("session_key", flat=True)
        )
    except DatabaseError:
        return 0

    closed_count = 0
    for access_log in active_logs:
        if access_log.session_key and access_log.session_key in live_session_keys:
            continue
        try:
            _close_access_log(
                access_log,
                UserAccessLog.END_REASON_IDLE_TIMEOUT,
                ended_at=ended_at,
            )
        except DatabaseError:
            continue
        closed_count += 1

    return closed_count


def begin_user_session_log(request, user):
    if not request.session.session_key:
        request.session.save()

    session_key = request.session.session_key or ""
    now = timezone.now()
    request.session[SESSION_STARTED_AT_KEY] = now.isoformat()
    request.session[LAST_ACTIVITY_AT_KEY] = now.isoformat()
    reconcile_stale_user_session_logs(user, ended_at=now)
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

    try:
        return _close_access_log(access_log, end_reason, ended_at=timezone.now())
    except DatabaseError:
        request.session.pop(ACCESS_LOG_SESSION_ID_KEY, None)
        return None
