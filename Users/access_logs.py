import ipaddress
from datetime import timedelta

from django.core.cache import cache
from django.contrib.sessions.models import Session
from django.db import DatabaseError
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from .models import UserAccessLog


ACCESS_LOG_SESSION_ID_KEY = "users_access_log_id"
SESSION_STARTED_AT_KEY = "users_session_started_at"
LAST_ACTIVITY_AT_KEY = "users_last_activity_at"
IDLE_SWEEP_CACHE_KEY = "users:access_logs:idle_session_sweep"


def _read_session_timestamp(value):
    if not value:
        return None

    if hasattr(value, "isoformat"):
        parsed = value
    else:
        parsed = parse_datetime(str(value))

    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


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


def _default_idle_timeout_minutes():
    try:
        from .runtime import get_system_settings

        return int(get_system_settings().idle_timeout_minutes or 0)
    except Exception:
        return 0


def _stale_log_end_time(access_log, session_expiry=None, ended_at=None, idle_timeout_minutes=None):
    ended_at = ended_at or timezone.now()
    candidate_times = []

    if session_expiry:
        candidate_times.append(session_expiry)

    idle_timeout_minutes = _default_idle_timeout_minutes() if idle_timeout_minutes is None else idle_timeout_minutes
    if idle_timeout_minutes:
        candidate_times.append(access_log.login_time + timedelta(minutes=idle_timeout_minutes))

    candidate_times.append(ended_at)
    resolved_end = min(candidate_times)
    if resolved_end < access_log.login_time:
        resolved_end = access_log.login_time
    return resolved_end


def _idle_session_end_time(access_log, session_data, session_expiry=None, ended_at=None, idle_timeout_minutes=None):
    ended_at = ended_at or timezone.now()
    idle_timeout_minutes = _default_idle_timeout_minutes() if idle_timeout_minutes is None else idle_timeout_minutes

    last_activity_at = (
        _read_session_timestamp(session_data.get(LAST_ACTIVITY_AT_KEY))
        or _read_session_timestamp(session_data.get(SESSION_STARTED_AT_KEY))
        or access_log.login_time
    )
    candidate_times = [ended_at]

    if session_expiry:
        candidate_times.append(session_expiry)

    if idle_timeout_minutes and last_activity_at:
        candidate_times.append(last_activity_at + timedelta(minutes=idle_timeout_minutes))

    resolved_end = min(candidate_times)
    if resolved_end < access_log.login_time:
        resolved_end = access_log.login_time
    return resolved_end


def sweep_idle_user_sessions(ended_at=None, idle_timeout_minutes=None):
    ended_at = ended_at or timezone.now()
    idle_timeout_minutes = _default_idle_timeout_minutes() if idle_timeout_minutes is None else int(idle_timeout_minutes or 0)
    if not idle_timeout_minutes:
        return 0

    try:
        active_logs = list(
            UserAccessLog.objects.filter(logout_time__isnull=True)
            .exclude(session_key="")
            .only("id", "session_key", "login_time", "logout_time")
            .order_by("login_time", "id")
        )
    except DatabaseError:
        return 0

    if not active_logs:
        return 0

    logs_by_session_key = {}
    for access_log in active_logs:
        logs_by_session_key.setdefault(access_log.session_key, []).append(access_log)

    try:
        sessions_by_key = {
            session.session_key: session
            for session in Session.objects.filter(session_key__in=logs_by_session_key.keys())
        }
    except DatabaseError:
        return 0

    idle_cutoff = ended_at - timedelta(minutes=idle_timeout_minutes)
    closed_count = 0

    for session_key, access_logs in logs_by_session_key.items():
        session = sessions_by_key.get(session_key)
        if session is None:
            for access_log in access_logs:
                try:
                    _close_access_log(
                        access_log,
                        UserAccessLog.END_REASON_IDLE_TIMEOUT,
                        ended_at=_stale_log_end_time(
                            access_log,
                            ended_at=ended_at,
                            idle_timeout_minutes=idle_timeout_minutes,
                        ),
                    )
                    closed_count += 1
                except DatabaseError:
                    continue
            continue

        try:
            session_data = session.get_decoded()
        except Exception:
            session_data = {}

        last_activity_at = (
            _read_session_timestamp(session_data.get(LAST_ACTIVITY_AT_KEY))
            or _read_session_timestamp(session_data.get(SESSION_STARTED_AT_KEY))
            or min(access_log.login_time for access_log in access_logs)
        )
        session_expired = bool(session.expire_date and session.expire_date <= ended_at)
        idle_expired = bool(last_activity_at and last_activity_at <= idle_cutoff)

        if not session_expired and not idle_expired:
            continue

        for access_log in access_logs:
            try:
                _close_access_log(
                    access_log,
                    UserAccessLog.END_REASON_IDLE_TIMEOUT,
                    ended_at=_idle_session_end_time(
                        access_log,
                        session_data,
                        session_expiry=session.expire_date,
                        ended_at=ended_at,
                        idle_timeout_minutes=idle_timeout_minutes,
                    ),
                )
                closed_count += 1
            except DatabaseError:
                continue

        try:
            session.delete()
        except DatabaseError:
            continue

    return closed_count


