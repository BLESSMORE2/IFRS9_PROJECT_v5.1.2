from __future__ import annotations

import os
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import DatabaseError, OperationalError, ProgrammingError, connection
from django.utils import timezone

from scorecard.functions_view.api import (
    _format_duration,
    _upsert_scheduler_service_status,
    run_due_import_schedules,
    run_due_main_customer_syncs,
)
from scorecard.functions_view.email import send_due_without_score_summary_emails
from scorecard.functions_view.historical_scores import run_due_historical_score_capture
from scorecard.functions_view.score_auto_refresh import run_due_autofilled_score_refresh
from scorecard.models import ApiSchedulerServiceLog, ApiSchedulerServiceStatus


SchedulerLogWriter = Callable[[str, str], None]

_scheduler_thread: threading.Thread | None = None
_scheduler_lock = threading.Lock()
_scheduler_stop_event: threading.Event | None = None
_scheduler_process_lock: "_SchedulerProcessLock | None" = None
_SCHEDULER_REQUIRED_COLUMNS = {
    "scheduler_enabled",
    "run_import_schedules",
    "run_main_customer_sync",
    "run_historical_score_capture",
    "retry_limit",
    "retry_delay_seconds",
}

AUTO_REFRESH_STATUS_EVENT_CODE = "auto_score_refresh_status"


class _SchedulerProcessLock:
    """Keep only one in-process scheduler active across web worker processes."""

    def __init__(self) -> None:
        base_dir = str(Path(settings.BASE_DIR).resolve()).lower()
        lock_name = f"scorecard-scheduler-{abs(hash(base_dir))}.lock"
        self.path = Path(tempfile.gettempdir()) / lock_name
        self.handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        self.handle.write(b"0")
        self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            self.handle.close()
            self.handle = None
            return False
        return True

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def _emit(writer: SchedulerLogWriter | None, level: str, message: str) -> None:
    if writer is not None:
        writer(level, message)


