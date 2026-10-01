import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.utils import timezone

from .access_logs import _close_access_log
from .models import AuditTrail, CustomUser, UserAccessLog
from .runtime import get_system_settings


logger = logging.getLogger(__name__)

INACTIVITY_SWEEP_CACHE_KEY = "users:security:inactivity-lock-sweep"


def _policy_days(runtime_settings) -> int:
    if not bool(getattr(runtime_settings, "enable_inactivity_lock", False)):
        return 0
    try:
        return max(int(getattr(runtime_settings, "inactivity_lock_days", 0) or 0), 0)
    except (TypeError, ValueError):
        return 0


def inactivity_reference_at(user):
    """Return the latest successful-login or administrator-reset baseline."""
    login_or_creation = getattr(user, "last_login", None) or getattr(user, "date_joined", None)
    candidates = [login_or_creation, getattr(user, "inactivity_lock_reset_at", None)]
    return max(value for value in candidates if value is not None)


def _close_locked_user_sessions(user_ids, locked_at) -> None:
    if not user_ids:
        return

    active_logs = list(
        UserAccessLog.objects.filter(
            user_id__in=user_ids,
            logout_time__isnull=True,
        ).exclude(session_key="")
    )
    session_keys = {row.session_key for row in active_logs if row.session_key}
    if session_keys:
        Session.objects.filter(session_key__in=session_keys).delete()

    for access_log in active_logs:
        _close_access_log(
            access_log,
            UserAccessLog.END_REASON_INACTIVITY_LOCK,
            ended_at=locked_at,
        )


def sweep_inactive_user_accounts(checked_at=None, runtime_settings=None) -> int:
    """Lock regular active users whose inactivity reference reached the policy cutoff."""
    checked_at = checked_at or timezone.now()
    runtime_settings = runtime_settings or get_system_settings()
    inactivity_days = _policy_days(runtime_settings)
    if not inactivity_days:
        return 0

    cutoff = checked_at - timedelta(days=inactivity_days)
    candidates = list(
        CustomUser.objects.filter(
            is_active=True,
            is_superuser=False,
            permanently_locked=False,
            inactivity_locked_at__isnull=True,
        )
        .filter(Q(lockout_until__isnull=True) | Q(lockout_until__lte=checked_at))
        .filter(
            Q(last_login__lte=cutoff)
            | Q(last_login__isnull=True, date_joined__lte=cutoff)
        )
        .only(
            "id",
            "email",
            "last_login",
            "date_joined",
            "inactivity_lock_reset_at",
        )
        .order_by("id")
    )
    eligible = [user for user in candidates if inactivity_reference_at(user) <= cutoff]
    if not eligible:
        return 0

    eligible_ids = [user.pk for user in eligible]
    with transaction.atomic():
        locked_ids = list(
            CustomUser.objects.select_for_update().filter(
                pk__in=eligible_ids,
                inactivity_locked_at__isnull=True,
            ).values_list("pk", flat=True)
        )
        if not locked_ids:
            return 0

        CustomUser.objects.filter(
            pk__in=locked_ids,
            inactivity_locked_at__isnull=True,
        ).update(inactivity_locked_at=checked_at)
        references = {
            user.pk: inactivity_reference_at(user)
            for user in eligible
            if user.pk in locked_ids
        }
        emails = {
            user.pk: user.email
            for user in eligible
            if user.pk in locked_ids
        }
        AuditTrail.objects.bulk_create(
            [
                AuditTrail(
                    user=None,
                    model_name="CustomUser",
                    action="update",
                    object_id=str(user_id),
                    change_description=(
                        f"Automatically locked inactive account {emails[user_id]}. "
                        f"Last activity reference: {references[user_id].isoformat()}. "
                        f"Policy threshold: {inactivity_days} day(s)."
                    ),
                )
                for user_id in locked_ids
            ],
            batch_size=500,
        )

    _close_locked_user_sessions(locked_ids, checked_at)
    return len(locked_ids)


def maybe_sweep_inactive_user_accounts(checked_at=None) -> int:
    runtime_settings = get_system_settings()
    if not _policy_days(runtime_settings):
        return 0

    interval_seconds = getattr(settings, "USERS_INACTIVITY_SWEEP_INTERVAL_SECONDS", 300)
    try:
        interval_seconds = max(60, int(interval_seconds))
    except (TypeError, ValueError):
        interval_seconds = 300

    try:
        if not cache.add(INACTIVITY_SWEEP_CACHE_KEY, True, interval_seconds):
            return 0
    except Exception:
        logger.warning("Inactivity sweep cache throttle was unavailable; continuing without it.")

    try:
        return sweep_inactive_user_accounts(
            checked_at=checked_at,
            runtime_settings=runtime_settings,
        )
    except DatabaseError:
        logger.exception("Inactive-account sweep could not access the database.")
        return 0