def maybe_sweep_idle_user_sessions(ended_at=None):
    idle_timeout_minutes = _default_idle_timeout_minutes()
    if not idle_timeout_minutes:
        return 0

    throttle_seconds = max(5, min(30, int((idle_timeout_minutes * 60) / 4) or 5))
    if not cache.add(IDLE_SWEEP_CACHE_KEY, True, throttle_seconds):
        return 0

    return sweep_idle_user_sessions(ended_at=ended_at, idle_timeout_minutes=idle_timeout_minutes)


def get_live_access_log_session_keys(ended_at=None):
    ended_at = ended_at or timezone.now()
    sweep_idle_user_sessions(ended_at=ended_at)
    try:
        open_session_keys = list(
            UserAccessLog.objects.filter(logout_time__isnull=True)
            .exclude(session_key="")
            .values_list("session_key", flat=True)
        )
    except DatabaseError:
        return set()

    if not open_session_keys:
        return set()

    try:
        return set(
            Session.objects.filter(
                session_key__in=open_session_keys,
                expire_date__gt=ended_at,
            ).values_list("session_key", flat=True)
        )
    except DatabaseError:
        return set()


def get_live_access_log_user_ids(ended_at=None):
    live_session_keys = get_live_access_log_session_keys(ended_at=ended_at)
    if not live_session_keys:
        return set()

    try:
        return set(
            UserAccessLog.objects.filter(
                logout_time__isnull=True,
                session_key__in=live_session_keys,
            ).values_list("user_id", flat=True)
        )
    except DatabaseError:
        return set()


def reconcile_stale_user_session_logs(user=None, ended_at=None):
    ended_at = ended_at or timezone.now()
    try:
        active_log_qs = UserAccessLog.objects.filter(logout_time__isnull=True)
        if user is not None:
            active_log_qs = active_log_qs.filter(user=user)
        active_logs = list(active_log_qs.only("id", "session_key", "login_time", "logout_time").order_by("login_time", "id"))
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
        session_expiry_by_key = dict(
            Session.objects.filter(session_key__in=session_keys).values_list("session_key", "expire_date")
        )
    except DatabaseError:
        return 0

    live_session_keys = {
        session_key
        for session_key, expire_date in session_expiry_by_key.items()
        if expire_date and expire_date > ended_at
    }
    idle_timeout_minutes = _default_idle_timeout_minutes()
    closed_count = 0
    for access_log in active_logs:
        if access_log.session_key and access_log.session_key in live_session_keys:
            continue
        try:
            _close_access_log(
                access_log,
                UserAccessLog.END_REASON_IDLE_TIMEOUT,
                ended_at=_stale_log_end_time(
                    access_log,
                    session_expiry=session_expiry_by_key.get(access_log.session_key),
                    ended_at=ended_at,
                    idle_timeout_minutes=idle_timeout_minutes,
                ),
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