def _json_safe_details(details: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize scheduler diagnostics before writing them to a JSONField."""
    if not details:
        return None
    return json.loads(json.dumps(details, cls=DjangoJSONEncoder))


def _scheduler_schema_ready() -> bool:
    """Do not start scheduler work until its database migration is available."""
    try:
        with connection.cursor() as cursor:
            columns = {
                column.name
                for column in connection.introspection.get_table_description(
                    cursor,
                    ApiSchedulerServiceStatus._meta.db_table,
                )
            }
    except DatabaseError:
        return False
    return _SCHEDULER_REQUIRED_COLUMNS.issubset(columns)


def _persist_scheduler_log(*, level: str, message: str, event_code: str = "", details: dict[str, Any] | None = None) -> None:
    safe_details = _json_safe_details(details)
    try:
        if event_code == "service_started":
            startup_logs = ApiSchedulerServiceLog.objects.filter(
                service_name="django_api_scheduler_service",
                event_code="service_started",
            ).order_by("-created_at", "-id")
            current_startup = startup_logs.first()
            if current_startup is not None:
                current_startup.level = level
                current_startup.message = message
                current_startup.details = safe_details
                current_startup.created_at = timezone.now()
                current_startup.save(
                    update_fields=["level", "message", "details", "created_at"]
                )
                startup_logs.exclude(pk=current_startup.pk).delete()
                return

        ApiSchedulerServiceLog.objects.create(
            service_name="django_api_scheduler_service",
            level=level,
            event_code=event_code,
            message=message,
            details=safe_details,
        )
    except (DatabaseError, TypeError, ValueError):
        # The scheduler may start before migrations create the log table.
        # Logging must never interrupt the actual scheduler workload.
        return


def _auto_refresh_display_time(value: Any) -> str:
    if value is None:
        return "-"
    if hasattr(value, "strftime"):
        return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _auto_refresh_detail_key(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _auto_refresh_status_message(result: dict[str, Any], now) -> tuple[str, str]:
    reason = result.get("reason") or "not_due"
    frequency = result.get("frequency") or "-"
    scheduled_time = result.get("scheduled_time") or "-"
    checked_at = _auto_refresh_display_time(result.get("checked_at") or now)
    last_run_at = _auto_refresh_display_time(result.get("last_run_at"))

    if reason == "disabled":
        return "info", "Auto score refresh is disabled in Workflow Rules."
    if reason == "not_due_time":
        return (
            "info",
            f"Auto score refresh waiting for scheduled time {scheduled_time}. "
            f"Frequency={frequency}; checked_at={checked_at}; last_run={last_run_at}.",
        )
    if reason == "not_due_weekday":
        return (
            "info",
            f"Auto score refresh not due today because the weekly weekday does not match. "
            f"Frequency={frequency}; scheduled_time={scheduled_time}; checked_at={checked_at}; last_run={last_run_at}.",
        )
    if reason == "not_due_month_day":
        return (
            "info",
            f"Auto score refresh not due today because the monthly day does not match. "
            f"Frequency={frequency}; scheduled_time={scheduled_time}; checked_at={checked_at}; last_run={last_run_at}.",
        )
    if reason in {"already_ran_today", "already_ran_this_week", "already_ran_this_month"}:
        return (
            "info",
            f"Auto score refresh already ran for this schedule window. "
            f"Reason={reason}; frequency={frequency}; scheduled_time={scheduled_time}; last_run={last_run_at}.",
        )
    if reason == "no_engine":
        return (
            "warning",
            "Auto score refresh is enabled but no auto-refresh engine was found.",
        )
    return (
        "info",
        f"Auto score refresh did not run. Reason={reason}; frequency={frequency}; "
        f"scheduled_time={scheduled_time}; checked_at={checked_at}; last_run={last_run_at}.",
    )


def _auto_refresh_status_recently_logged(result: dict[str, Any], now) -> bool:
    reason = str(result.get("reason") or "")
    scheduled_for = _auto_refresh_detail_key(result.get("scheduled_for"))
    local_today = timezone.localtime(now).date()
    try:
        recent_logs = ApiSchedulerServiceLog.objects.filter(
            service_name="django_api_scheduler_service",
            event_code=AUTO_REFRESH_STATUS_EVENT_CODE,
        ).order_by("-created_at", "-id")[:25]
        for log in recent_logs:
            if not log.created_at or timezone.localtime(log.created_at).date() != local_today:
                continue
            details = log.details if isinstance(log.details, dict) else {}
            if details.get("reason") == reason and _auto_refresh_detail_key(details.get("scheduled_for")) == scheduled_for:
                return True
    except DatabaseError:
        return False
    return False


def _persist_auto_refresh_status_log(result: dict[str, Any], now, writer: SchedulerLogWriter | None = None) -> None:
    if result.get("performed") or result.get("reason") == "error":
        return
    if result.get("reason") in {"already_ran_today", "already_ran_this_week", "already_ran_this_month"}:
        return
    if _auto_refresh_status_recently_logged(result, now):
        return
    level, message = _auto_refresh_status_message(result, now)
    _emit(writer, level, message)
    _persist_scheduler_log(
        level=level,
        event_code=AUTO_REFRESH_STATUS_EVENT_CODE,
        message=message,
        details=result,
    )


def _scheduler_runtime_settings(default_interval: int = 30) -> dict[str, Any]:
    defaults = {
        "scheduler_enabled": True,
        "run_import_schedules": True,
        "run_main_customer_sync": True,
        "run_historical_score_capture": True,
        "check_interval_seconds": max(10, default_interval),
        "retry_limit": 3,
        "retry_delay_seconds": 30,
    }
    try:
        status = ApiSchedulerServiceStatus.objects.filter(
            service_name="django_api_scheduler_service"
        ).only(
            "scheduler_enabled",
            "run_import_schedules",
            "run_main_customer_sync",
            "run_historical_score_capture",
            "check_interval_seconds",
            "retry_limit",
            "retry_delay_seconds",
        ).first()
    except (ProgrammingError, OperationalError):
        return defaults
    if status is None:
        return defaults
    return {
        "scheduler_enabled": status.scheduler_enabled,
        "run_import_schedules": status.run_import_schedules,
        "run_main_customer_sync": status.run_main_customer_sync,
        "run_historical_score_capture": status.run_historical_score_capture,
        "check_interval_seconds": min(max(status.check_interval_seconds, 10), 3600),
        "retry_limit": min(status.retry_limit, 10),
        "retry_delay_seconds": min(max(status.retry_delay_seconds, 5), 3600),
    }


def run_scheduler_cycle(
    *,
    interval: int,
    writer: SchedulerLogWriter | None = None,
    run_imports: bool = True,
    run_main_sync: bool = True,
    run_historical_capture: bool = True,
    run_auto_refresh: bool = True,
) -> dict[str, Any]:
    now = timezone.now()
    results = run_due_import_schedules(now=now) if run_imports else []
    sync_results = run_due_main_customer_syncs() if run_main_sync else []
    historical_result = (
        run_due_historical_score_capture(now=now)
        if run_historical_capture
        else {"performed": False, "reason": "disabled"}
    )
    auto_refresh_result: dict[str, Any]
    try:
        auto_refresh_result = (
            run_due_autofilled_score_refresh(now=now)
            if run_auto_refresh
            else {"performed": False, "reason": "disabled"}
        )
    except Exception as exc:
        auto_refresh_result = {
            "performed": False,
            "reason": "error",
            "error": str(exc) or exc.__class__.__name__,
        }
        connection.close_if_unusable_or_obsolete()
        _emit(writer, "error", f"Auto score refresh failed: {auto_refresh_result['error']}")
        _persist_scheduler_log(
            level="error",
            event_code="auto_score_refresh_failed",
            message=f"Auto score refresh failed: {auto_refresh_result['error']}",
            details=auto_refresh_result,
        )
    without_score_email_result: dict[str, Any]
    try:
        without_score_email_result = send_due_without_score_summary_emails(now=now)
    except Exception as exc:
        without_score_email_result = {
            "performed": False,
            "reason": "error",
            "error": str(exc) or exc.__class__.__name__,
        }
        _emit(writer, "error", f"Without-score summary email failed: {without_score_email_result['error']}")
        _persist_scheduler_log(
            level="error",
            event_code="without_score_email_failed",
            message=f"Without-score summary email failed: {without_score_email_result['error']}",
            details=without_score_email_result,
        )

    if results:
        _persist_scheduler_log(
            level="warning",
            event_code="due_schedules_found",
            message=f"Found {len(results)} due schedule(s) at {timezone.localtime(now).strftime('%Y-%m-%d %H:%M:%S')}.",
            details={"due_schedule_count": len(results)},
        )
        _emit(
            writer,
            "warning",
            f"Found {len(results)} due schedule(s) at {timezone.localtime(now).strftime('%Y-%m-%d %H:%M:%S')}.",
        )
        for result in results:
            schedule = result["schedule"]
            if result["status"] == "success":
                stats = result["stats"]
                _emit(
                    writer,
                    "success",
                    f"{schedule.name}: fetched={stats.fetched}, created={stats.created}, "
                    f"updated={stats.updated}, unchanged={stats.unchanged}, "
                    f"skipped={stats.skipped}, duration={_format_duration(result['duration_seconds'])}",
                )
                _persist_scheduler_log(
                    level="success",
                    event_code="schedule_import_success",
                    message=f"{schedule.name}: fetched={stats.fetched}, created={stats.created}, updated={stats.updated}, unchanged={stats.unchanged}, skipped={stats.skipped}.",
                    details={
                        "schedule_id": schedule.id,
                        "schedule_name": schedule.name,
                        "fetched": stats.fetched,
                        "created": stats.created,
                        "updated": stats.updated,
                        "unchanged": stats.unchanged,
                        "skipped": stats.skipped,
                        "duration_seconds": result["duration_seconds"],
                    },
                )
            else:
                _emit(writer, "error", f"{schedule.name}: {result['error']}")
                _persist_scheduler_log(
                    level="error",
                    event_code="schedule_import_failed",
                    message=f"{schedule.name}: {result['error']}",
                    details={
                        "schedule_id": schedule.id,
                        "schedule_name": schedule.name,
                    },
                )

    if sync_results:
        for result in sync_results:
            _emit(
                writer,
                "success",
                f"Main customer sync {result['reporting_date']}: {result['detail']}",
            )
            _persist_scheduler_log(
                level="success",
                event_code="main_sync_success",
                message=f"Main customer sync {result['reporting_date']}: {result['detail']}",
                details={
                    "reporting_date": str(result["reporting_date"]),
                    "status": result.get("status"),
                },
            )

    if historical_result.get("performed"):
        captured_dates = historical_result.get("captured_reporting_dates") or [
            historical_result["reporting_date"]
        ]
        _emit(
            writer,
            "success",
            f"Historical scores {', '.join(str(value) for value in captured_dates)}: "
            f"created={historical_result.get('created', 0)}, updated={historical_result.get('updated', 0)}, "
            f"both={historical_result.get('both', 0)}, basel_only={historical_result.get('basel_only', 0)}, "
            f"ifrs9_only={historical_result.get('ifrs9_only', 0)}, "
            f"snapshot_as_of={historical_result.get('snapshot_as_of', '-')}",
        )
        _persist_scheduler_log(
            level="success",
            event_code="historical_scores_captured",
            message=(
                f"Historical scores captured for {', '.join(str(value) for value in captured_dates)}. "
                f"Created={historical_result.get('created', 0)}, Updated={historical_result.get('updated', 0)}."
            ),
            details=historical_result,
        )

    if auto_refresh_result.get("performed"):
        notification = auto_refresh_result.get("notification") or {}
        _emit(
            writer,
            "success",
            f"Auto score refresh: updated={auto_refresh_result.get('updated', 0)}, "
            f"checked={auto_refresh_result.get('checked_count', 0)}, "
            f"skipped={auto_refresh_result.get('skipped_count', 0)}, "
            f"adopted={auto_refresh_result.get('adopted_count', 0)}, "
            f"errors={auto_refresh_result.get('error_count', 0)}, "
            f"document={notification.get('document_name', '-')}, "
            f"notifications={notification.get('notifications_sent', 0)}.",
        )
        _persist_scheduler_log(
            level="success",
            event_code="auto_score_refresh_completed",
            message=(
                f"Auto score refresh completed. Checked={auto_refresh_result.get('checked_count', 0)}, "
                f"updated={auto_refresh_result.get('updated', 0)}, "
                f"skipped={auto_refresh_result.get('skipped_count', 0)}, "
                f"adopted={auto_refresh_result.get('adopted_count', 0)}, "
                f"errors={auto_refresh_result.get('error_count', 0)}."
            ),
            details=auto_refresh_result,
        )
    else:
        _persist_auto_refresh_status_log(auto_refresh_result, now, writer)

    if without_score_email_result.get("performed"):
        _emit(
            writer,
            "success",
            f"Without-score summary emails: sent={without_score_email_result.get('sent', 0)}, "
            f"branches={without_score_email_result.get('branches', 0)}, "
            f"reason={without_score_email_result.get('reason', '-')}.",
        )
        _persist_scheduler_log(
            level="success",
            event_code="without_score_email_sent",
            message=(
                f"Without-score summary emails sent={without_score_email_result.get('sent', 0)}, "
                f"branches={without_score_email_result.get('branches', 0)}."
            ),
            details=without_score_email_result,
        )

    if (
        not results
        and not sync_results
        and not historical_result.get("performed")
        and not auto_refresh_result.get("performed")
        and not without_score_email_result.get("performed")
    ):
        _emit(
            writer,
            "info",
            f"[{timezone.localtime(now).strftime('%H:%M:%S')}] "
            "No due API schedules, main customer syncs, historical score captures, score auto-refreshes, or without-score emails.",
        )

    _upsert_scheduler_service_status(
        last_status="running",
        last_message=(
            f"Last scheduler check completed at "
            f"{timezone.localtime(now).strftime('%Y-%m-%d %H:%M:%S')}. "
            f"Import schedules: {len(results)}. Main syncs: {len(sync_results)}. "
            f"Historical capture: {'done' if historical_result.get('performed') else historical_result.get('reason', 'not_due')}. "
            f"Auto-refresh: {'done' if auto_refresh_result.get('performed') else auto_refresh_result.get('reason', 'not_due')}. "
            f"Without-score email: {'done' if without_score_email_result.get('performed') else without_score_email_result.get('reason', 'not_due')}."
        ),
        last_run_count=(
            len(results)
            + len(sync_results)
            + (1 if historical_result.get("performed") else 0)
            + (1 if auto_refresh_result.get("performed") else 0)
            + (1 if without_score_email_result.get("performed") else 0)
        ),
        check_interval_seconds=interval,
    )

    return {
        "results": results,
        "sync_results": sync_results,
        "historical_result": historical_result,
        "auto_refresh_result": auto_refresh_result,
        "without_score_email_result": without_score_email_result,
    }


def run_scheduler_loop(
    *,
    interval: int,
    run_once: bool = False,
    writer: SchedulerLogWriter | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    interval = max(5, interval)
    _emit(
        writer,
        "info",
        f"Scheduler worker started. Check interval: {interval} seconds.",
    )
    _persist_scheduler_log(
        level="info",
        event_code="service_started",
        message=f"Scheduler worker started. Check interval: {interval} seconds.",
        details={"interval": interval},
    )
    _upsert_scheduler_service_status(
        last_status="running",
        last_message="Scheduler worker is running in this Django process and is waiting for due schedules.",
        check_interval_seconds=interval,
        set_started=True,
    )

    try:
        consecutive_failures = 0
        while True:
            runtime_settings = _scheduler_runtime_settings(interval)
            interval = runtime_settings["check_interval_seconds"]
            if not runtime_settings["scheduler_enabled"]:
                _upsert_scheduler_service_status(
                    last_status="paused",
                    last_message=(
                        "Scheduler automation is paused from the Scheduler Control Centre. "
                        "The watchdog remains available and will resume automatically when enabled."
                    ),
                    check_interval_seconds=interval,
                )
                if run_once:
                    return
                if stop_event is not None:
                    if stop_event.wait(interval):
                        break
                else:
                    time.sleep(interval)
                continue

            try:
                run_scheduler_cycle(
                    interval=interval,
                    writer=writer,
                    run_imports=runtime_settings["run_import_schedules"],
                    run_main_sync=runtime_settings["run_main_customer_sync"],
                    run_historical_capture=runtime_settings["run_historical_score_capture"],
                )
                consecutive_failures = 0
            except Exception as exc:
                consecutive_failures += 1
                retry_limit = runtime_settings["retry_limit"]
                retry_delay = min(
                    runtime_settings["retry_delay_seconds"] * consecutive_failures,
                    3600,
                )
                if retry_limit and consecutive_failures > retry_limit:
                    retry_delay = max(retry_delay, interval * 5)
                message = (
                    f"Scheduler cycle failed: {exc}. It will retry in "
                    f"{retry_delay} seconds."
                )
                _emit(writer, "error", message)
                _persist_scheduler_log(
                    level="error",
                    event_code="scheduler_cycle_failed",
                    message=message,
                    details={
                        "interval": interval,
                        "consecutive_failures": consecutive_failures,
                        "retry_limit": retry_limit,
                        "retry_delay_seconds": retry_delay,
                    },
                )
                try:
                    _upsert_scheduler_service_status(
                        last_status="degraded",
                        last_message=message,
                        check_interval_seconds=interval,
                    )
                except Exception:
                    # A database outage may also prevent status persistence.
                    pass
                if run_once:
                    raise
                if stop_event is not None:
                    if stop_event.wait(retry_delay):
                        break
                else:
                    time.sleep(retry_delay)
                continue
            if run_once:
                return
            if stop_event is not None:
                if stop_event.wait(interval):
                    break
            else:
                time.sleep(interval)
    except KeyboardInterrupt:
        _persist_scheduler_log(
            level="warning",
            event_code="service_stopped",
            message="Scheduler service stopped manually.",
            details={"interval": interval},
        )
        _upsert_scheduler_service_status(
            last_status="stopped",
            last_message="Scheduler service stopped manually.",
            check_interval_seconds=interval,
        )
        _emit(writer, "info", "Scheduler worker stopped in this Django process.")
    except Exception as exc:
        _persist_scheduler_log(
            level="error",
            event_code="service_failed",
            message=f"Scheduler service failed: {exc}",
            details={"interval": interval},
        )
        _upsert_scheduler_service_status(
            last_status="failed",
            last_message=f"Scheduler service failed: {exc}",
            check_interval_seconds=interval,
        )
        raise


def _should_autostart_scheduler() -> bool:
    if os.environ.get("SCORECARD_AUTOSTART_SCHEDULER", "1") != "1":
        return False
    argv_command = sys.argv[1] if len(sys.argv) > 1 else ""
    if argv_command == "runserver":
        return os.environ.get("RUN_MAIN") == "true"
    if os.environ.get("SCORECARD_WEB_PROCESS") == "1":
        return True
    return os.environ.get("SCORECARD_FORCE_AUTOSTART_SCHEDULER") == "1"


def autostart_scheduler_if_needed(*, interval: int = 30) -> None:
    if not _should_autostart_scheduler():
        return
    ensure_scheduler_running(interval=interval)


def ensure_scheduler_running(*, interval: int = 30) -> tuple[bool, str]:
    global _scheduler_thread, _scheduler_stop_event, _scheduler_process_lock

    with _scheduler_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return False, "Scheduler service is already running."

        process_lock = _SchedulerProcessLock()
        if not process_lock.acquire():
            return False, "Scheduler service is already running in another Django worker."

        _scheduler_process_lock = process_lock
        _scheduler_stop_event = threading.Event()

        def _runner() -> None:
            try:
                while not _scheduler_stop_event.is_set():
                    if not _scheduler_schema_ready():
                        # Deployments can briefly start Django before migrations finish.
                        # Wait without touching scheduler models or creating restart logs.
                        if _scheduler_stop_event.wait(30):
                            break
                        continue
                    try:
                        run_scheduler_loop(
                            interval=interval,
                            writer=None,
                            stop_event=_scheduler_stop_event,
                        )
                    except Exception as exc:
                        if _scheduler_stop_event.is_set():
                            break
                        restart_delay = max(5, min(interval, 30))
                        message = (
                            f"Scheduler worker stopped unexpectedly: {exc}. "
                            f"The watchdog will restart it in {restart_delay} seconds."
                        )
                        _persist_scheduler_log(
                            level="error",
                            event_code="scheduler_watchdog_restart",
                            message=message,
                            details={
                                "interval": interval,
                                "restart_delay_seconds": restart_delay,
                            },
                        )
                        try:
                            _upsert_scheduler_service_status(
                                last_status="restarting",
                                last_message=message,
                                check_interval_seconds=interval,
                            )
                        except Exception:
                            pass
                        if _scheduler_stop_event.wait(restart_delay):
                            break
                        continue

                    # A normal return is only expected when the stop event is set.
                    if not _scheduler_stop_event.is_set():
                        restart_delay = max(5, min(interval, 30))
                        _persist_scheduler_log(
                            level="warning",
                            event_code="scheduler_watchdog_restart",
                            message=(
                                "Scheduler worker stopped without a shutdown request. "
                                f"The watchdog will restart it in {restart_delay} seconds."
                            ),
                            details={
                                "interval": interval,
                                "restart_delay_seconds": restart_delay,
                            },
                        )
                        if _scheduler_stop_event.wait(restart_delay):
                            break
            finally:
                global _scheduler_process_lock
                if _scheduler_process_lock is not None:
                    _scheduler_process_lock.release()
                    _scheduler_process_lock = None

        _scheduler_thread = threading.Thread(
            target=_runner,
            name="scorecard-api-scheduler-watchdog",
            daemon=True,
        )
        _scheduler_thread.start()
        return True, "Scheduler service and automatic watchdog started successfully."


def is_scheduler_running_in_process() -> bool:
    with _scheduler_lock:
        return _scheduler_thread is not None and _scheduler_thread.is_alive()
