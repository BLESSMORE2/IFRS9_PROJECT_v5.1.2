import logging
import os
import sys
import threading
import time

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone


logger = logging.getLogger(__name__)

_sweeper_lock = threading.Lock()
_sweeper_started = False


def _should_start_sweeper():
    if os.environ.get("RUN_MAIN") == "false":
        return False
    # Django's development reloader imports apps in both the parent and child
    # processes. Only the child should own the background scheduler.
    if "runserver" in sys.argv and os.environ.get("RUN_MAIN") != "true":
        return False
    if os.environ.get("USERS_DISABLE_IDLE_SESSION_SWEEPER") == "1":
        return False
    if getattr(settings, "USERS_DISABLE_IDLE_SESSION_SWEEPER", False):
        return False
    return True


def _sweeper_interval_seconds():
    configured_interval = getattr(settings, "USERS_IDLE_SWEEP_INTERVAL_SECONDS", 10)
    try:
        return max(5, int(configured_interval))
    except (TypeError, ValueError):
        return 10


def run_user_security_sweep_cycle(checked_at=None):
    from .access_logs import sweep_idle_user_sessions
    from .inactivity_locks import maybe_sweep_inactive_user_accounts

    checked_at = checked_at or timezone.now()
    result = {"closed_sessions": 0, "locked_users": 0}

    try:
        result["closed_sessions"] = sweep_idle_user_sessions(ended_at=checked_at)
    except Exception:
        logger.exception("Idle-session sweep failed; inactivity checks will continue.")

    try:
        result["locked_users"] = maybe_sweep_inactive_user_accounts(checked_at=checked_at)
    except Exception:
        logger.exception("Inactive-account sweep failed; session checks will continue.")

    return result


def _close_connections_safely():
    try:
        close_old_connections()
    except Exception:
        logger.exception("Scheduler could not close stale database connections.")


def _run_idle_session_sweeper():
    # Let migrations/app startup finish before the worker opens a DB connection.
    configured_delay = getattr(settings, "USERS_IDLE_SWEEP_START_DELAY_SECONDS", 5)
    try:
        start_delay = max(0, float(configured_delay))
    except (TypeError, ValueError):
        start_delay = 5
    time.sleep(start_delay)

    while True:
        try:
            _close_connections_safely()
            result = run_user_security_sweep_cycle(checked_at=timezone.now())
            if result["closed_sessions"]:
                logger.info("Idle session sweeper closed %s session(s).", result["closed_sessions"])
            if result["locked_users"]:
                logger.info("Inactive-account sweeper locked %s user(s).", result["locked_users"])
        except Exception:
            logger.exception("Idle session sweeper failed; it will retry on the next interval.")
        finally:
            _close_connections_safely()

        time.sleep(_sweeper_interval_seconds())


def start_idle_session_sweeper():
    global _sweeper_started

    if not _should_start_sweeper():
        return

    with _sweeper_lock:
        if _sweeper_started:
            return

        sweeper_thread = threading.Thread(
            target=_run_idle_session_sweeper,
            name="users-idle-session-sweeper",
            daemon=True,
        )
        sweeper_thread.start()
        _sweeper_started = True
