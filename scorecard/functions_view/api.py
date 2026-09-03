from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import re
import threading
from time import perf_counter
from typing import Any, Callable
from urllib.parse import parse_qsl, urljoin, urlsplit

import requests
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import (
    DataError,
    IntegrityError,
    OperationalError,
    ProgrammingError,
    close_old_connections,
    connection,
    transaction,
)
from django.db import models as django_models
from django.db.models import Count, F, Max, Min, Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.urls import reverse
from django.utils import timezone

from scorecard.api_forms import (
    ApiConfigurationForm,
    ApiConfigurationParameterFormSet,
    ApiEndpointForm,
    ApiImportForm,
    ApiMainSyncConfigurationForm,
    ApiImportScheduleForm,
    ApiRetrieveForm,
    ApiSchedulerControlForm,
)
from scorecard.functions_view.audit import log_api_audit
from scorecard.functions_view.notifications import (
    notify_api_import_result,
    notify_api_schedule_failure,
    notify_main_sync_result,
)
from scorecard.models import (
    ApiConfiguration,
    ApiConfigurationParameter,
    ApiEndpoint,
    ApiMainSyncConfiguration,
    ApiMainSyncRun,
    ApiImportRun,
    ApiImportSchedule,
    ApiSchedulerServiceLog,
    ApiSchedulerServiceStatus,
    CustomerCorporate,
    CustomerIndividual,
    CustomerLoan,
    MainCustomer,
    CustomerOverdraft,
)


DEFAULT_TIMEOUT = 60
TEST_REQUEST_TIMEOUT = 5
DEFAULT_BROWSER_TEST_TIMEOUT = 7
BULK_SYNC_BATCH_SIZE = 500
IMPORT_PROGRESS_CACHE_TTL = 60 * 60
IMPORT_PROGRESS_LOG_LIMIT = 200
FAILURE_SAMPLE_LIMIT = 20
SCHEDULER_SERVICE_NAME = "django_api_scheduler_service"
IMPORT_STALE_SECONDS = 180
MAIN_SYNC_LOCK_SECONDS = 300
API_TABLE_PER_PAGE_OPTIONS = (10, 20, 50, 100)
API_SUMMARY_CACHE_KEY = "scorecard:api-summary-counts:v2"
API_SUMMARY_CACHE_TTL_SECONDS = 300
MAIN_SYNC_SNAPSHOT_CACHE_TTL_SECONDS = 60
MODEL_TABLE_NAMES_CACHE_TTL_SECONDS = 30
MAIN_CUSTOMER_REQUIRED_TARGETS = [
    ApiEndpoint.TARGET_CUSTOMER_CORPORATE,
    ApiEndpoint.TARGET_CUSTOMER_INDIVIDUAL,
]
MAIN_SYNC_SOURCE_FIELDS = [
    ("corporate", "corporate_endpoint", ApiEndpoint.TARGET_CUSTOMER_CORPORATE),
    ("individual", "individual_endpoint", ApiEndpoint.TARGET_CUSTOMER_INDIVIDUAL),
]
API_TARGET_LABELS = dict(ApiEndpoint.TARGET_TABLE_CHOICES)
_manual_import_threads: dict[int, threading.Thread] = {}
_manual_import_threads_lock = threading.Lock()
_model_table_names_cache: set[str] = set()
_model_table_names_cached_at = 0.0
_model_table_names_lock = threading.Lock()


class ImportStoppedError(Exception):
    pass


class ScheduleAlreadyRunningError(Exception):
    pass


def _mask_debug_value(value: Any) -> Any:
    if value in (None, ""):
        return value
    text = str(value)
    if len(text) <= 6:
        return "*" * len(text)
    return f"{text[:3]}...{text[-2:]}"


def _sanitize_debug_context(context: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in context.items():
        lowered = key.lower()
        if isinstance(value, dict):
            nested: dict[str, Any] = {}
            for nested_key, nested_value in value.items():
                nested_lowered = str(nested_key).lower()
                if any(token in nested_lowered for token in ("secret", "token", "key", "authorization")):
                    nested[nested_key] = _mask_debug_value(nested_value)
                else:
                    nested[nested_key] = nested_value
            sanitized[key] = nested
        elif any(token in lowered for token in ("secret", "token", "key", "authorization")):
            sanitized[key] = _mask_debug_value(value)
        else:
            sanitized[key] = value
    return sanitized


def _terminal_log(event: str, **context: Any) -> None:
    payload = _sanitize_debug_context(context)
    message = f"[SCORECARD API DEBUG] {event}"
    if payload:
        message = f"{message} | {json.dumps(payload, default=str, ensure_ascii=False)}"
    print(message, flush=True)


DEFAULT_PARAMETER_ROWS = [
    {
        "name": "reporting_date",
        "default_value": "2026-03-30",
        "display_order": 1,
        "is_required": True,
        "use_for_testing": True,
        "use_for_retrieval": True,
        "is_active": True,
    },
    {
        "name": "branch_code",
        "default_value": "",
        "display_order": 2,
        "is_required": False,
        "use_for_testing": True,
        "use_for_retrieval": True,
        "is_active": True,
    },
]


def _missing_default_parameter_rows(configuration: ApiConfiguration | None) -> list[dict[str, Any]]:
    if configuration is None:
        return DEFAULT_PARAMETER_ROWS

    existing_names = set(
        ApiConfigurationParameter.objects.filter(configuration=configuration).values_list("name", flat=True)
    )
    return [row for row in DEFAULT_PARAMETER_ROWS if row["name"] not in existing_names]


def _ensure_default_parameters(configuration: ApiConfiguration | None) -> None:
    if configuration is None:
        return

    for row in _missing_default_parameter_rows(configuration):
        ApiConfigurationParameter.objects.create(configuration=configuration, **row)


@dataclass
class SyncStats:
    fetched: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    duplicate_skipped: int = 0
    missing_required_skipped: int = 0
    unchanged: int = 0
    failure_samples: list[dict[str, Any]] = field(default_factory=list)

    def add(self, other: "SyncStats") -> None:
        self.fetched += other.fetched
        self.created += other.created
        self.updated += other.updated
        self.skipped += other.skipped
        self.duplicate_skipped += other.duplicate_skipped
        self.missing_required_skipped += other.missing_required_skipped
        self.unchanged += other.unchanged
        remaining_slots = max(0, FAILURE_SAMPLE_LIMIT - len(self.failure_samples))
        if remaining_slots:
            self.failure_samples.extend(other.failure_samples[:remaining_slots])


def _stats_to_dict(stats: SyncStats) -> dict[str, int]:
    return {
        "fetched": stats.fetched,
        "created": stats.created,
        "updated": stats.updated,
        "skipped": stats.skipped,
        "duplicate_skipped": stats.duplicate_skipped,
        "missing_required_skipped": stats.missing_required_skipped,
        "unchanged": stats.unchanged,
        "total_loaded": stats.created + stats.updated,
    }


def _append_failure_sample(stats: SyncStats, *, reason: str, client_code: str | None = None, detail: str | None = None) -> None:
    if len(stats.failure_samples) >= FAILURE_SAMPLE_LIMIT:
        return
    stats.failure_samples.append(
        {
            "reason": reason,
            "client_code": client_code or "-",
            "detail": detail or "",
        }
    )


def _serialize_failure_details(stats: SyncStats) -> dict[str, Any]:
    return {
        "duplicate_skipped": stats.duplicate_skipped,
        "missing_required_skipped": stats.missing_required_skipped,
        "samples": stats.failure_samples,
    }


def _flatten_form_errors(form: Any) -> str:
    messages_list: list[str] = []
    for field_name, errors in form.errors.items():
        label = form.fields[field_name].label if field_name in form.fields else "Form"
        for error in errors:
            if field_name == "__all__":
                messages_list.append(str(error))
            else:
                messages_list.append(f"{label}: {error}")
    return " ".join(messages_list)


def _model_table_ready(model: Any) -> bool:
    global _model_table_names_cache, _model_table_names_cached_at

    try:
        now = perf_counter()
        if now - _model_table_names_cached_at > MODEL_TABLE_NAMES_CACHE_TTL_SECONDS:
            with _model_table_names_lock:
                if now - _model_table_names_cached_at > MODEL_TABLE_NAMES_CACHE_TTL_SECONDS:
                    _model_table_names_cache = set(connection.introspection.table_names())
                    _model_table_names_cached_at = now
        return model._meta.db_table in _model_table_names_cache
    except (ProgrammingError, OperationalError):
        return False


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    rounded_seconds = max(0, int(round(seconds)))
    minutes, secs = divmod(rounded_seconds, 60)
    hours, mins = divmod(minutes, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if hours or mins:
        parts.append(f"{mins}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def _normalized_api_per_page(value: str | None, default: int = 10) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        parsed = default
    if parsed not in API_TABLE_PER_PAGE_OPTIONS:
        return default
    return parsed


def _paginate_api_rows(
    rows: Any,
    *,
    page_number: str | None,
    per_page_value: str | None,
    default_per_page: int = 10,
):
    per_page = _normalized_api_per_page(per_page_value, default_per_page)
    paginator = Paginator(rows, per_page)
    page_obj = paginator.get_page(page_number or 1)
    return page_obj, per_page


def _build_import_error_message(*, endpoint: ApiEndpoint | None, exc: Exception) -> str:
    detail = str(exc).strip() or exc.__class__.__name__
    endpoint_name = endpoint.name if endpoint is not None else "Unknown endpoint"
    target_table = endpoint.get_target_table_display() if endpoint is not None else "unknown target"
    return (
        f"Import failed for endpoint '{endpoint_name}' into {target_table}. "
        f"Reason: {detail}"
    )


def _finalize_stopped_import_run(import_run: ApiImportRun, message: str) -> None:
    _finalize_import_run_record(
        import_run,
        status=ApiImportRun.STATUS_STOPPED,
        stats=SyncStats(
            fetched=import_run.fetched,
            created=import_run.created,
            updated=import_run.updated,
            unchanged=import_run.unchanged,
            skipped=import_run.skipped,
            duplicate_skipped=import_run.duplicate_skipped,
            missing_required_skipped=import_run.missing_required_skipped,
        ),
        completed_at=timezone.now(),
        duration_seconds=import_run.duration_seconds,
        failure_message=message,
        failure_details=import_run.failure_details or {"samples": []},
    )


def _mark_import_run_retried(previous_run: ApiImportRun, replacement_run: ApiImportRun) -> None:
    previous_run.status = ApiImportRun.STATUS_RETRIED
    previous_run.completed_at = previous_run.completed_at or timezone.now()
    previous_run.failure_message = (
        f"Retried successfully by import run started at {replacement_run.started_at:%Y-%m-%d %H:%M:%S}."
    )
    previous_run.save(update_fields=["status", "completed_at", "failure_message", "updated_at"])


def _refresh_stale_import_runs() -> None:
    if not _api_import_history_ready():
        return

    cutoff = timezone.now() - timedelta(seconds=IMPORT_STALE_SECONDS)
    running_runs = list(ApiImportRun.objects.filter(status=ApiImportRun.STATUS_RUNNING).select_related("triggered_by", "endpoint"))
    for run in running_runs:
        if _is_manual_import_thread_running(run.id):
            continue
        if run.triggered_by_id:
            progress_state = _get_import_progress(run.triggered_by_id)
            same_run = progress_state.get("import_run_id") == run.id and progress_state.get("status") == "running"
            updated_at_raw = progress_state.get("updated_at") or progress_state.get("started_at")
            progress_is_fresh = False
            if same_run and updated_at_raw:
                try:
                    progress_timestamp = datetime.fromisoformat(updated_at_raw)
                    if timezone.is_naive(progress_timestamp):
                        progress_timestamp = timezone.make_aware(progress_timestamp, timezone.get_current_timezone())
                    progress_is_fresh = progress_timestamp >= cutoff
                except ValueError:
                    progress_is_fresh = False
            if same_run and progress_is_fresh:
                continue
        elif run.started_at >= cutoff:
            continue

        _finalize_stopped_import_run(
            run,
            "Import was marked as stopped because no active background progress was detected.",
        )


def _format_schedule_next_run(next_run_at: datetime | None) -> str:
    if next_run_at is None:
        return "Not scheduled"
    return timezone.localtime(next_run_at).strftime("%Y-%m-%d %H:%M")


def _format_datetime_display(value: datetime | None) -> str:
    if value is None:
        return "-"
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M")


def _format_relative_age(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total_seconds = max(0, int(seconds))
    if total_seconds < 60:
        return f"{total_seconds}s ago"
    minutes, seconds_part = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds_part}s ago"
    hours, minutes_part = divmod(minutes, 60)
    return f"{hours}h {minutes_part}m ago"


def _format_elapsed_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    rounded = max(0, int(seconds))
    if rounded < 60:
        return f"{rounded}s ago"
    minutes, secs = divmod(rounded, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s ago"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m ago"


def _schedule_candidate_datetime(schedule: ApiImportSchedule, candidate_date: date) -> datetime:
    scheduled_time = schedule.run_time or time(0, 0)
    naive_candidate = datetime.combine(candidate_date, scheduled_time)
    return timezone.make_aware(naive_candidate, timezone.get_current_timezone())


def _monthly_run_date(year: int, month: int, desired_day: int | None) -> date:
    desired = desired_day or 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(desired, last_day))


def _calculate_next_run_at(schedule: ApiImportSchedule, now: datetime | None = None) -> datetime | None:
    if not schedule.is_active:
        return None

    current = timezone.localtime(now or timezone.now())
    today = current.date()

    if schedule.frequency == ApiImportSchedule.FREQUENCY_DAILY:
        candidate = _schedule_candidate_datetime(schedule, today)
        return candidate if candidate > current else candidate + timedelta(days=1)

    if schedule.frequency == ApiImportSchedule.FREQUENCY_WEEKLY:
        target_weekday = schedule.weekday if schedule.weekday is not None else 0
        days_ahead = (target_weekday - today.weekday()) % 7
        candidate_date = today + timedelta(days=days_ahead)
        candidate = _schedule_candidate_datetime(schedule, candidate_date)
        return candidate if candidate > current else candidate + timedelta(days=7)

    if schedule.frequency == ApiImportSchedule.FREQUENCY_MONTHLY:
        candidate_date = _monthly_run_date(today.year, today.month, schedule.day_of_month)
        candidate = _schedule_candidate_datetime(schedule, candidate_date)
        if candidate > current:
            return candidate
        next_month = today.replace(day=28) + timedelta(days=4)
        rolled = next_month.replace(day=1)
        return _schedule_candidate_datetime(
            schedule,
            _monthly_run_date(rolled.year, rolled.month, schedule.day_of_month),
        )

    return None


def _is_schedule_due(schedule: ApiImportSchedule, now: datetime | None = None) -> bool:
    if not schedule.is_active:
        return False

    current = timezone.localtime(now or timezone.now())
    if schedule.frequency == ApiImportSchedule.FREQUENCY_DAILY:
        candidate = _schedule_candidate_datetime(schedule, current.date())
    elif schedule.frequency == ApiImportSchedule.FREQUENCY_WEEKLY:
        target_weekday = schedule.weekday if schedule.weekday is not None else 0
        candidate_date = current.date() + timedelta(days=(target_weekday - current.date().weekday()) % 7)
        candidate = _schedule_candidate_datetime(schedule, candidate_date)
        if candidate > current:
            candidate -= timedelta(days=7)
    else:
        candidate = _schedule_candidate_datetime(
            schedule,
            _monthly_run_date(current.year, current.month, schedule.day_of_month),
        )
        if candidate > current:
            rolled = (current.date().replace(day=1) - timedelta(days=1))
            candidate = _schedule_candidate_datetime(
                schedule,
                _monthly_run_date(rolled.year, rolled.month, schedule.day_of_month),
            )

    if candidate > current:
        return False
    if schedule.last_run_at is None:
        return True
    return timezone.localtime(schedule.last_run_at) < candidate


def _import_progress_cache_key(user_id: int) -> str:
    return f"scorecard_api_import_progress_{user_id}"


def _import_stop_cache_key(import_run_id: int) -> str:
    return f"scorecard_api_import_stop_{import_run_id}"


def _set_import_progress(user_id: int, payload: dict[str, Any]) -> None:
    cache.set(_import_progress_cache_key(user_id), payload, timeout=IMPORT_PROGRESS_CACHE_TTL)


def _get_import_progress(user_id: int) -> dict[str, Any]:
    return cache.get(_import_progress_cache_key(user_id)) or {"status": "idle", "logs": []}


def _request_import_stop(import_run_id: int) -> None:
    cache.set(_import_stop_cache_key(import_run_id), True, timeout=IMPORT_PROGRESS_CACHE_TTL)


def _clear_import_stop(import_run_id: int) -> None:
    cache.delete(_import_stop_cache_key(import_run_id))


def _is_import_stop_requested(import_run_id: int) -> bool:
    return bool(cache.get(_import_stop_cache_key(import_run_id)))


def _register_manual_import_thread(import_run_id: int, worker: threading.Thread) -> None:
    with _manual_import_threads_lock:
        _manual_import_threads[import_run_id] = worker


def _clear_manual_import_thread(import_run_id: int) -> None:
    with _manual_import_threads_lock:
        _manual_import_threads.pop(import_run_id, None)


def _is_manual_import_thread_running(import_run_id: int) -> bool:
    with _manual_import_threads_lock:
        worker = _manual_import_threads.get(import_run_id)
        if worker is None:
            return False
        if worker.is_alive():
            return True
        _manual_import_threads.pop(import_run_id, None)
        return False


def _running_manual_import_for_user(user_id: int) -> ApiImportRun | None:
    if not _api_import_history_ready():
        return None
    return (
        ApiImportRun.objects.select_related("endpoint")
        .filter(triggered_by_id=user_id, status=ApiImportRun.STATUS_RUNNING, run_source=ApiImportRun.SOURCE_MANUAL)
        .order_by("-started_at", "-id")
        .first()
    )


def _build_import_progress_state(*, import_run: ApiImportRun, parameters: list[str], message: str) -> dict[str, Any]:
    now_iso = timezone.now().isoformat()
    return {
        "status": "running",
        "message": message,
        "endpoint_name": import_run.endpoint.name,
        "endpoint_id": import_run.endpoint_id,
        "target_table": import_run.target_table or import_run.endpoint.get_target_table_display(),
        "import_run_id": import_run.id,
        "logs": [],
        "stats": _stats_to_dict(SyncStats()),
        "started_at": now_iso,
        "updated_at": now_iso,
        "completed_at": None,
        "duration_seconds": None,
        "duration_display": "-",
        "parameters": parameters,
        "parameters_used": import_run.parameters_used or {},
        "error": None,
        "failure_details": {"samples": []},
    }


def _ensure_import_progress_for_user(user_id: int) -> dict[str, Any]:
    progress = _get_import_progress(user_id)
    if progress.get("status") == "running":
        return progress

    active_run = _running_manual_import_for_user(user_id)
    if active_run is None:
        return progress

    parameters = list(active_run.endpoint.parameters.order_by("display_order", "id").values_list("name", flat=True))
    recovered_progress = _build_import_progress_state(
        import_run=active_run,
        parameters=parameters,
        message=f"Import is running for {active_run.endpoint.name}.",
    )
    if not _is_manual_import_thread_running(active_run.id):
        recovered_progress["message"] = (
            f"Import run for {active_run.endpoint.name} is still marked as running. "
            "Waiting for the next background progress heartbeat."
        )
    _set_import_progress(user_id, recovered_progress)
    return recovered_progress


def _api_storage_ready() -> bool:
    try:
        table_names = set(connection.introspection.table_names())
    except (ProgrammingError, OperationalError):
        return False
    return {
        ApiConfiguration._meta.db_table,
        ApiConfigurationParameter._meta.db_table,
        ApiEndpoint._meta.db_table,
        ApiEndpoint.parameters.through._meta.db_table,
        ApiImportSchedule._meta.db_table,
    }.issubset(table_names)


def _api_import_history_ready() -> bool:
    return _model_table_ready(ApiImportRun)


def _api_scheduler_status_ready() -> bool:
    return _model_table_ready(ApiSchedulerServiceStatus)


def _main_customer_table_ready() -> bool:
    return _model_table_ready(MainCustomer)


def _api_main_sync_config_ready() -> bool:
    return _model_table_ready(ApiMainSyncConfiguration)


def _api_main_sync_history_ready() -> bool:
    return _model_table_ready(ApiMainSyncRun)


def _get_recent_import_runs(limit: int | None = 12):
    if not _api_import_history_ready():
        return ApiImportRun.objects.none()
    _refresh_stale_import_runs()
    queryset = ApiImportRun.objects.select_related("endpoint", "schedule", "triggered_by").order_by("-started_at", "-id")
    if limit is not None:
        return queryset[:limit]
    return queryset


def _main_sync_configuration() -> ApiMainSyncConfiguration | None:
    if not _api_main_sync_config_ready():
        return None
    configuration = ApiMainSyncConfiguration.objects.select_related(
        "corporate_endpoint",
        "individual_endpoint",
    ).order_by("id").first()
    if configuration is None:
        configuration = ApiMainSyncConfiguration.objects.create()
    return configuration


def _main_sync_source_slots() -> list[dict[str, Any]]:
    configuration = _main_sync_configuration()
    slots: list[dict[str, Any]] = []
    for key, field_name, target_table in MAIN_SYNC_SOURCE_FIELDS:
        endpoint = getattr(configuration, field_name, None) if configuration is not None else None
        slots.append(
            {
                "key": key,
                "field_name": field_name,
                "target_table": target_table,
                "endpoint": endpoint,
                "label": endpoint.name if endpoint else API_TARGET_LABELS[target_table],
            }
        )
    return slots


def _upsert_scheduler_service_status(
    *,
    last_status: str,
    last_message: str,
    last_run_count: int = 0,
    check_interval_seconds: int = 30,
    set_started: bool = False,
) -> None:
    if not _api_scheduler_status_ready():
        return

    now = timezone.now()
    defaults = {
        "last_heartbeat_at": now,
        "last_check_at": now,
        "check_interval_seconds": check_interval_seconds,
        "last_status": last_status,
        "last_message": last_message,
        "last_run_count": max(0, last_run_count),
    }
    if set_started:
        defaults["last_started_at"] = now
    ApiSchedulerServiceStatus.objects.update_or_create(
        service_name=SCHEDULER_SERVICE_NAME,
        defaults=defaults,
    )


def _get_scheduler_service_snapshot() -> dict[str, Any] | None:
    if not _api_scheduler_status_ready():
        return None

    status = ApiSchedulerServiceStatus.objects.filter(service_name=SCHEDULER_SERVICE_NAME).first()
    if status is None:
        return None

    current = timezone.now()
    heartbeat_age = None
    heartbeat_fresh = False
    if status.last_heartbeat_at:
        heartbeat_age = (current - status.last_heartbeat_at).total_seconds()
        heartbeat_fresh = heartbeat_age <= max(status.check_interval_seconds * 2, 90)

    return {
        "status": status,
        "is_running": heartbeat_fresh,
        "heartbeat_age_seconds": heartbeat_age,
    }


def _scheduler_control_record() -> ApiSchedulerServiceStatus:
    status, _ = ApiSchedulerServiceStatus.objects.get_or_create(
        service_name=SCHEDULER_SERVICE_NAME,
        defaults={
            "scheduler_enabled": True,
            "check_interval_seconds": 30,
            "retry_limit": 3,
            "retry_delay_seconds": 30,
            "last_status": "starting",
            "last_message": "Scheduler controls initialized. Waiting for the worker heartbeat.",
        },
    )
    return status


def _build_scheduler_status_payload() -> dict[str, Any]:
    snapshot = _get_scheduler_service_snapshot()
    if snapshot is None:
        return {
            "available": False,
            "state": "unknown",
            "state_label": "No heartbeat yet",
            "last_heartbeat": "-",
            "last_check": "-",
            "heartbeat_age": "-",
            "last_run_count": 0,
            "last_message": "No scheduler heartbeat has been recorded yet.",
            "command": "python manage.py run_api_scheduler_service",
        }

    status = snapshot["status"]
    if snapshot["is_running"] and status.last_status == "paused":
        state = "paused"
        state_label = "Paused"
    elif snapshot["is_running"]:
        state = "running"
        state_label = "Running"
    elif status.last_status == "failed":
        state = "failed"
        state_label = "Failed"
    elif status.last_status == "stopped":
        state = "stopped"
        state_label = "Stopped"
    else:
        state = "inactive"
        state_label = "Not currently active"

    return {
        "available": True,
        "state": state,
        "state_label": state_label,
        "last_heartbeat": _format_datetime_display(status.last_heartbeat_at),
        "last_check": _format_datetime_display(status.last_check_at),
        "heartbeat_age": _format_relative_age(snapshot.get("heartbeat_age_seconds")),
        "last_run_count": status.last_run_count,
        "last_message": status.last_message or "No scheduler message yet.",
        "scheduler_enabled": status.scheduler_enabled,
        "check_interval_seconds": status.check_interval_seconds,
        "command": "python manage.py run_api_scheduler_service",
    }


def _scheduler_service_status_payload() -> dict[str, Any]:
    snapshot = _get_scheduler_service_snapshot()
    if snapshot is None:
        return {
            "available": False,
            "state": "unknown",
            "state_label": "No heartbeat yet",
            "last_heartbeat": "-",
            "last_check": "-",
            "heartbeat_age": "-",
            "last_message": (
                "The scheduler service has not reported yet. "
                "If it is already running, restart it once so it can publish heartbeat status."
            ),
            "last_run_count": 0,
            "service_name": SCHEDULER_SERVICE_NAME,
            "command": "python manage.py run_api_scheduler_service",
        }

    status = snapshot["status"]
    if snapshot["is_running"] and status.last_status == "paused":
        state = "paused"
        state_label = "Paused"
    elif snapshot["is_running"]:
        state = "running"
        state_label = "Running"
    elif status.last_status == "failed":
        state = "failed"
        state_label = "Failed"
    elif status.last_status == "stopped":
        state = "stopped"
        state_label = "Stopped"
    else:
        state = "stale"
        state_label = "Not currently active"

    return {
        "available": True,
        "state": state,
        "state_label": state_label,
        "last_heartbeat": _format_datetime_display(status.last_heartbeat_at),
        "last_check": _format_datetime_display(status.last_check_at),
        "heartbeat_age": _format_elapsed_seconds(snapshot.get("heartbeat_age_seconds")),
        "last_message": status.last_message or "No scheduler message yet.",
        "last_run_count": status.last_run_count,
        "service_name": status.service_name,
        "scheduler_enabled": status.scheduler_enabled,
        "check_interval_seconds": status.check_interval_seconds,
        "run_import_schedules": status.run_import_schedules,
        "run_main_customer_sync": status.run_main_customer_sync,
        "run_historical_score_capture": status.run_historical_score_capture,
        "retry_limit": status.retry_limit,
        "retry_delay_seconds": status.retry_delay_seconds,
        "command": "python manage.py run_api_scheduler_service",
    }


def _recent_scheduler_logs(limit: int = 20) -> list[ApiSchedulerServiceLog]:
    if not _model_table_ready(ApiSchedulerServiceLog):
        return ApiSchedulerServiceLog.objects.none()
    queryset = ApiSchedulerServiceLog.objects.filter(service_name=SCHEDULER_SERVICE_NAME).order_by("-created_at", "-id")
    if limit is not None:
        return queryset[:limit]
    return queryset


SCHEDULER_EVENT_LABELS = {
    "service_started": "Scheduler Started",
    "service_stopped": "Scheduler Stopped",
    "service_failed": "Scheduler Failed",
    "scheduler_cycle_failed": "Scheduler Cycle Failed",
    "scheduler_watchdog_restart": "Scheduler Watchdog Restarted",
    "due_schedules_found": "Due Schedules Found",
    "schedule_import_success": "Schedule Import Succeeded",
    "schedule_import_failed": "Schedule Import Failed",
    "schedule_manual_run_success": "Manual Schedule Run Succeeded",
    "schedule_manual_run_skipped": "Manual Schedule Run Skipped",
    "schedule_manual_run_failed": "Manual Schedule Run Failed",
    "main_sync_success": "Main Sync Completed",
    "historical_scores_captured": "Historical Scores Captured",
    "auto_score_refresh_status": "Auto Score Refresh Status",
    "auto_score_refresh_completed": "Auto Score Refresh Completed",
    "auto_score_refresh_failed": "Auto Score Refresh Failed",
    "without_score_email_sent": "Without-Score Emails Sent",
    "without_score_email_failed": "Without-Score Email Failed",
}


def _serialize_scheduler_log(log: ApiSchedulerServiceLog) -> dict[str, Any]:
    return {
        "id": log.id,
        "created_display": timezone.localtime(log.created_at).strftime("%Y-%m-%d %H:%M:%S") if log.created_at else "-",
        "level": log.level,
        "level_display": log.get_level_display(),
        "event_code": log.event_code,
        "event_label": SCHEDULER_EVENT_LABELS.get(log.event_code, (log.event_code or "").replace("_", " ").title() or "-"),
        "message": log.message,
    }


def _active_configuration() -> ApiConfiguration | None:
    if not _api_storage_ready():
        return None
    return ApiConfiguration.objects.filter(is_active=True).order_by("-updated_at", "-id").first()


def _api_summary_counts() -> dict[str, int]:
    cached_counts = cache.get(API_SUMMARY_CACHE_KEY)
    if cached_counts is not None:
        return cached_counts

    counts = {
        "total_customers": MainCustomer.objects.count() if _main_customer_table_ready() else 0,
        "corporate_profiles": CustomerCorporate.objects.count(),
        "individual_profiles": CustomerIndividual.objects.count(),
        "loan_profiles": CustomerLoan.objects.count(),
        "overdraft_profiles": CustomerOverdraft.objects.count(),
        "saved_endpoints": ApiEndpoint.objects.count() if _api_storage_ready() else 0,
        "saved_parameters": ApiConfigurationParameter.objects.count() if _api_storage_ready() else 0,
        "saved_schedules": ApiImportSchedule.objects.count() if _api_storage_ready() else 0,
    }
    cache.set(API_SUMMARY_CACHE_KEY, counts, API_SUMMARY_CACHE_TTL_SECONDS)
    return counts



def _api_can_view(user: Any) -> bool:
    return bool(user.is_superuser or user.has_perm("scorecard.view_scorecard_api"))


def _api_can_manage_settings(user: Any) -> bool:
    return bool(user.is_superuser or user.has_perm("scorecard.manage_scorecard_api_settings"))


def _api_can_manage_endpoints(user: Any) -> bool:
    return bool(user.is_superuser or user.has_perm("scorecard.manage_scorecard_api_endpoints"))


def _api_can_manage_schedules(user: Any) -> bool:
    return bool(user.is_superuser or user.has_perm("scorecard.manage_scorecard_api_schedules"))


def _api_can_run_operations(user: Any) -> bool:
    return bool(user.is_superuser or user.has_perm("scorecard.run_scorecard_api_operations"))


def _api_base_context(
    request: HttpRequest,
    current_page: str,
    *,
    configuration: ApiConfiguration | None = None,
) -> dict[str, Any]:
    storage_ready = _api_storage_ready()
    summary_counts = _api_summary_counts() if storage_ready else {
        "total_customers": 0,
        "corporate_profiles": 0,
        "individual_profiles": 0,
        "loan_profiles": 0,
        "overdraft_profiles": 0,
        "saved_endpoints": 0,
        "saved_parameters": 0,
        "saved_schedules": 0,
    }
    if configuration is None and storage_ready:
        configuration = _active_configuration()

    can_view_api = _api_can_view(request.user)
    can_manage_settings = _api_can_manage_settings(request.user)
    can_manage_endpoints = _api_can_manage_endpoints(request.user)
    can_manage_schedules = _api_can_manage_schedules(request.user)
    can_run_operations = _api_can_run_operations(request.user)

    api_navigation: list[dict[str, str]] = []

    def add_nav_item(*, allowed: bool, label: str, url_name: str, key: str, description: str) -> None:
        if not allowed:
            return
        api_navigation.append(
            {
                "label": label,
                "url": reverse(url_name),
                "key": key,
                "description": description,
            }
        )

    add_nav_item(
        allowed=can_view_api or can_manage_settings,
        label="Settings",
        url_name="scorecard:api_settings",
        key="settings",
        description="Define API URL, key, and connection details",
    )
    add_nav_item(
        allowed=can_view_api or can_manage_schedules,
        label="Automation",
        url_name="scorecard:api_scheduler",
        key="scheduler",
        description="Schedule automatic API imports by date and time",
    )
    add_nav_item(
        allowed=can_view_api or can_manage_settings or can_run_operations,
        label="Auto Sync",
        url_name="scorecard:api_main_sync",
        key="main_sync",
        description="Track main customer sync readiness and completion status",
    )
    add_nav_item(
        allowed=can_view_api or can_manage_endpoints,
        label="Endpoints",
        url_name="scorecard:api_endpoints",
        key="endpoints",
        description="Create, test, and save endpoint definitions",
    )
    add_nav_item(
        allowed=can_view_api or can_manage_settings,
        label="Health",
        url_name="scorecard:api_health",
        key="health",
        description="Monitor endpoint test, import, schedule, and trend status",
    )
    add_nav_item(
        allowed=can_view_api or can_run_operations,
        label="Retrieve Data",
        url_name="scorecard:api_retrieve",
        key="retrieve",
        description="Use saved endpoints to retrieve data",
    )
    add_nav_item(
        allowed=can_run_operations,
        label="Import Data",
        url_name="scorecard:api_import",
        key="import",
        description="Preview endpoint data and load it into the target table",
    )

    return {
        "api_summary": {
            "total_customers": summary_counts["total_customers"],
            "corporate_profiles": summary_counts["corporate_profiles"],
            "individual_profiles": summary_counts["individual_profiles"],
            "loan_profiles": summary_counts["loan_profiles"],
            "overdraft_profiles": summary_counts["overdraft_profiles"],
            "saved_endpoints": summary_counts["saved_endpoints"],
            "saved_parameters": summary_counts["saved_parameters"],
            "saved_schedules": summary_counts["saved_schedules"],
        },
        "api_workspace_url": request.build_absolute_uri(reverse("scorecard:api_dashboard")),
        "api_configuration": configuration,
        "api_storage_ready": storage_ready,
        "api_current_page": current_page,
        "api_navigation": api_navigation,
        "api_can_view": can_view_api,
        "api_can_manage_settings": can_manage_settings,
        "api_can_manage_endpoints": can_manage_endpoints,
        "api_can_manage_schedules": can_manage_schedules,
        "api_can_run_operations": can_run_operations,
        "api_settings_read_only": not can_manage_settings,
        "api_retrieve_read_only": not can_run_operations,
    }

@login_required
def api_dashboard_view(request: HttpRequest) -> HttpResponse:
    return redirect("scorecard:api_settings")


@login_required

def api_settings_view(request: HttpRequest) -> HttpResponse:
    storage_ready = _api_storage_ready()
    configuration = _active_configuration()
    can_manage_settings = _api_can_manage_settings(request.user)
    edit_mode = can_manage_settings and (request.GET.get("edit") == "1" or configuration is None)

    if request.method == "POST":
        if not can_manage_settings:
            messages.error(request, "You do not have permission to change API settings.")
            return redirect("scorecard:api_settings")
        form = ApiConfigurationForm(request.POST, instance=configuration)
        parameter_formset = ApiConfigurationParameterFormSet(
            request.POST,
            queryset=ApiConfigurationParameter.objects.filter(configuration=configuration) if configuration else ApiConfigurationParameter.objects.none(),
            prefix="params",
        )
        if not storage_ready:
            messages.error(request, "API tables are not available yet. Run the scorecard migrations first, then save settings.")
        elif form.is_valid() and parameter_formset.is_valid():
            saved = form.save()
            ApiConfiguration.objects.exclude(pk=saved.pk).update(is_active=False)
            parameter_instances = parameter_formset.save(commit=False)
            for obj in parameter_formset.deleted_objects:
                obj.delete()
            for param in parameter_instances:
                param.configuration = saved
                param.save()
            log_api_audit(
                request.user,
                "save_settings",
                details=f"API settings saved for configuration '{saved.name}'.",
                object_id=saved.pk,
            )
            messages.success(request, "API settings saved successfully.")
            return redirect("scorecard:api_settings")
    else:
        initial = None
        if configuration is None:
            initial = {
                "name": "Primary API Configuration",
                "base_url": "http://192.168.4.48:8090",
                "auth_header_name": "X-API-KEY",
                "timeout_seconds": 60,
                "test_timeout_seconds": 5,
                "browser_timeout_seconds": 7,
                "is_active": True,
            }
        form = ApiConfigurationForm(instance=configuration, initial=initial)
        parameter_formset = ApiConfigurationParameterFormSet(
            initial=_missing_default_parameter_rows(configuration) if configuration is None else [],
            queryset=ApiConfigurationParameter.objects.filter(configuration=configuration) if configuration else ApiConfigurationParameter.objects.none(),
            prefix="params",
        )

    context = _api_base_context(request, "settings", configuration=configuration)
    context["form"] = form
    context["parameter_formset"] = parameter_formset
    context["edit_mode"] = edit_mode
    context["api_settings_missing_configuration"] = configuration is None
    return render(request, "api/settings.html", context)



@login_required
def api_scheduler_view(request: HttpRequest) -> HttpResponse:
    if not _api_storage_ready():
        messages.warning(request, "API tables are not available yet. Run the scorecard migrations first.")
        return redirect("scorecard:api_settings")

    configuration = _active_configuration()
    if configuration is None:
        messages.warning(request, "Save the API settings first before creating automation schedules.")
        return redirect("scorecard:api_settings")

    can_manage_schedules = _api_can_manage_schedules(request.user)

    endpoint_queryset = ApiEndpoint.objects.filter(
        configuration=configuration,
        is_active=True,
    ).exclude(target_table="").order_by("name")

    if not endpoint_queryset.exists() and can_manage_schedules:
        messages.warning(request, "Create at least one active endpoint with a target table before setting up automation.")
        return redirect("scorecard:api_endpoints")

    schedule_id = request.GET.get("schedule")
    schedule = ApiImportSchedule.objects.filter(pk=schedule_id).select_related("endpoint").first() if schedule_id else None

    if request.method == "POST":
        if not can_manage_schedules:
            messages.error(request, "You do not have permission to change automation schedules.")
            return redirect("scorecard:api_scheduler")
        schedule_id = request.POST.get("schedule_id")
        schedule = ApiImportSchedule.objects.filter(pk=schedule_id).select_related("endpoint").first() if schedule_id else None
        form = ApiImportScheduleForm(request.POST, instance=schedule, endpoint_queryset=endpoint_queryset)
        if form.is_valid():
            saved_schedule = form.save()
            log_api_audit(
                request.user,
                "save_schedule",
                details=f"Automation schedule '{saved_schedule.name}' saved for endpoint '{saved_schedule.endpoint.name}'.",
                object_id=saved_schedule.pk,
            )
            messages.success(request, "Automation schedule saved successfully.")
            return redirect("scorecard:api_scheduler")
    else:
        form = ApiImportScheduleForm(instance=schedule, endpoint_queryset=endpoint_queryset)

    schedules = ApiImportSchedule.objects.select_related("endpoint").order_by("name", "id")
    schedule_rows = [
        {
            "schedule": item,
            "next_run_at": _format_schedule_next_run(_calculate_next_run_at(item)),
        }
        for item in schedules
    ]

    context = _api_base_context(request, "scheduler", configuration=configuration)
    context["form"] = form
    context["editing_schedule"] = schedule if can_manage_schedules else None
    context["schedule_rows"] = schedule_rows
    scheduler_control = _scheduler_control_record()
    context["scheduler_control"] = scheduler_control
    context["scheduler_control_form"] = ApiSchedulerControlForm(instance=scheduler_control)
    context["editing_scheduler_controls"] = (
        can_manage_schedules and request.GET.get("edit_settings") == "1"
    )
    context["scheduler_command"] = "python manage.py run_api_scheduler_service"
    context["scheduler_service"] = _scheduler_service_status_payload()
    scheduler_logs_page_obj, scheduler_logs_per_page = _paginate_api_rows(
        _recent_scheduler_logs(limit=None),
        page_number=request.GET.get("scheduler_logs_page"),
        per_page_value=request.GET.get("scheduler_logs_per_page"),
        default_per_page=10,
    )
    scheduler_logs_page_obj.object_list = [
        _serialize_scheduler_log(log) for log in scheduler_logs_page_obj.object_list
    ]
    context["scheduler_logs_page_obj"] = scheduler_logs_page_obj
    context["scheduler_logs_per_page"] = scheduler_logs_per_page
    return render(request, "api/scheduler.html", context)


@login_required
def api_scheduler_control_view(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("scorecard:api_scheduler")
    if not _api_can_manage_schedules(request.user):
        messages.error(request, "You do not have permission to change scheduler controls.")
        return redirect("scorecard:api_scheduler")

    scheduler_control = _scheduler_control_record()
    previous_state = {
        "scheduler_enabled": scheduler_control.scheduler_enabled,
        "check_interval_seconds": scheduler_control.check_interval_seconds,
        "run_import_schedules": scheduler_control.run_import_schedules,
        "run_main_customer_sync": scheduler_control.run_main_customer_sync,
        "run_historical_score_capture": scheduler_control.run_historical_score_capture,
        "retry_limit": scheduler_control.retry_limit,
        "retry_delay_seconds": scheduler_control.retry_delay_seconds,
    }
    form = ApiSchedulerControlForm(request.POST, instance=scheduler_control)
    if not form.is_valid():
        for error in form.non_field_errors():
            messages.error(request, str(error))
        for field_name, errors in form.errors.items():
            label = form.fields[field_name].label if field_name in form.fields else "Scheduler setting"
            for error in errors:
                messages.error(request, f"{label}: {error}")
        return redirect("scorecard:api_scheduler")

    saved = form.save(commit=False)
    saved.last_status = "starting" if saved.scheduler_enabled else "paused"
    saved.last_message = (
        "Scheduler controls updated. Automatic work is enabled."
        if saved.scheduler_enabled
        else "Scheduler controls updated. Automatic work is paused."
    )
    saved.save()

    current_state = {
        key: getattr(saved, key)
        for key in previous_state
    }
    changed = [
        f"{key}={current_state[key]}"
        for key in previous_state
        if previous_state[key] != current_state[key]
    ]
    log_api_audit(
        request.user,
        "update_scheduler_controls",
        details="Scheduler controls updated. " + (", ".join(changed) if changed else "No values changed."),
        object_id=saved.pk,
    )

    if saved.scheduler_enabled:
        from scorecard.scheduler_runtime import ensure_scheduler_running

        ensure_scheduler_running(interval=saved.check_interval_seconds)

    messages.success(
        request,
        "Scheduler controls saved. Changes will be used by the next scheduler heartbeat.",
    )
    return redirect("scorecard:api_scheduler")


@login_required
def api_scheduler_toggle_view(request: HttpRequest, schedule_id: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("scorecard:api_scheduler")
    if not _api_can_manage_schedules(request.user):
        messages.error(request, "You do not have permission to change automation schedules.")
        return redirect("scorecard:api_scheduler")

    schedule = get_object_or_404(ApiImportSchedule, pk=schedule_id)
    schedule.is_active = not schedule.is_active
    schedule.save(update_fields=["is_active", "updated_at"])
    state_label = "enabled" if schedule.is_active else "paused"
    log_api_audit(
        request.user,
        "toggle_schedule",
        details=f"Automation schedule '{schedule.name}' was {state_label}.",
        object_id=schedule.pk,
    )
    messages.success(request, f"Schedule '{schedule.name}' is now {state_label}.")
    return redirect("scorecard:api_scheduler")


def _run_schedule_now_worker(schedule_id: int, user_id: int) -> None:
    close_old_connections()
    try:
        schedule = (
            ApiImportSchedule.objects
            .select_related("endpoint", "endpoint__configuration")
            .get(pk=schedule_id)
        )
        user = get_user_model().objects.filter(pk=user_id).first()
        stats, _, duration_seconds = execute_import_schedule(schedule, triggered_by=user)
        ApiSchedulerServiceLog.objects.create(
            service_name=SCHEDULER_SERVICE_NAME,
            level=ApiSchedulerServiceLog.LEVEL_SUCCESS,
            event_code="schedule_manual_run_success",
            message=(
                f"Manual run completed for {schedule.name}: fetched={stats.fetched}, "
                f"created={stats.created}, updated={stats.updated}, skipped={stats.skipped}."
            ),
            details={
                "schedule_id": schedule.pk,
                "triggered_by_id": user_id,
                "duration_seconds": duration_seconds,
            },
        )
    except ScheduleAlreadyRunningError as exc:
        ApiSchedulerServiceLog.objects.create(
            service_name=SCHEDULER_SERVICE_NAME,
            level=ApiSchedulerServiceLog.LEVEL_WARNING,
            event_code="schedule_manual_run_skipped",
            message=str(exc),
            details={"schedule_id": schedule_id, "triggered_by_id": user_id},
        )
    except Exception as exc:
        schedule = ApiImportSchedule.objects.filter(pk=schedule_id).first()
        if schedule is not None:
            schedule.last_run_at = timezone.now()
            schedule.last_status = "failed"
            schedule.last_message = str(exc)
            schedule.last_duration_seconds = None
            schedule.save(
                update_fields=[
                    "last_run_at",
                    "last_status",
                    "last_message",
                    "last_duration_seconds",
                    "updated_at",
                ]
            )
        ApiSchedulerServiceLog.objects.create(
            service_name=SCHEDULER_SERVICE_NAME,
            level=ApiSchedulerServiceLog.LEVEL_ERROR,
            event_code="schedule_manual_run_failed",
            message=f"Manual schedule run failed: {exc}",
            details={"schedule_id": schedule_id, "triggered_by_id": user_id},
        )
    finally:
        close_old_connections()


@login_required
def api_scheduler_run_now_view(request: HttpRequest, schedule_id: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("scorecard:api_scheduler")
    if not _api_can_manage_schedules(request.user):
        messages.error(request, "You do not have permission to run automation schedules.")
        return redirect("scorecard:api_scheduler")

    schedule = get_object_or_404(
        ApiImportSchedule.objects.select_related("endpoint", "endpoint__configuration"),
        pk=schedule_id,
    )
    lock_key = f"scorecard:api-schedule-running:{schedule.pk}"
    if cache.get(lock_key):
        messages.warning(request, f"Schedule '{schedule.name}' is already running.")
        return redirect("scorecard:api_scheduler")

    worker = threading.Thread(
        target=_run_schedule_now_worker,
        args=(schedule.pk, request.user.pk),
        name=f"scorecard-api-schedule-{schedule.pk}",
        daemon=True,
    )
    worker.start()
    log_api_audit(
        request.user,
        "run_schedule_now",
        details=f"Manual run requested for automation schedule '{schedule.name}'.",
        object_id=schedule.pk,
    )
    messages.success(
        request,
        f"Schedule '{schedule.name}' started in the background. Refresh the status shortly to see the result.",
    )
    return redirect("scorecard:api_scheduler")


@login_required
def api_scheduler_delete_view(request: HttpRequest, schedule_id: int) -> HttpResponse:
    if request.method != "POST":
        messages.error(request, "Schedule deletion must be submitted as a form action.")
        return redirect("scorecard:api_scheduler")

    schedule = get_object_or_404(ApiImportSchedule, pk=schedule_id)
    schedule_name = schedule.name
    log_api_audit(
        request.user,
        "delete_schedule",
        details=f"Automation schedule '{schedule_name}' deleted.",
        object_id=schedule.pk,
    )
    schedule.delete()
    messages.success(request, f"Schedule '{schedule_name}' was deleted successfully.")
    return redirect("scorecard:api_scheduler")


@login_required

def api_main_sync_view(request: HttpRequest) -> HttpResponse:
    if not _api_storage_ready():
        messages.warning(request, "API tables are not available yet. Run the scorecard migrations first.")
        return redirect("scorecard:api_settings")

    configuration = _active_configuration()
    if configuration is None:
        messages.warning(request, "Save the API settings first before viewing main sync status.")
        return redirect("scorecard:api_settings")

    can_manage_settings = _api_can_manage_settings(request.user)
    sync_configuration = _main_sync_configuration() if _api_main_sync_config_ready() else None
    edit_sync_configuration = can_manage_settings and request.GET.get("edit") == "1"
    if request.method == "POST":
        if not can_manage_settings:
            messages.error(request, "You do not have permission to change main sync configuration.")
            return redirect("scorecard:api_main_sync")
        sync_form = ApiMainSyncConfigurationForm(request.POST, instance=sync_configuration)
        if sync_form.is_valid():
            sync_configuration = sync_form.save()
            log_api_audit(
                request.user,
                "save_main_sync_configuration",
                details="Main customer auto-sync configuration saved.",
                object_id=sync_configuration.pk,
            )
            messages.success(request, "Auto sync configuration saved successfully.")
            return redirect("scorecard:api_main_sync")
        edit_sync_configuration = True
    else:
        sync_form = ApiMainSyncConfigurationForm(instance=sync_configuration)

    latest_runs = _get_recent_import_runs(limit=150) if _api_import_history_ready() else []
    reporting_dates: set[date] = set()
    for run in latest_runs:
        params = run.parameters_used or {}
        reporting_date_value = _parse_date(params.get("reporting_date"))
        if reporting_date_value:
            reporting_dates.add(reporting_date_value)
    reporting_dates.update(
        MainCustomer.objects.exclude(reporting_date__isnull=True)
        .order_by()
        .values_list("reporting_date", flat=True)
        .distinct()
    )

    sorted_reporting_dates = sorted(reporting_dates, reverse=True)
    sync_status_page_obj, sync_status_per_page = _paginate_api_rows(
        sorted_reporting_dates,
        page_number=request.GET.get("sync_status_page"),
        per_page_value=request.GET.get("sync_status_per_page"),
        default_per_page=10,
    )
    sync_rows = [
        _main_customer_sync_snapshot(reporting_date)
        for reporting_date in sync_status_page_obj.object_list
    ]
    _backfill_missing_main_sync_history(sync_rows)
    sync_status_page_obj.object_list = sync_rows
    recent_main_sync_runs = _recent_main_sync_run_queryset(limit=None)
    main_sync_history_page_obj, main_sync_history_per_page = _paginate_api_rows(
        recent_main_sync_runs,
        page_number=request.GET.get("main_sync_history_page"),
        per_page_value=request.GET.get("main_sync_history_per_page"),
        default_per_page=10,
    )
    main_sync_history_page_obj.object_list = [
        _serialize_main_sync_run(run) for run in main_sync_history_page_obj.object_list
    ]

    next_due_sync = None
    priority_order = {"countdown": 0, "awaiting_scheduler": 1, "ready": 2, "paused": 3, "waiting": 4, "synced": 5}
    for row in sorted(
        sync_rows,
        key=lambda item: (
            priority_order.get(item["status"], 9),
            item.get("countdown_seconds", 0),
            item["reporting_date"],
        ),
    ):
        if row["status"] in {"countdown", "awaiting_scheduler", "ready", "paused"}:
            next_due_sync = row
            break

    context = _api_base_context(request, "main_sync", configuration=configuration)
    context["sync_rows"] = sync_rows
    context["sync_status_page_obj"] = sync_status_page_obj
    context["sync_status_per_page"] = sync_status_per_page
    context["required_targets"] = [slot["label"] for slot in _main_sync_source_slots()]
    context["sync_status_colspan"] = len(context["required_targets"]) + (9 if context["api_can_run_operations"] else 8)
    context["sync_form"] = sync_form
    context["sync_configuration"] = sync_configuration
    context["next_due_sync"] = next_due_sync
    context["edit_sync_configuration"] = edit_sync_configuration
    context["recent_main_sync_runs"] = main_sync_history_page_obj.object_list
    context["main_sync_history_page_obj"] = main_sync_history_page_obj
    context["main_sync_history_per_page"] = main_sync_history_per_page
    return render(request, "api/main_sync.html", context)



@login_required
def api_main_sync_run_view(request: HttpRequest, reporting_date_value: str) -> HttpResponse:
    if request.method != "POST":
        return redirect("scorecard:api_main_sync")

    if not _api_storage_ready():
        messages.warning(request, "API tables are not available yet. Run the scorecard migrations first.")
        return redirect("scorecard:api_settings")

    reporting_date = _parse_date(reporting_date_value)
    if reporting_date is None:
        messages.error(request, "The selected reporting date is not valid for main customer sync.")
        return redirect("scorecard:api_main_sync")

    snapshot = _main_customer_sync_snapshot(reporting_date)
    if snapshot["status"] == "waiting":
        messages.warning(request, f"Main customer sync is still waiting. {snapshot['detail']}")
        return redirect("scorecard:api_main_sync")

    sync_result = _attempt_main_customer_sync(
        reporting_date,
        run_source=ApiMainSyncRun.SOURCE_MANUAL,
        triggered_by=request.user,
    )
    if sync_result["performed"]:
        log_api_audit(
            request.user,
            "run_main_sync",
            details=f"Manual main customer sync ran for reporting date {reporting_date.strftime('%Y-%m-%d')}. {sync_result['detail']}",
            object_id=reporting_date.strftime("%Y-%m-%d"),
        )
        messages.success(
            request,
            f"Main customer sync ran successfully for {reporting_date.strftime('%Y-%m-%d')}. {sync_result['detail']}",
        )
    else:
        log_api_audit(
            request.user,
            "skip_main_sync",
            details=f"Manual main customer sync did not run for reporting date {reporting_date.strftime('%Y-%m-%d')}. {sync_result['detail']}",
            object_id=reporting_date.strftime("%Y-%m-%d"),
        )
        messages.warning(
            request,
            f"Main customer sync did not run for {reporting_date.strftime('%Y-%m-%d')}. {sync_result['detail']}",
        )
    return redirect("scorecard:api_main_sync")


@login_required
def api_health_view(request: HttpRequest) -> HttpResponse:
    if not _api_storage_ready():
        messages.warning(request, "API tables are not available yet. Run the scorecard migrations first.")
        return redirect("scorecard:api_settings")

    configuration = _active_configuration()
    if configuration is None:
        messages.warning(request, "Save the API settings first before viewing endpoint health.")
        return redirect("scorecard:api_settings")

    endpoints = list(
        ApiEndpoint.objects.filter(configuration=configuration)
        .prefetch_related("parameters", "import_schedules")
        .order_by("name", "id")
    )
    recent_runs_by_endpoint: dict[int, list[ApiImportRun]] = {}
    if _api_import_history_ready():
        for run in (
            ApiImportRun.objects.select_related("schedule")
            .filter(endpoint__in=endpoints)
            .order_by("-started_at", "-id")
        ):
            recent_runs_by_endpoint.setdefault(run.endpoint_id, [])
            if len(recent_runs_by_endpoint[run.endpoint_id]) < 10:
                recent_runs_by_endpoint[run.endpoint_id].append(run)

    health_rows: list[dict[str, Any]] = []
    for endpoint in endpoints:
        endpoint_runs = recent_runs_by_endpoint.get(endpoint.id, [])
        last_import_run = endpoint_runs[0] if endpoint_runs else None
        last_schedule_run = next((run for run in endpoint_runs if run.run_source == ApiImportRun.SOURCE_SCHEDULE), None)
        linked_schedule = endpoint.import_schedules.order_by("name", "id").first()
        successful_runs = sum(1 for run in endpoint_runs if run.status == ApiImportRun.STATUS_SUCCESS)
        failed_runs = sum(1 for run in endpoint_runs if run.status == ApiImportRun.STATUS_FAILED)

        if failed_runs == 0 and successful_runs > 0:
            trend_label = "Healthy"
            trend_class = "success"
        elif failed_runs > successful_runs:
            trend_label = "Needs Attention"
            trend_class = "failed"
        elif failed_runs > 0:
            trend_label = "Mixed"
            trend_class = "warning"
        else:
            trend_label = "No Import Runs"
            trend_class = "neutral"

        health_rows.append(
            {
                "endpoint": endpoint,
                "last_test_time": _format_datetime_display(endpoint.last_tested_at),
                "last_test_status": endpoint.last_test_status or "not_tested",
                "last_import_time": _format_datetime_display(last_import_run.started_at if last_import_run else None),
                "last_import_status": last_import_run.status if last_import_run else "not_run",
                "last_schedule_run_time": _format_datetime_display(
                    last_schedule_run.completed_at if last_schedule_run else (linked_schedule.last_run_at if linked_schedule else None)
                ),
                "last_schedule_status": (
                    last_schedule_run.status if last_schedule_run else (linked_schedule.last_status or "not_run") if linked_schedule else "not_scheduled"
                ),
                "successful_runs": successful_runs,
                "failed_runs": failed_runs,
                "trend_label": trend_label,
                "trend_class": trend_class,
                "run_sample_size": len(endpoint_runs),
                "linked_schedule": linked_schedule,
            }
        )

    health_page_obj, health_per_page = _paginate_api_rows(
        health_rows,
        page_number=request.GET.get("health_page"),
        per_page_value=request.GET.get("health_per_page"),
        default_per_page=10,
    )

    context = _api_base_context(request, "health", configuration=configuration)
    context["health_rows"] = health_rows
    context["health_page_obj"] = health_page_obj
    context["health_per_page"] = health_per_page
    context["scheduler_service"] = _scheduler_service_status_payload()
    scheduler_logs_page_obj, scheduler_logs_per_page = _paginate_api_rows(
        _recent_scheduler_logs(limit=None),
        page_number=request.GET.get("scheduler_logs_page"),
        per_page_value=request.GET.get("scheduler_logs_per_page"),
        default_per_page=10,
    )
    scheduler_logs_page_obj.object_list = [
        _serialize_scheduler_log(log) for log in scheduler_logs_page_obj.object_list
    ]
    context["scheduler_logs_page_obj"] = scheduler_logs_page_obj
    context["scheduler_logs_per_page"] = scheduler_logs_per_page
    return render(request, "api/health.html", context)


@login_required
def api_scheduler_start_view(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("scorecard:api_health")

    if not (
        request.user.is_superuser
        or request.user.has_perm("scorecard.manage_scorecard_api_settings")
        or request.user.has_perm("scorecard.manage_scorecard_api_schedules")
    ):
        messages.error(request, "You do not have permission to start the scheduler service.")
        return redirect(request.POST.get("next") or "scorecard:api_health")

    if not _api_storage_ready():
        messages.warning(request, "API tables are not available yet. Run the scorecard migrations first.")
        return redirect("scorecard:api_settings")

    scheduler_control = _scheduler_control_record()
    if not scheduler_control.scheduler_enabled:
        scheduler_control.scheduler_enabled = True
        scheduler_control.last_status = "starting"
        scheduler_control.last_message = "Scheduler enabled from the API workspace."
        scheduler_control.save(
            update_fields=["scheduler_enabled", "last_status", "last_message", "updated_at"]
        )

    from scorecard.scheduler_runtime import ensure_scheduler_running

    started, detail = ensure_scheduler_running(interval=scheduler_control.check_interval_seconds)
    if started:
        log_api_audit(
            request.user,
            "start_scheduler_service",
            details="Scheduler service was started from the API workspace.",
            object_id=SCHEDULER_SERVICE_NAME,
        )
        messages.success(request, detail)
    else:
        messages.info(request, detail)
    return redirect(request.POST.get("next") or "scorecard:api_health")


@login_required
def api_scheduler_status_view(request: HttpRequest) -> JsonResponse:
    if not _api_storage_ready():
        return JsonResponse(
            {
                "available": False,
                "state": "unknown",
                "state_label": "Unavailable",
                "last_message": "API tables are not available yet.",
            }
        )
    return JsonResponse(_scheduler_service_status_payload())


@login_required
def api_endpoints_view(request: HttpRequest) -> HttpResponse:
    if not _api_storage_ready():
        messages.warning(request, "API tables are not available yet. Run the scorecard migrations first.")
        return redirect("scorecard:api_settings")

    configuration = _active_configuration()
    if configuration is None:
        messages.warning(request, "Save the API settings first before creating endpoints.")
        return redirect("scorecard:api_settings")

    can_manage_endpoints = _api_can_manage_endpoints(request.user)

    endpoint_id = request.GET.get("endpoint")
    endpoint = None
    test_result = None
    if endpoint_id:
        endpoint = get_object_or_404(ApiEndpoint, pk=endpoint_id)

    if request.method == "POST":
        if not can_manage_endpoints:
            messages.error(request, "You do not have permission to change API endpoints.")
            return redirect("scorecard:api_endpoints")
        endpoint_id = request.POST.get("endpoint_id")
        endpoint = ApiEndpoint.objects.filter(pk=endpoint_id).first() if endpoint_id else None
        form = ApiEndpointForm(request.POST, instance=endpoint, configuration=configuration)
        action = request.POST.get("action", "save")

        if form.is_valid():
            endpoint_obj = form.save(commit=False)
            endpoint_obj.configuration = configuration
            endpoint_obj.code = _generate_endpoint_code(endpoint_obj.name, endpoint.pk if endpoint else None)
            endpoint_obj.http_method = "GET"

            if action == "test":
                selected_parameters = list(form.cleaned_data.get("parameters") or [])
                ok, status_code, payload, preview_text = _test_endpoint_connection(
                    configuration,
                    endpoint_obj,
                    selected_parameters,
                )
                if endpoint and endpoint.pk:
                    endpoint_obj.pk = endpoint.pk
                    endpoint_obj.last_tested_at = timezone.now()
                    endpoint_obj.last_test_status = "success" if ok else "failed"
                    endpoint_obj.last_test_message = preview_text
                    endpoint_obj.save()
                    endpoint = endpoint_obj
                test_result = {
                    "ok": ok,
                    "status_code": status_code,
                    "payload": payload,
                    "preview_text": preview_text,
                }
                messages.success(request, f"Endpoint test completed with status {status_code}.") if ok else messages.error(request, preview_text)

            if action == "save":
                endpoint_obj.save()
                form.save_m2m()
                log_api_audit(
                    request.user,
                    "save_endpoint",
                    details=f"Endpoint '{endpoint_obj.name}' saved for target '{endpoint_obj.get_target_table_display()}'.",
                    object_id=endpoint_obj.pk,
                )
                messages.success(request, "Endpoint saved successfully.")
                return redirect(f"{reverse('scorecard:api_endpoints')}?endpoint={endpoint_obj.pk}")
        if getattr(form, "path_warning", None):
            messages.warning(request, form.path_warning)
    else:
        form = ApiEndpointForm(instance=endpoint, configuration=configuration)

    saved_endpoints = (
        ApiEndpoint.objects.filter(configuration=configuration)
        .prefetch_related("parameters")
        .order_by("name", "id")
    )

    context = _api_base_context(request, "endpoints", configuration=configuration)
    context["form"] = form
    context["editing_endpoint"] = endpoint if can_manage_endpoints else None
    context["saved_endpoints"] = saved_endpoints
    context["test_result"] = test_result
    context["browser_test_timeout_ms"] = (
        (configuration.browser_timeout_seconds if configuration else DEFAULT_BROWSER_TEST_TIMEOUT) * 1000
    )
    return render(request, "api/endpoints.html", context)


@login_required
def api_endpoint_test_view(request: HttpRequest) -> JsonResponse:
    if request.method != "POST":
        return JsonResponse({"ok": False, "message": "POST method is required."}, status=405)

    if not _api_storage_ready():
        return JsonResponse(
            {"ok": False, "message": "API tables are not available yet. Run the scorecard migrations first."},
            status=400,
        )

    configuration = _active_configuration()
    if configuration is None:
        return JsonResponse(
            {"ok": False, "message": "Save the API settings first before testing endpoints."},
            status=400,
        )

    _terminal_log(
        "endpoint_test.view.start",
        endpoint_id=request.POST.get("endpoint_id"),
        posted_reporting_date=request.POST.get("reporting_date"),
        post_keys=sorted(request.POST.keys()),
    )
    endpoint_id = request.POST.get("endpoint_id")
    endpoint = ApiEndpoint.objects.filter(pk=endpoint_id).first() if endpoint_id else None
    form = ApiEndpointForm(request.POST, instance=endpoint, configuration=configuration)
    if not form.is_valid():
        _terminal_log(
            "endpoint_test.view.invalid_form",
            endpoint_id=endpoint_id,
            errors=form.errors,
        )
        return JsonResponse(
            {
                "ok": False,
                "message": "Please correct the endpoint form before testing.",
                "errors": form.errors,
            },
            status=400,
        )

    endpoint_obj = form.save(commit=False)
    endpoint_obj.configuration = configuration
    endpoint_obj.code = _generate_endpoint_code(endpoint_obj.name, endpoint.pk if endpoint else None)
    endpoint_obj.http_method = "GET"
    selected_parameters = list(form.cleaned_data.get("parameters") or [])
    override_params: dict[str, str] = {}
    reporting_date_value = (request.POST.get("reporting_date") or "").strip()
    if reporting_date_value:
        override_params["reporting_date"] = reporting_date_value
    _terminal_log(
        "endpoint_test.view.preflight",
        endpoint_id=endpoint_id,
        endpoint_name=getattr(endpoint_obj, "name", ""),
        endpoint_path=getattr(endpoint_obj, "path", ""),
        selected_parameters=[getattr(parameter, "name", "") for parameter in selected_parameters],
        override_params=override_params,
    )
    ok, status_code, payload, preview_text = _test_endpoint_connection(
        configuration,
        endpoint_obj,
        selected_parameters,
        override_params=override_params,
    )
    _terminal_log(
        "endpoint_test.view.result",
        endpoint_id=endpoint_id,
        ok=ok,
        status_code=status_code,
        preview_text=_truncate_text(preview_text, 500),
    )

    if endpoint and endpoint.pk:
        endpoint.last_tested_at = timezone.now()
        endpoint.last_test_status = "success" if ok else "failed"
        endpoint.last_test_message = preview_text
        endpoint.save(update_fields=["last_tested_at", "last_test_status", "last_test_message"])

    response_status = 200 if ok else 502
    return JsonResponse(
        {
            "ok": ok,
            "status_code": status_code,
            "message": "Endpoint test succeeded." if ok else preview_text,
            "preview_text": preview_text,
            "path_warning": getattr(form, "path_warning", None),
            "saved_endpoint": bool(endpoint and endpoint.pk),
        },
        status=response_status,
    )


@login_required
def api_endpoint_save_view(request: HttpRequest) -> JsonResponse:
    if request.method != "POST":
        return JsonResponse({"ok": False, "message": "POST method is required."}, status=405)

    if not _api_storage_ready():
        return JsonResponse(
            {"ok": False, "message": "API tables are not available yet. Run the scorecard migrations first."},
            status=400,
        )

    configuration = _active_configuration()
    if configuration is None:
        return JsonResponse(
            {"ok": False, "message": "Save the API settings first before creating endpoints."},
            status=400,
        )

    endpoint_id = request.POST.get("endpoint_id")
    endpoint = ApiEndpoint.objects.filter(pk=endpoint_id).first() if endpoint_id else None
    form = ApiEndpointForm(request.POST, instance=endpoint, configuration=configuration)
    if not form.is_valid():
        detailed_message = _flatten_form_errors(form) or "Please correct the endpoint form before saving."
        return JsonResponse(
            {
                "ok": False,
                "message": detailed_message,
                "errors": form.errors,
            },
            status=400,
        )

    endpoint_obj = form.save(commit=False)
    endpoint_obj.configuration = configuration
    endpoint_obj.code = _generate_endpoint_code(endpoint_obj.name, endpoint.pk if endpoint else None)
    endpoint_obj.http_method = "GET"
    endpoint_obj.save()
    form.save_m2m()

    return JsonResponse(
        {
            "ok": True,
            "message": "Endpoint saved successfully.",
            "endpoint_id": endpoint_obj.pk,
            "redirect_url": reverse("scorecard:api_endpoints"),
            "path_warning": getattr(form, "path_warning", None),
        }
    )


@login_required
def api_endpoint_delete_view(request: HttpRequest, endpoint_id: int) -> HttpResponse:
    if request.method != "POST":
        messages.error(request, "Endpoint deletion must be submitted as a form action.")
        return redirect("scorecard:api_endpoints")

    if not _api_storage_ready():
        messages.warning(request, "API tables are not available yet. Run the scorecard migrations first.")
        return redirect("scorecard:api_settings")

    endpoint = get_object_or_404(ApiEndpoint, pk=endpoint_id)
    endpoint_name = endpoint.name
    log_api_audit(
        request.user,
        "delete_endpoint",
        details=f"Endpoint '{endpoint_name}' deleted.",
        object_id=endpoint.pk,
    )
    endpoint.delete()
    messages.success(request, f"Endpoint '{endpoint_name}' was deleted successfully.")
    return redirect("scorecard:api_endpoints")


@login_required

def api_retrieve_view(request: HttpRequest) -> HttpResponse:
    if not _api_storage_ready():
        messages.warning(request, "API tables are not available yet. Run the scorecard migrations first.")
        return redirect("scorecard:api_settings")

    configuration = _active_configuration()
    endpoints = ApiEndpoint.objects.filter(is_active=True).select_related("configuration").prefetch_related("parameters").order_by("name")
    if configuration is None:
        messages.warning(request, "Save the API settings first before retrieving data.")
        return redirect("scorecard:api_settings")

    can_run_operations = _api_can_run_operations(request.user)
    selected_endpoint_id = request.GET.get("endpoint") or request.POST.get("endpoint")
    selected_endpoint = None
    if selected_endpoint_id:
        selected_endpoint = endpoints.filter(pk=selected_endpoint_id).first()
    if selected_endpoint is None:
        selected_endpoint = endpoints.first()

    retrieval_parameters = _endpoint_retrieval_parameters(selected_endpoint)

    result_payload = None
    result_items = None
    result_error = None
    response_status = None
    result_preview_text = None

    if request.method == "POST":
        if not can_run_operations:
            messages.error(request, "You have read-only access to API retrieval. Retrieval actions are not available.")
            redirect_url = reverse("scorecard:api_retrieve")
            if selected_endpoint_id:
                redirect_url = f"{redirect_url}?endpoint={selected_endpoint_id}"
            return redirect(redirect_url)
        form = ApiRetrieveForm(request.POST, endpoint_queryset=endpoints, parameter_definitions=retrieval_parameters)
        if form.is_valid():
            endpoint = form.cleaned_data["endpoint"]
            if endpoint != selected_endpoint:
                selected_endpoint = endpoint
                retrieval_parameters = _endpoint_retrieval_parameters(selected_endpoint)
                form = ApiRetrieveForm(request.POST, endpoint_queryset=endpoints, parameter_definitions=retrieval_parameters)
                form.is_valid()
            params = _dynamic_form_parameters(form)
            extra_query_string = form.cleaned_data["extra_query_string"] or ""
            params.update(_parse_query_string(extra_query_string))
            ok, response_status, result_payload = _execute_get_request(endpoint.configuration, endpoint.path, params)
            if ok:
                result_items = result_payload.get("data") if isinstance(result_payload, dict) else result_payload
                result_preview_text = _format_test_payload(result_payload, endpoint=endpoint)
                messages.success(request, f"Data retrieved successfully with status {response_status}.")
            else:
                result_error = _format_test_payload(result_payload, endpoint=endpoint)
                messages.error(request, result_error)
    else:
        initial = {}
        if selected_endpoint:
            initial = {
                "endpoint": selected_endpoint,
            }
        for parameter in retrieval_parameters:
            initial[f"param_{parameter.pk}"] = parameter.default_value
        form = ApiRetrieveForm(initial=initial, endpoint_queryset=endpoints, parameter_definitions=retrieval_parameters)

    context = _api_base_context(request, "retrieve", configuration=configuration)
    context["form"] = form
    context["result_payload"] = result_payload
    context["result_items"] = result_items[:10] if isinstance(result_items, list) else None
    context["result_error"] = result_error
    context["response_status"] = response_status
    context["result_preview_text"] = result_preview_text
    context["selected_endpoint"] = selected_endpoint
    return render(request, "api/retrieve.html", context)



@login_required
def api_import_view(request: HttpRequest) -> HttpResponse:
    if not _api_storage_ready():
        messages.warning(request, "API tables are not available yet. Run the scorecard migrations first.")
        return redirect("scorecard:api_settings")

    configuration = _active_configuration()
    endpoints = ApiEndpoint.objects.filter(is_active=True).select_related("configuration").prefetch_related("parameters").order_by("name")
    if configuration is None:
        messages.warning(request, "Save the API settings first before importing data.")
        return redirect("scorecard:api_settings")

    importable_endpoints = endpoints.exclude(target_table="")
    if not importable_endpoints.exists():
        messages.warning(request, "Create at least one endpoint with a target table before importing data.")
        return redirect("scorecard:api_endpoints")

    selected_endpoint_id = request.GET.get("endpoint") or request.POST.get("endpoint")
    selected_endpoint = importable_endpoints.filter(pk=selected_endpoint_id).first() if selected_endpoint_id else None

    import_parameters = _endpoint_retrieval_parameters(selected_endpoint)
    import_stats = None
    import_error = None
    import_completed_at = None
    import_duration_seconds = None
    import_failure_details = None

    if request.method == "POST":
        form = ApiImportForm(request.POST, endpoint_queryset=importable_endpoints, parameter_definitions=import_parameters)
        if form.is_valid():
            endpoint = form.cleaned_data["endpoint"]
            if endpoint != selected_endpoint:
                selected_endpoint = endpoint
                import_parameters = _endpoint_retrieval_parameters(selected_endpoint)
                form = ApiImportForm(request.POST, endpoint_queryset=importable_endpoints, parameter_definitions=import_parameters)
                form.is_valid()
            params = _dynamic_form_parameters(form)
            params.update(_parse_query_string(form.cleaned_data["extra_query_string"] or ""))
            action = request.POST.get("action", "import")
            try:
                if action == "import":
                    import_stats, import_completed_at, import_duration_seconds = _run_endpoint_import(endpoint, params)
                    import_failure_details = _serialize_failure_details(import_stats)
                    messages.success(request, f"Imported {import_stats.fetched} records into {endpoint.get_target_table_display()}.")
            except Exception as exc:
                import_error = _build_import_error_message(endpoint=endpoint, exc=exc)
                messages.error(request, import_error)
    else:
        initial = {}
        if selected_endpoint:
            initial["endpoint"] = selected_endpoint
        form = ApiImportForm(initial=initial, endpoint_queryset=importable_endpoints, parameter_definitions=import_parameters)

    context = _api_base_context(request, "import", configuration=configuration)
    context["form"] = form
    context["selected_endpoint"] = selected_endpoint
    context["import_stats"] = import_stats
    context["import_error"] = import_error
    context["import_completed_at"] = import_completed_at
    context["import_duration_seconds"] = import_duration_seconds
    context["import_duration_display"] = _format_duration(import_duration_seconds)
    context["import_failure_details"] = import_failure_details
    recent_import_runs = _get_recent_import_runs(limit=None)
    import_history_page_obj, import_history_per_page = _paginate_api_rows(
        recent_import_runs,
        page_number=request.GET.get("import_history_page"),
        per_page_value=request.GET.get("import_history_per_page"),
        default_per_page=10,
    )
    context["recent_import_runs"] = recent_import_runs
    context["import_history_page_obj"] = import_history_page_obj
    context["import_history_per_page"] = import_history_per_page
    return render(request, "api/import.html", context)


def _execute_manual_import_run(
    *,
    request: HttpRequest,
    endpoint: ApiEndpoint,
    params: dict[str, str],
    retry_source_run: ApiImportRun | None = None,
) -> JsonResponse:
    existing_run = _running_manual_import_for_user(request.user.id)
    if existing_run is not None:
        _ensure_import_progress_for_user(request.user.id)
        return JsonResponse(
            {
                "ok": False,
                "message": (
                    f"An import is already running for {existing_run.endpoint.name}. "
                    "Please wait for it to finish or stop it before starting another one."
                ),
                "running": True,
                "import_run_id": existing_run.id,
                "endpoint_id": existing_run.endpoint_id,
                "endpoint_name": existing_run.endpoint.name,
            },
            status=409,
        )

    import_run = _create_import_run_record(
        endpoint=endpoint,
        run_source=ApiImportRun.SOURCE_MANUAL,
        parameters_used=params,
        triggered_by=request.user,
    )
    if import_run is None:
        return JsonResponse({"ok": False, "message": "Import history is not available yet."}, status=500)

    _clear_import_stop(import_run.id)
    base_progress = _build_import_progress_state(
        import_run=import_run,
        parameters=[parameter.name for parameter in endpoint.parameters.all()],
        message=f"Starting import for {endpoint.name}...",
    )
    _set_import_progress(request.user.id, base_progress)
    _start_manual_import_worker(
        import_run_id=import_run.id,
        endpoint_id=endpoint.id,
        user_id=request.user.id,
        params=params,
        retry_source_run_id=retry_source_run.id if retry_source_run is not None else None,
    )
    return JsonResponse(
        {
            "ok": True,
            "started": True,
            "message": f"Import started for {endpoint.name}. It will continue in the background until it finishes.",
            "import_run_id": import_run.id,
            "endpoint_id": endpoint.id,
            "endpoint_name": endpoint.name,
            "target_table": endpoint.get_target_table_display(),
            "parameters": base_progress["parameters"],
            "parameters_used": params,
        },
        status=202,
    )


def _run_manual_import_in_background(
    *,
    import_run_id: int,
    endpoint_id: int,
    user_id: int,
    params: dict[str, str],
    retry_source_run_id: int | None = None,
) -> None:
    close_old_connections()
    try:
        endpoint = ApiEndpoint.objects.prefetch_related("parameters").get(pk=endpoint_id)
        import_run = ApiImportRun.objects.select_related("endpoint", "triggered_by").get(pk=import_run_id)
        user = get_user_model().objects.filter(pk=user_id).first()
        retry_source_run = ApiImportRun.objects.filter(pk=retry_source_run_id).first() if retry_source_run_id else None

        def progress_callback(page_number: int, page_stats: SyncStats, total_stats: SyncStats) -> None:
            progress_state = _get_import_progress(user_id)
            logs = list(progress_state.get("logs") or [])
            logs.append(
                {
                    "page": page_number,
                    "fetched": page_stats.fetched,
                    "created": page_stats.created,
                    "updated": page_stats.updated,
                    "unchanged": page_stats.unchanged,
                    "skipped": page_stats.skipped,
                    "duplicate_skipped": page_stats.duplicate_skipped,
                    "missing_required_skipped": page_stats.missing_required_skipped,
                    "logged_at": timezone.now().strftime("%H:%M:%S"),
                    "message": (
                        f"Page {page_number} loaded: fetched {page_stats.fetched}, "
                        f"created {page_stats.created}, updated {page_stats.updated}, unchanged {page_stats.unchanged}, "
                        f"skipped {page_stats.skipped}, duplicates {page_stats.duplicate_skipped}, "
                        f"missing required {page_stats.missing_required_skipped}."
                    ),
                }
            )
            progress_state.update(
                {
                    "status": "running",
                    "message": (
                        f"Page {page_number} completed. Continuing with the next page. "
                        f"Duplicates skipped so far: {total_stats.duplicate_skipped}. "
                        f"Missing required so far: {total_stats.missing_required_skipped}."
                    ),
                    "logs": logs[-IMPORT_PROGRESS_LOG_LIMIT:],
                    "stats": _stats_to_dict(total_stats),
                    "last_page": page_number,
                    "updated_at": timezone.now().isoformat(),
                }
            )
            _set_import_progress(user_id, progress_state)

        import_stats, import_completed_at, import_duration_seconds = _run_endpoint_import(
            endpoint,
            params,
            progress_callback=progress_callback,
            should_stop=lambda: _is_import_stop_requested(import_run.id),
        )
        completed_state = _get_import_progress(user_id)
        completed_state.update(
            {
                "status": "completed",
                "message": f"Import completed successfully for {endpoint.name}.",
                "stats": _stats_to_dict(import_stats),
                "completed_at": import_completed_at.isoformat(),
                "duration_seconds": import_duration_seconds,
                "duration_display": _format_duration(import_duration_seconds),
                "updated_at": timezone.now().isoformat(),
                "failure_details": _serialize_failure_details(import_stats),
            }
        )
        _set_import_progress(user_id, completed_state)
        _finalize_import_run_record(
            import_run,
            status=ApiImportRun.STATUS_SUCCESS,
            stats=import_stats,
            completed_at=import_completed_at,
            duration_seconds=import_duration_seconds,
        )
        log_api_audit(
            user,
            "run_import",
            details=(
                f"Import completed for endpoint '{endpoint.name}' into '{endpoint.get_target_table_display()}'. "
                f"Fetched: {import_stats.fetched}; Created: {import_stats.created}; Updated: {import_stats.updated}; "
                f"Unchanged: {import_stats.unchanged}; Skipped: {import_stats.skipped}."
            ),
            object_id=import_run.pk,
        )
        if retry_source_run is not None:
            _mark_import_run_retried(retry_source_run, import_run)
        _clear_import_stop(import_run.id)
    except ImportStoppedError as exc:
        import_run = ApiImportRun.objects.select_related("endpoint", "triggered_by").filter(pk=import_run_id).first()
        endpoint = import_run.endpoint if import_run is not None else ApiEndpoint.objects.filter(pk=endpoint_id).first()
        user = get_user_model().objects.filter(pk=user_id).first()
        stopped_state = _get_import_progress(user_id)
        logs = list(stopped_state.get("logs") or [])
        logs.append(
            {
                "page": stopped_state.get("last_page") or 0,
                "fetched": stopped_state.get("stats", {}).get("fetched", 0),
                "created": stopped_state.get("stats", {}).get("created", 0),
                "updated": stopped_state.get("stats", {}).get("updated", 0),
                "unchanged": stopped_state.get("stats", {}).get("unchanged", 0),
                "skipped": stopped_state.get("stats", {}).get("skipped", 0),
                "duplicate_skipped": stopped_state.get("stats", {}).get("duplicate_skipped", 0),
                "missing_required_skipped": stopped_state.get("stats", {}).get("missing_required_skipped", 0),
                "logged_at": timezone.now().strftime("%H:%M:%S"),
                "message": str(exc),
            }
        )
        stopped_state.update(
            {
                "status": "stopped",
                "message": f"Import stopped for {endpoint.name if endpoint else 'the selected endpoint'}.",
                "error": str(exc),
                "completed_at": timezone.now().isoformat(),
                "updated_at": timezone.now().isoformat(),
                "logs": logs[-IMPORT_PROGRESS_LOG_LIMIT:],
            }
        )
        _set_import_progress(user_id, stopped_state)
        if import_run is not None:
            _finalize_stopped_import_run(import_run, str(exc))
            log_api_audit(
                user,
                "stop_import",
                details=f"Import stopped for endpoint '{endpoint.name}'. Reason: {str(exc)}",
                object_id=import_run.pk,
            )
            _clear_import_stop(import_run.id)
    except Exception as exc:
        import_run = ApiImportRun.objects.select_related("endpoint", "triggered_by").filter(pk=import_run_id).first()
        endpoint = import_run.endpoint if import_run is not None else ApiEndpoint.objects.filter(pk=endpoint_id).first()
        user = get_user_model().objects.filter(pk=user_id).first()
        import_error_message = _build_import_error_message(endpoint=endpoint, exc=exc)
        failed_state = _get_import_progress(user_id)
        logs = list(failed_state.get("logs") or [])
        logs.append(
            {
                "page": failed_state.get("last_page") or 0,
                "fetched": failed_state.get("stats", {}).get("fetched", 0),
                "created": failed_state.get("stats", {}).get("created", 0),
                "updated": failed_state.get("stats", {}).get("updated", 0),
                "unchanged": failed_state.get("stats", {}).get("unchanged", 0),
                "skipped": failed_state.get("stats", {}).get("skipped", 0),
                "duplicate_skipped": failed_state.get("stats", {}).get("duplicate_skipped", 0),
                "missing_required_skipped": failed_state.get("stats", {}).get("missing_required_skipped", 0),
                "logged_at": timezone.now().strftime("%H:%M:%S"),
                "message": import_error_message,
            }
        )
        failed_state.update(
            {
                "status": "failed",
                "message": (
                    f"Import failed for {endpoint.name if endpoint else 'the selected endpoint'}. "
                    f"Fetched: {failed_state.get('stats', {}).get('fetched', 0)}, "
                    f"Created: {failed_state.get('stats', {}).get('created', 0)}, "
                    f"Updated: {failed_state.get('stats', {}).get('updated', 0)}, "
                    f"Unchanged: {failed_state.get('stats', {}).get('unchanged', 0)}, "
                    f"Skipped: {failed_state.get('stats', {}).get('skipped', 0)}, "
                    f"Duplicate skipped: {failed_state.get('stats', {}).get('duplicate_skipped', 0)}, "
                    f"Missing required: {failed_state.get('stats', {}).get('missing_required_skipped', 0)}."
                ),
                "error": import_error_message,
                "completed_at": timezone.now().isoformat(),
                "updated_at": timezone.now().isoformat(),
                "logs": logs[-IMPORT_PROGRESS_LOG_LIMIT:],
                "failure_details": {
                    "samples": [
                        {
                            "reason": "Import execution failed",
                            "client_code": "-",
                            "detail": str(exc),
                        }
                    ]
                },
            }
        )
        _set_import_progress(user_id, failed_state)
        if import_run is not None:
            _finalize_import_run_record(
                import_run,
                status=ApiImportRun.STATUS_FAILED,
                completed_at=timezone.now(),
                failure_message=import_error_message,
                failure_details=failed_state.get("failure_details"),
            )
            log_api_audit(
                user,
                "fail_import",
                details=f"Import failed for endpoint '{endpoint.name}'. {import_error_message}",
                object_id=import_run.pk,
            )
            _clear_import_stop(import_run.id)
    finally:
        close_old_connections()
        _clear_manual_import_thread(import_run_id)


def _start_manual_import_worker(
    *,
    import_run_id: int,
    endpoint_id: int,
    user_id: int,
    params: dict[str, str],
    retry_source_run_id: int | None = None,
) -> None:
    worker = threading.Thread(
        target=_run_manual_import_in_background,
        kwargs={
            "import_run_id": import_run_id,
            "endpoint_id": endpoint_id,
            "user_id": user_id,
            "params": dict(params),
            "retry_source_run_id": retry_source_run_id,
        },
        name=f"scorecard-manual-import-{import_run_id}",
        daemon=True,
    )
    _register_manual_import_thread(import_run_id, worker)
    worker.start()


@login_required
def api_import_run_view(request: HttpRequest) -> JsonResponse:
    if request.method != "POST":
        return JsonResponse({"ok": False, "message": "POST method is required."}, status=405)

    if not _api_storage_ready():
        return JsonResponse(
            {"ok": False, "message": "API tables are not available yet. Run the scorecard migrations first."},
            status=400,
        )

    configuration = _active_configuration()
    endpoints = ApiEndpoint.objects.filter(is_active=True).select_related("configuration").prefetch_related("parameters").order_by("name")
    if configuration is None:
        return JsonResponse({"ok": False, "message": "Save the API settings first before importing data."}, status=400)

    importable_endpoints = endpoints.exclude(target_table="")
    form = ApiImportForm(request.POST, endpoint_queryset=importable_endpoints, parameter_definitions=[])
    endpoint_id = request.POST.get("endpoint")
    selected_endpoint = importable_endpoints.filter(pk=endpoint_id).first() if endpoint_id else None
    parameter_definitions = _endpoint_retrieval_parameters(selected_endpoint)
    form = ApiImportForm(request.POST, endpoint_queryset=importable_endpoints, parameter_definitions=parameter_definitions)
    if not form.is_valid():
        return JsonResponse(
            {
                "ok": False,
                "message": "Please correct the import form before loading data.",
                "errors": form.errors,
            },
            status=400,
        )

    endpoint = form.cleaned_data["endpoint"]
    if endpoint != selected_endpoint:
        parameter_definitions = _endpoint_retrieval_parameters(endpoint)
        form = ApiImportForm(request.POST, endpoint_queryset=importable_endpoints, parameter_definitions=parameter_definitions)
        form.is_valid()

    params = _dynamic_form_parameters(form)
    params.update(_parse_query_string(form.cleaned_data["extra_query_string"] or ""))
    return _execute_manual_import_run(request=request, endpoint=endpoint, params=params)


@login_required
def api_import_progress_view(request: HttpRequest) -> JsonResponse:
    return JsonResponse(_ensure_import_progress_for_user(request.user.id))


@login_required
def api_import_stop_view(request: HttpRequest, run_id: int) -> JsonResponse:
    if request.method != "POST":
        return JsonResponse({"ok": False, "message": "POST method is required."}, status=405)
    if not _api_import_history_ready():
        return JsonResponse({"ok": False, "message": "Import history is not available yet."}, status=400)

    import_run = get_object_or_404(ApiImportRun.objects.select_related("triggered_by", "endpoint"), pk=run_id)
    if import_run.status != ApiImportRun.STATUS_RUNNING:
        return JsonResponse({"ok": False, "message": "Only running imports can be stopped."}, status=400)
    if import_run.triggered_by_id not in (None, request.user.id):
        return JsonResponse({"ok": False, "message": "You can only stop your own import run."}, status=403)

    if _is_manual_import_thread_running(import_run.id):
        _request_import_stop(import_run.id)
        log_api_audit(
            request.user,
            "request_import_stop",
            details=f"Stop requested for running import '{import_run.endpoint.name}'.",
            object_id=import_run.pk,
        )
        return JsonResponse({"ok": True, "message": "Stop request sent. The import will stop after the current API page finishes."})

    _finalize_stopped_import_run(
        import_run,
        "Import was marked as stopped because no active background process was detected.",
    )
    log_api_audit(
        request.user,
        "mark_stale_import_stopped",
        details=f"Stale running import '{import_run.endpoint.name}' was marked as stopped.",
        object_id=import_run.pk,
    )
    return JsonResponse({"ok": True, "message": "The stale running import was marked as stopped."})


@login_required
def api_import_retry_view(request: HttpRequest, run_id: int) -> JsonResponse:
    if request.method != "POST":
        return JsonResponse({"ok": False, "message": "POST method is required."}, status=405)
    if not _api_import_history_ready():
        return JsonResponse({"ok": False, "message": "Import history is not available yet."}, status=400)

    import_run = get_object_or_404(ApiImportRun.objects.select_related("endpoint", "triggered_by"), pk=run_id)
    if import_run.status not in {ApiImportRun.STATUS_FAILED, ApiImportRun.STATUS_STOPPED}:
        return JsonResponse({"ok": False, "message": "Only failed or stopped imports can be retried."}, status=400)

    params = {
        key: str(value)
        for key, value in (import_run.parameters_used or {}).items()
        if value not in (None, "")
    }
    return _execute_manual_import_run(
        request=request,
        endpoint=import_run.endpoint,
        params=params,
        retry_source_run=import_run,
    )


def _parse_query_string(query_string: str) -> dict[str, str]:
    return dict(parse_qsl(query_string.strip(), keep_blank_values=True)) if query_string.strip() else {}


def _generate_endpoint_code(name: str, current_id: int | None = None) -> str:
    base = slugify(name).replace("-", "_").upper() or "API_ENDPOINT"
    candidate = base
    counter = 1
    queryset = ApiEndpoint.objects.all()
    if current_id is not None:
        queryset = queryset.exclude(pk=current_id)

    while queryset.filter(code=candidate).exists():
        counter += 1
        candidate = f"{base}_{counter}"
    return candidate


def _build_request_url(base_url: str, path: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def _split_path_and_query(path: str) -> tuple[str, dict[str, str]]:
    cleaned_path = (path or "").strip()
    cleaned_path = cleaned_path.lstrip("- ").strip()
    parts = urlsplit(cleaned_path)
    inline_params = dict(parse_qsl(parts.query, keep_blank_values=True))
    normalized_path = parts.path or cleaned_path
    return normalized_path, inline_params


def _execute_get_request(
    configuration: ApiConfiguration,
    endpoint_path: str,
    params: dict[str, str],
    timeout_override: int | None = None,
    timeout_context: str = "request",
    session: requests.Session | None = None,
) -> tuple[bool, int | None, Any]:
    managed_session = session is None
    request_url = ""
    try:
        normalized_path, inline_params = _split_path_and_query(endpoint_path)
        merged_params = {**inline_params, **params}
        timeout_seconds = timeout_override or configuration.timeout_seconds or DEFAULT_TIMEOUT
        active_session = session or requests.Session()
        # Ignore machine-level proxy variables for internal API hosts.
        active_session.trust_env = False
        request_url = _build_request_url(configuration.base_url, normalized_path)
        _terminal_log(
            "request.start",
            request_url=request_url,
            endpoint_path=endpoint_path,
            params=merged_params,
            timeout_seconds=timeout_seconds,
            timeout_context=timeout_context,
            headers={
                configuration.auth_header_name: configuration.auth_secret_key,
                "Accept": "application/json",
                "Connection": "close",
            },
        )
        response = active_session.get(
            request_url,
            headers={
                configuration.auth_header_name: configuration.auth_secret_key,
                "Accept": "application/json",
                "Connection": "close",
            },
            params=merged_params,
            timeout=(3, timeout_seconds),
            allow_redirects=False,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw_text": response.text}
        _terminal_log(
            "request.response",
            request_url=request_url,
            status_code=response.status_code,
            ok=response.ok,
            payload_preview=_truncate_text(_format_json(payload), 500),
        )

        if response.ok:
            return True, response.status_code, payload

        return False, response.status_code, payload
    except requests.Timeout:
        _terminal_log(
            "request.timeout",
            request_url=request_url or _build_request_url(configuration.base_url, endpoint_path),
            timeout_context=timeout_context,
            timeout_override=timeout_override,
        )
        if timeout_context == "test":
            return False, None, (
                f"Test request timed out after {timeout_seconds} seconds. "
                "The endpoint test is intentionally short; use the retrieve page for heavier pulls."
            )
        if timeout_context == "retrieve":
            return False, None, (
                f"Retrieve request timed out after {timeout_seconds} seconds. "
                "Try a smaller sample or verify that the upstream API is responding."
            )
        return False, None, (
            f"Import request timed out after {timeout_seconds} seconds. "
            "The scheduled import did not complete within the configured timeout."
        )
    except requests.RequestException as exc:
        status_code = exc.response.status_code if getattr(exc, "response", None) is not None else None
        _terminal_log(
            "request.exception",
            request_url=request_url or _build_request_url(configuration.base_url, endpoint_path),
            status_code=status_code,
            error=str(exc),
        )
        return False, status_code, str(exc)
    finally:
        if managed_session and session is None:
            active_session.close()


def _test_endpoint_connection(
    configuration: ApiConfiguration,
    endpoint: ApiEndpoint,
    selected_parameters: list[ApiConfigurationParameter] | None = None,
    override_params: dict[str, str] | None = None,
) -> tuple[bool, int | None, Any, str]:
    # Keep endpoint testing fast by requesting only the first page/sample.
    params = {
        "page": "1",
        "page_size": "100",
    }
    params.update(_configuration_parameter_defaults(configuration, selected_parameters=selected_parameters, use_for_testing=True))
    if override_params:
        params.update({key: value for key, value in override_params.items() if value not in (None, "")})
    selected_parameter_names = {
        _normalize_parameter_key(parameter.name)
        for parameter in (selected_parameters or [])
        if parameter is not None
    }
    endpoint_has_reporting_date = False
    try:
        if getattr(endpoint, "pk", None):
            endpoint_parameter_names = {
                _normalize_parameter_key(name)
                for name in endpoint.parameters.values_list("name", flat=True)
            }
            endpoint_has_reporting_date = "reporting_date" in endpoint_parameter_names
    except Exception:
        endpoint_has_reporting_date = False
    if not params.get("reporting_date") and (
        "reporting_date" in selected_parameter_names or endpoint_has_reporting_date
    ):
        params["reporting_date"] = timezone.localdate().isoformat()
    _terminal_log(
        "endpoint_test.params",
        endpoint_name=getattr(endpoint, "name", ""),
        endpoint_path=getattr(endpoint, "path", ""),
        selected_parameter_names=sorted(selected_parameter_names),
        endpoint_has_reporting_date=endpoint_has_reporting_date,
        override_params=override_params or {},
        final_params=params,
    )
    ok, status_code, payload = _execute_get_request(
        configuration,
        endpoint.path,
        params,
        timeout_override=configuration.test_timeout_seconds or TEST_REQUEST_TIMEOUT,
        timeout_context="test",
    )
    preview_text = _format_test_payload(payload, endpoint=endpoint)
    if ok:
        return True, status_code, payload, preview_text
    return False, status_code, payload, preview_text


def _truncate_text(value: Any, limit: int = 300) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _format_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, default=str)
    except TypeError:
        return str(value)


def _clean_payload_for_preview(payload: Any, endpoint: ApiEndpoint | None = None) -> Any:
    if endpoint is None:
        return payload

    if endpoint.target_table == ApiEndpoint.TARGET_CUSTOMER_CORPORATE:
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            cleaned_payload = dict(payload)
            cleaned_payload["data"] = [
                _normalize_record_for_storage(item, code_fields=CORPORATE_CODE_FIELDS)
                for item in payload["data"]
                if isinstance(item, dict)
            ]
            return cleaned_payload
        return payload

    if endpoint.target_table == ApiEndpoint.TARGET_CUSTOMER_INDIVIDUAL:
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            cleaned_payload = dict(payload)
            cleaned_payload["data"] = [
                _normalize_record_for_storage(item, code_fields=INDIVIDUAL_CODE_FIELDS)
                for item in payload["data"]
                if isinstance(item, dict)
            ]
            return cleaned_payload
        return payload

    if endpoint.target_table == ApiEndpoint.TARGET_CUSTOMER_LOAN:
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            cleaned_payload = dict(payload)
            cleaned_payload["data"] = [
                _normalize_loan_record_for_storage(item)
                for item in payload["data"]
                if isinstance(item, dict)
            ]
            return cleaned_payload
        return payload

    if endpoint.target_table == ApiEndpoint.TARGET_CUSTOMER_OVERDRAFT:
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            cleaned_payload = dict(payload)
            cleaned_payload["data"] = [
                {
                    "reporting_date": _clean_str(item.get("reporting_date")),
                    "customer_code": _normalize_customer_code(item.get("customer_code")),
                    "customer_name": _normalize_upper_text(item.get("customer_name")),
                    "branch_description": _normalize_branch_description(item.get("branch_description")),
                    "ac_category": _normalize_code_value(item.get("ac_category")),
                    "account_number": _normalize_code_value(item.get("account_number")),
                }
                for item in payload["data"]
                if isinstance(item, dict)
            ]
            return cleaned_payload
        return payload

    return payload


def _normalize_parameter_key(name: Any) -> str:
    text = str(name or "").strip().lower()
    if not text:
        return ""
    normalized = "_".join(text.replace("-", " ").split())
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized


def _format_test_payload(payload: Any, endpoint: ApiEndpoint | None = None) -> str:
    payload = _clean_payload_for_preview(payload, endpoint=endpoint)
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error_description") or payload.get("detail")
        error = payload.get("error")
        if message and error:
            return f"{error}: {message}\n\n" + json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        if message:
            return f"{message}\n\n" + json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    if isinstance(payload, (dict, list)):
        try:
            return json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        except TypeError:
            return str(payload)
    return str(payload)


def _configuration_parameter_defaults(
    configuration: ApiConfiguration,
    *,
    selected_parameters: list[ApiConfigurationParameter] | None = None,
    use_for_testing: bool = False,
    use_for_retrieval: bool = False,
) -> dict[str, str]:
    queryset = ApiConfigurationParameter.objects.filter(configuration=configuration, is_active=True)
    if selected_parameters is not None:
        queryset = queryset.filter(pk__in=[parameter.pk for parameter in selected_parameters])
    if use_for_testing:
        queryset = queryset.filter(use_for_testing=True)
    if use_for_retrieval:
        queryset = queryset.filter(use_for_retrieval=True)
    return {
        _normalize_parameter_key(parameter.name): parameter.default_value
        for parameter in queryset
        if _normalize_parameter_key(parameter.name) and parameter.default_value not in ("", None)
    }


def _endpoint_retrieval_parameters(endpoint: ApiEndpoint | None) -> list[ApiConfigurationParameter]:
    if endpoint is None:
        return []
    return list(
        endpoint.parameters.filter(
            is_active=True,
            use_for_retrieval=True,
        ).order_by("display_order", "name")
    )


def _create_import_run_record(
    *,
    endpoint: ApiEndpoint,
    run_source: str,
    parameters_used: dict[str, str],
    schedule: ApiImportSchedule | None = None,
    triggered_by: Any = None,
) -> ApiImportRun | None:
    if not _api_import_history_ready():
        return None

    return ApiImportRun.objects.create(
        endpoint=endpoint,
        schedule=schedule,
        run_source=run_source,
        triggered_by=triggered_by,
        target_table=endpoint.get_target_table_display(),
        status=ApiImportRun.STATUS_RUNNING,
        started_at=timezone.now(),
        parameters_used=parameters_used,
    )


def _finalize_import_run_record(
    import_run: ApiImportRun | None,
    *,
    status: str,
    stats: SyncStats | None = None,
    completed_at: datetime | None = None,
    duration_seconds: float | None = None,
    failure_message: str = "",
    failure_details: dict[str, Any] | None = None,
) -> None:
    if import_run is None:
        return

    stats = stats or SyncStats()
    import_run.status = status
    import_run.completed_at = completed_at or timezone.now()
    import_run.duration_seconds = duration_seconds
    import_run.fetched = stats.fetched
    import_run.created = stats.created
    import_run.updated = stats.updated
    import_run.unchanged = stats.unchanged
    import_run.skipped = stats.skipped
    import_run.duplicate_skipped = stats.duplicate_skipped
    import_run.missing_required_skipped = stats.missing_required_skipped
    import_run.failure_message = failure_message
    import_run.failure_details = failure_details or _serialize_failure_details(stats)
    import_run.save()
    notify_api_import_result(import_run)


def _schedule_reporting_date_value(schedule: ApiImportSchedule, reference_date: date | None = None) -> str | None:
    active_date = reference_date or timezone.localdate()
    if schedule.reporting_date_mode == ApiImportSchedule.REPORTING_DATE_RUN_DATE:
        return active_date.isoformat()
    if schedule.reporting_date_mode == ApiImportSchedule.REPORTING_DATE_PREVIOUS_DAY:
        return (active_date - timedelta(days=1)).isoformat()
    if schedule.reporting_date_mode == ApiImportSchedule.REPORTING_DATE_FIXED and schedule.fixed_reporting_date:
        return schedule.fixed_reporting_date.isoformat()
    return None


def _build_schedule_params(schedule: ApiImportSchedule, reference_date: date | None = None) -> dict[str, str]:
    endpoint = schedule.endpoint
    params = _configuration_parameter_defaults(
        endpoint.configuration,
        selected_parameters=list(endpoint.parameters.all()),
        use_for_retrieval=True,
    )
    reporting_date_value = _schedule_reporting_date_value(schedule, reference_date=reference_date)
    endpoint_parameter_names = {
        _normalize_parameter_key(name)
        for name in endpoint.parameters.values_list("name", flat=True)
    }
    if reporting_date_value and "reporting_date" in endpoint_parameter_names:
        params["reporting_date"] = reporting_date_value
    params.update(_parse_query_string(schedule.extra_query_string or ""))
    return params


def execute_import_schedule(
    schedule: ApiImportSchedule,
    *,
    triggered_by: Any = None,
) -> tuple[SyncStats, datetime, float]:
    lock_key = f"scorecard:api-schedule-running:{schedule.pk}"
    lock_timeout = max(
        int(getattr(schedule.endpoint.configuration, "timeout_seconds", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT) * 10,
        600,
    )
    if not cache.add(lock_key, timezone.now().isoformat(), timeout=lock_timeout):
        raise ScheduleAlreadyRunningError(
            f"Schedule '{schedule.name}' is already running. A second overlapping run was not started."
        )

    import_run = None
    try:
        params = _build_schedule_params(schedule)
        import_run = _create_import_run_record(
            endpoint=schedule.endpoint,
            run_source=ApiImportRun.SOURCE_SCHEDULE,
            parameters_used=params,
            schedule=schedule,
            triggered_by=triggered_by,
        )
        stats, completed_at, duration_seconds = _run_endpoint_import(schedule.endpoint, params)
    except Exception as exc:
        _finalize_import_run_record(
            import_run,
            status=ApiImportRun.STATUS_FAILED,
            completed_at=timezone.now(),
            failure_message=str(exc),
        )
        raise
    else:
        schedule.last_run_at = completed_at
        schedule.last_status = "success"
        schedule.last_message = (
            f"Imported {stats.fetched} rows. Created {stats.created}, updated {stats.updated}, "
            f"unchanged {stats.unchanged}, skipped {stats.skipped}."
        )
        schedule.last_duration_seconds = duration_seconds
        schedule.save(update_fields=["last_run_at", "last_status", "last_message", "last_duration_seconds", "updated_at"])
        _finalize_import_run_record(
            import_run,
            status=ApiImportRun.STATUS_SUCCESS,
            stats=stats,
            completed_at=completed_at,
            duration_seconds=duration_seconds,
        )
        return stats, completed_at, duration_seconds
    finally:
        cache.delete(lock_key)


def run_due_import_schedules(now: datetime | None = None) -> list[dict[str, Any]]:
    current = now or timezone.now()
    due_schedules = [
        schedule
        for schedule in ApiImportSchedule.objects.select_related("endpoint", "endpoint__configuration").filter(is_active=True)
        if _is_schedule_due(schedule, now=current)
    ]

    results: list[dict[str, Any]] = []
    for schedule in due_schedules:
        try:
            stats, completed_at, duration_seconds = execute_import_schedule(schedule)
            results.append(
                {
                    "schedule": schedule,
                    "status": "success",
                    "stats": stats,
                    "completed_at": completed_at,
                    "duration_seconds": duration_seconds,
                }
            )
        except ScheduleAlreadyRunningError:
            continue
        except Exception as exc:
            schedule.last_run_at = timezone.now()
            schedule.last_status = "failed"
            schedule.last_message = str(exc)
            schedule.last_duration_seconds = None
            schedule.save(update_fields=["last_run_at", "last_status", "last_message", "last_duration_seconds", "updated_at"])
            notify_api_schedule_failure(schedule, str(exc))
            results.append(
                {
                    "schedule": schedule,
                    "status": "failed",
                    "error": str(exc),
                }
            )
    return results


def _dynamic_form_parameters(form: ApiRetrieveForm | ApiImportForm) -> dict[str, str]:
    params: dict[str, str] = {}
    for field_name, parameter in form.parameter_fields:
        value = form.cleaned_data.get(field_name)
        if value not in (None, ""):
            normalized_name = _normalize_parameter_key(parameter.name)
            if normalized_name:
                params[normalized_name] = str(value)
    return params


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _normalize_blank_text(value: Any) -> str | None:
    cleaned_value = _clean_str(value)
    if cleaned_value is None:
        return None
    if cleaned_value.upper() in {"NULL", "NONE", "N/A", "NA"}:
        return None
    return cleaned_value


def _normalize_whitespace(value: Any) -> str | None:
    cleaned_value = _normalize_blank_text(value)
    if cleaned_value is None:
        return None
    normalized_value = " ".join(cleaned_value.split())
    return normalized_value or None


def _normalize_upper_text(value: Any) -> str | None:
    normalized_value = _normalize_whitespace(value)
    if normalized_value is None:
        return None
    return normalized_value.upper()


def _normalize_code_value(value: Any) -> str | None:
    cleaned_value = _normalize_blank_text(value)
    if cleaned_value is None:
        return None
    normalized_value = "".join(str(cleaned_value).replace("-", " ").split())
    if not normalized_value:
        return None
    return normalized_value.upper()


def _normalize_customer_code(value: Any) -> str | None:
    cleaned_value = _normalize_code_value(value)
    if cleaned_value is None:
        return None
    stripped_value = cleaned_value.lstrip("0")
    return stripped_value or "0"


def _normalize_branch_description(value: Any) -> str | None:
    return _normalize_upper_text(value)


CORPORATE_CODE_FIELDS = {
    "client_code",
    "swift_code",
    "industry_code",
    "sub_industry_code",
    "investment_currency",
    "capital_currency",
    "incorporation_country",
    "registration_number",
    "import_export_code",
    "commercial_business_identifier",
    "business_entity_identifier",
    "country_code",
    "connected_person_investment_number",
    "bank_code",
    "weaker_section_code",
    "branch_code",
}


INDIVIDUAL_CODE_FIELDS = {
    "client_code",
    "work_sector_code",
    "birth_place_code",
    "religion_code",
    "nationality_code",
    "language_code",
    "phone_home",
    "phone_office",
    "phone_office_alt",
    "extension_number",
    "mobile_number",
    "fax_number",
    "employee_number",
    "occupation_code",
    "employer_code",
    "designation_code",
    "pid_inv_number",
    "employer_reference_code",
    "application_number",
    "branch_code",
}


def _normalize_record_for_storage(record: dict[str, Any], *, code_fields: set[str]) -> dict[str, Any]:
    normalized_record: dict[str, Any] = {}
    for key, value in record.items():
        if not isinstance(value, str):
            normalized_record[key] = value
            continue
        if "date" in key:
            normalized_record[key] = _clean_str(value)
            continue
        if key in code_fields:
            normalized_record[key] = _normalize_code_value(value)
            continue
        normalized_record[key] = _normalize_upper_text(value)
    return normalized_record


IMPORT_EMPTY_VALUE_TOKENS = {"", "-", "--", "NULL", "NONE", "N/A", "NA", "NAN", "NOT AVAILABLE"}


def _is_empty_import_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().upper() in IMPORT_EMPTY_VALUE_TOKENS
    return False


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    value = _clean_str(value)
    if not value or value.upper() in IMPORT_EMPTY_VALUE_TOKENS:
        return None
    normalized_value = value.replace("T", " ").split(".")[0].strip()
    for date_format in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d-%m-%Y",
        "%m-%d-%Y",
    ):
        try:
            return datetime.strptime(normalized_value, date_format).date()
        except ValueError:
            continue
    return None


def _decimal_from_import_value(value: Any) -> Decimal | None:
    if _is_empty_import_value(value):
        return None
    if isinstance(value, Decimal):
        parsed = value
    else:
        text_value = str(value).strip().replace("\xa0", "")
        if text_value.upper() in IMPORT_EMPTY_VALUE_TOKENS:
            return None
        negative_parentheses = text_value.startswith("(") and text_value.endswith(")")
        text_value = text_value.strip("()").replace(",", "").replace(" ", "").replace("%", "")
        text_value = re.sub(r"[^0-9eE+\-.]", "", text_value)
        if text_value in {"", "+", "-", ".", "+.", "-."}:
            return None
        try:
            parsed = Decimal(text_value)
        except (InvalidOperation, TypeError, ValueError):
            return None
        if negative_parentheses:
            parsed = -abs(parsed)
    if not parsed.is_finite():
        return None
    return parsed


def _parse_decimal(
    value: Any,
    *,
    max_digits: int | None = None,
    decimal_places: int | None = None,
) -> Decimal | None:
    parsed = _decimal_from_import_value(value)
    if parsed is None:
        return None

    if decimal_places is not None:
        quantizer = Decimal("1").scaleb(-decimal_places)
        try:
            parsed = parsed.quantize(quantizer, rounding=ROUND_HALF_UP)
        except InvalidOperation:
            return None

    if max_digits is None:
        return parsed

    try:
        sign, digits, exponent = parsed.as_tuple()
    except (InvalidOperation, ValueError):
        return None
    if exponent >= 0:
        decimals = 0
        integer_digits = len(digits) + exponent
    else:
        decimals = -exponent
        integer_digits = max(0, len(digits) - decimals)

    allowed_integer_digits = max_digits - (decimal_places or decimals)
    total_digits = integer_digits + decimals
    if integer_digits > allowed_integer_digits or total_digits > max_digits:
        return None
    return parsed


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    parsed_date = _parse_date(value)
    if parsed_date is None:
        return None
    return datetime.combine(parsed_date, time.min)


def _parse_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if _is_empty_import_value(value):
        return None
    normalized_value = str(value).strip().upper()
    if normalized_value in {"1", "Y", "YES", "TRUE", "T"}:
        return True
    if normalized_value in {"0", "N", "NO", "FALSE", "F"}:
        return False
    return None


def _coerce_model_field_value(value: Any, field: Any) -> Any:
    if isinstance(field, django_models.JSONField):
        return value
    if isinstance(field, django_models.DateTimeField):
        return _parse_datetime(value)
    if isinstance(field, django_models.DateField):
        return _parse_date(value)
    if isinstance(field, django_models.DecimalField):
        return _parse_decimal(value, max_digits=field.max_digits, decimal_places=field.decimal_places)
    if isinstance(field, django_models.IntegerField):
        return _parse_integer(value)
    if isinstance(field, django_models.BooleanField):
        return _parse_boolean(value)
    if isinstance(field, (django_models.CharField, django_models.TextField)):
        cleaned_value = _normalize_whitespace(value)
        if cleaned_value is None:
            return None if getattr(field, "null", False) else ""
        max_length = getattr(field, "max_length", None)
        if max_length and len(cleaned_value) > max_length:
            return cleaned_value[:max_length]
        return cleaned_value
    return value


def _sanitize_model_defaults(model: Any, defaults: dict[str, Any]) -> dict[str, Any]:
    fields_by_name = {field.name: field for field in model._meta.concrete_fields}
    sanitized_defaults: dict[str, Any] = {}
    for field_name, value in defaults.items():
        field = fields_by_name.get(field_name)
        if field is None:
            sanitized_defaults[field_name] = value
            continue
        sanitized_defaults[field_name] = _coerce_model_field_value(value, field)
    return sanitized_defaults


def _coalesce(*values: Any) -> str | None:
    for value in values:
        cleaned = _clean_str(value)
        if cleaned:
            return cleaned
    return None


def _build_individual_name(record: dict[str, Any]) -> str:
    parts = [
        _normalize_upper_text(record.get("first_name")),
        _normalize_upper_text(record.get("middle_name")),
        _normalize_upper_text(record.get("surname")) or _normalize_upper_text(record.get("last_name")),
    ]
    display = " ".join(part for part in parts if part)
    return display or _normalize_code_value(record.get("client_code")) or "UNKNOWN INDIVIDUAL"


def _normalize_customer_type(value: Any) -> str | None:
    normalized_value = _normalize_upper_text(value)
    if normalized_value == "CORPORATE":
        return "CORPORATE"
    if normalized_value == "INDIVIDUAL":
        return "INDIVIDUAL"
    if normalized_value == "BORROWER":
        return "BORROWER"
    return normalized_value


def _headers(api_key: str) -> dict[str, str]:
    return {
        "X-API-KEY": api_key,
        "Accept": "application/json",
    }


def fetch_paginated_clients(
    base_url: str,
    api_key: str,
    endpoint_path: str,
    reporting_date: str,
    page_size: int = 1000,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """
    Fetch all pages from an ECL client endpoint.
    """
    session = requests.Session()
    session.headers.update(_headers(api_key))

    page = 1
    all_records: list[dict[str, Any]] = []

    while True:
        response = session.get(
            f"{base_url.rstrip('/')}/{endpoint_path.lstrip('/')}",
            params={
                "reporting_date": reporting_date,
                "page": page,
                "page_size": page_size,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        page_records = payload.get("data", [])

        if not isinstance(page_records, list):
            raise ValueError(f"Unexpected API response shape for {endpoint_path}: 'data' is not a list")

        all_records.extend(page_records)

        if len(page_records) < page_size:
            break
        page += 1

    return all_records


def _fetch_endpoint_preview(
    configuration: ApiConfiguration,
    endpoint_path: str,
    params: dict[str, str],
    preview_size: int = 20,
) -> Any:
    preview_params = {**params}
    preview_params.setdefault("page", "1")
    preview_params["page_size"] = str(preview_size)
    ok, status_code, payload = _execute_get_request(
        configuration,
        endpoint_path,
        preview_params,
        timeout_context="retrieve",
    )
    if not ok:
        raise ValueError(_format_test_payload(payload))
    return payload


def _fetch_all_endpoint_records(
    configuration: ApiConfiguration,
    endpoint_path: str,
    params: dict[str, str],
) -> list[dict[str, Any]]:
    all_records: list[dict[str, Any]] = []
    for _, page_records in _iter_endpoint_record_pages(configuration, endpoint_path, params):
        all_records.extend(page_records)
    return all_records


def _iter_endpoint_record_pages(
    configuration: ApiConfiguration,
    endpoint_path: str,
    params: dict[str, str],
    *,
    should_stop: Callable[[], bool] | None = None,
):
    merged_params = {**params}
    page_size = int(merged_params.pop("page_size", "1000") or "1000")
    page = int(merged_params.pop("page", "1") or "1")

    with requests.Session() as session:
        session.trust_env = False
        while True:
            if should_stop is not None and should_stop():
                raise ImportStoppedError("Import was stopped by the user.")
            page_params = {**merged_params, "page": str(page), "page_size": str(page_size)}
            ok, status_code, payload = _execute_get_request(
                configuration,
                endpoint_path,
                page_params,
                timeout_context="import",
                session=session,
            )
            if not ok:
                raise ValueError(_format_test_payload(payload))

            page_records = payload.get("data", []) if isinstance(payload, dict) else payload
            if not isinstance(page_records, list):
                raise ValueError("Unexpected API response shape: expected a list of records in 'data'.")

            yield page, page_records
            if len(page_records) < page_size:
                break
            page += 1


def _run_endpoint_import(
    endpoint: ApiEndpoint,
    params: dict[str, str],
    *,
    progress_callback: Callable[[int, SyncStats, SyncStats], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[SyncStats, datetime, float]:
    started_at = perf_counter()
    total_stats = SyncStats()

    for page_number, page_records in _iter_endpoint_record_pages(
        endpoint.configuration,
        endpoint.path,
        params,
        should_stop=should_stop,
    ):
        if should_stop is not None and should_stop():
            raise ImportStoppedError("Import was stopped by the user.")
        page_stats = _import_endpoint_records(endpoint, page_records)
        total_stats.add(page_stats)
        if progress_callback is not None:
            progress_callback(page_number, page_stats, total_stats)

    return total_stats, timezone.now(), perf_counter() - started_at


def _chunked(items: list[Any], size: int = BULK_SYNC_BATCH_SIZE) -> list[list[Any]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def _deduplicate_entries(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
    deduplicated: dict[str, dict[str, Any]] = {}
    duplicate_count = 0
    duplicate_samples: list[dict[str, Any]] = []

    for entry in entries:
        client_code = entry["client_code"]
        if client_code in deduplicated:
            duplicate_count += 1
            if len(duplicate_samples) < FAILURE_SAMPLE_LIMIT:
                duplicate_samples.append(
                    {
                        "reason": "Duplicate client code from API",
                        "client_code": client_code,
                        "detail": "The source API returned the same client code more than once in this import.",
                    }
                )
        deduplicated[client_code] = entry

    return list(deduplicated.values()), duplicate_count, duplicate_samples


def _build_corporate_detail_defaults(
    record: dict[str, Any],
    synced_at: datetime,
) -> dict[str, Any]:
    cleaned_record = _normalize_record_for_storage(record, code_fields=CORPORATE_CODE_FIELDS)
    return {
        "client_code": cleaned_record.get("client_code"),
        "client_name": cleaned_record.get("client_name"),
        "resident_status": cleaned_record.get("resident_status"),
        "organization_qualifier": cleaned_record.get("organization_qualifier"),
        "swift_code": cleaned_record.get("swift_code"),
        "industry_code": cleaned_record.get("industry_code"),
        "sub_industry_code": cleaned_record.get("sub_industry_code"),
        "nature_of_business_1": cleaned_record.get("nature_of_business_1"),
        "nature_of_business_2": cleaned_record.get("nature_of_business_2"),
        "nature_of_business_3": cleaned_record.get("nature_of_business_3"),
        "investment_currency": cleaned_record.get("investment_currency"),
        "investment_amount": _parse_decimal(record.get("investment_amount")),
        "capital_currency": cleaned_record.get("capital_currency"),
        "authorized_capital": _parse_decimal(record.get("authorized_capital")),
        "issued_capital": _parse_decimal(record.get("issued_capital")),
        "paid_up_capital": _parse_decimal(record.get("paid_up_capital")),
        "net_worth_amount": _parse_decimal(record.get("net_worth_amount")),
        "incorporation_date": _parse_date(cleaned_record.get("incorporation_date")),
        "incorporation_country": cleaned_record.get("incorporation_country"),
        "registration_number": cleaned_record.get("registration_number"),
        "registration_date": _parse_date(cleaned_record.get("registration_date")),
        "registration_authority": cleaned_record.get("registration_authority"),
        "registration_expiry_date": _parse_date(cleaned_record.get("registration_expiry_date")),
        "registered_office_address_1": cleaned_record.get("registered_office_address_1"),
        "registered_office_address_2": cleaned_record.get("registered_office_address_2"),
        "registered_office_address_3": cleaned_record.get("registered_office_address_3"),
        "registered_office_address_4": cleaned_record.get("registered_office_address_4"),
        "registered_office_address_5": cleaned_record.get("registered_office_address_5"),
        "is_trade_finance_client": cleaned_record.get("is_trade_finance_client"),
        "vostro_exchange_house": cleaned_record.get("vostro_exchange_house"),
        "import_export_code": cleaned_record.get("import_export_code"),
        "commercial_business_identifier": cleaned_record.get("commercial_business_identifier"),
        "business_entity_identifier": cleaned_record.get("business_entity_identifier"),
        "years_in_business": record.get("years_in_business"),
        "gross_turnover": _parse_decimal(record.get("gross_turnover")),
        "employee_size": record.get("employee_size"),
        "number_of_offices": record.get("number_of_offices"),
        "is_scheduled_bank": cleaned_record.get("is_scheduled_bank"),
        "is_sovereign": cleaned_record.get("is_sovereign"),
        "sovereign_type": cleaned_record.get("sovereign_type"),
        "country_code": cleaned_record.get("country_code"),
        "is_central_state": cleaned_record.get("is_central_state"),
        "is_public_sector": cleaned_record.get("is_public_sector"),
        "is_primary_dealer": cleaned_record.get("is_primary_dealer"),
        "is_multilateral_bank": cleaned_record.get("is_multilateral_bank"),
        "connected_person_investment_number": cleaned_record.get("connected_person_investment_number"),
        "bank_type": cleaned_record.get("bank_type"),
        "cooperative_bank_type": cleaned_record.get("cooperative_bank_type"),
        "bank_code": cleaned_record.get("bank_code"),
        "weaker_section_code": cleaned_record.get("weaker_section_code"),
        "source_of_funds": cleaned_record.get("source_of_funds"),
        "purpose_of_account_opening": cleaned_record.get("purpose_of_account_opening"),
        "branch_code": cleaned_record.get("branch_code"),
        "branch_name": _normalize_branch_description(cleaned_record.get("branch_name")),
        "raw_payload": cleaned_record,
        "source_last_sync_at": synced_at,
    }


def _build_individual_detail_defaults(
    record: dict[str, Any],
    synced_at: datetime,
) -> dict[str, Any]:
    cleaned_record = _normalize_record_for_storage(record, code_fields=INDIVIDUAL_CODE_FIELDS)
    return {
        "client_code": cleaned_record.get("client_code"),
        "first_name": cleaned_record.get("first_name"),
        "last_name": cleaned_record.get("last_name"),
        "surname": cleaned_record.get("surname"),
        "middle_name": cleaned_record.get("middle_name"),
        "work_sector_code": cleaned_record.get("work_sector_code"),
        "father_name": cleaned_record.get("father_name"),
        "birth_date": _parse_date(cleaned_record.get("birth_date")),
        "birth_place_code": cleaned_record.get("birth_place_code"),
        "birth_place_name": cleaned_record.get("birth_place_name"),
        "gender": cleaned_record.get("gender"),
        "marital_status": cleaned_record.get("marital_status"),
        "religion_code": cleaned_record.get("religion_code"),
        "nationality_code": cleaned_record.get("nationality_code"),
        "resident_status": cleaned_record.get("resident_status"),
        "language_code": cleaned_record.get("language_code"),
        "is_illiterate": cleaned_record.get("is_illiterate"),
        "is_disabled": cleaned_record.get("is_disabled"),
        "fax_address_required": cleaned_record.get("fax_address_required"),
        "phone_home": cleaned_record.get("phone_home"),
        "phone_office": cleaned_record.get("phone_office"),
        "phone_office_alt": cleaned_record.get("phone_office_alt"),
        "extension_number": cleaned_record.get("extension_number"),
        "mobile_number": cleaned_record.get("mobile_number"),
        "fax_number": cleaned_record.get("fax_number"),
        "email_primary": cleaned_record.get("email_primary"),
        "email_secondary": cleaned_record.get("email_secondary"),
        "employment_type": cleaned_record.get("employment_type"),
        "pension_flag": cleaned_record.get("pension_flag"),
        "bank_relationship_flag": cleaned_record.get("bank_relationship_flag"),
        "employee_number": cleaned_record.get("employee_number"),
        "occupation_code": cleaned_record.get("occupation_code"),
        "employer_code": cleaned_record.get("employer_code"),
        "employer_name": cleaned_record.get("employer_name"),
        "employer_address_1": cleaned_record.get("employer_address_1"),
        "employer_address_2": cleaned_record.get("employer_address_2"),
        "employer_address_3": cleaned_record.get("employer_address_3"),
        "employer_address_4": cleaned_record.get("employer_address_4"),
        "employer_address_5": cleaned_record.get("employer_address_5"),
        "designation_code": cleaned_record.get("designation_code"),
        "annual_income": _parse_decimal(
            record.get("annual_income"),
            max_digits=22,
            decimal_places=3,
        ),
        "income_slab": cleaned_record.get("income_slab"),
        "accommodation_type": cleaned_record.get("accommodation_type"),
        "accommodation_other": cleaned_record.get("accommodation_other"),
        "owns_two_wheeler": cleaned_record.get("owns_two_wheeler"),
        "owns_car": cleaned_record.get("owns_car"),
        "insurance_info": cleaned_record.get("insurance_info"),
        "pid_inv_number": cleaned_record.get("pid_inv_number"),
        "poverty_flag": cleaned_record.get("poverty_flag"),
        "employer_reference_code": cleaned_record.get("employer_reference_code"),
        "application_number": cleaned_record.get("application_number"),
        "account_purpose": cleaned_record.get("account_purpose"),
        "source_of_funds": cleaned_record.get("source_of_funds"),
        "branch_code": cleaned_record.get("branch_code"),
        "branch_name": _normalize_branch_description(cleaned_record.get("branch_name")),
        "raw_payload": cleaned_record,
        "source_last_sync_at": synced_at,
    }


def _bulk_upsert_detail_records(
    *,
    model: Any,
    entries: list[dict[str, Any]],
    defaults_builder: Any,
    unique_field: str = "client_code",
    entry_key: str = "client_code",
    row_error_handler: Callable[[Any, Exception], None] | None = None,
) -> tuple[int, int, int]:
    if not entries:
        return 0, 0, 0

    def report_row_error(row: Any, exc: Exception) -> None:
        if row_error_handler is not None:
            row_error_handler(row, exc)

    existing_records = model.objects.in_bulk(
        [entry[entry_key] for entry in entries],
        field_name=unique_field,
    )
    records_to_create: list[Any] = []
    records_to_update: list[Any] = []
    unchanged_count = 0
    synced_at = timezone.now()
    sample_defaults = _sanitize_model_defaults(model, defaults_builder(entries[0]["record"], synced_at))
    update_fields = [field for field in sample_defaults.keys() if field != unique_field]
    compare_fields = [field for field in update_fields if field != "source_last_sync_at"]

    for entry in entries:
        defaults = _sanitize_model_defaults(model, defaults_builder(entry["record"], synced_at))
        existing = existing_records.get(entry[entry_key])
        if existing is None:
            records_to_create.append(model(**defaults))
            continue

        has_changes = False
        for field_name in compare_fields:
            current_value = getattr(existing, field_name)
            new_value = defaults[field_name]
            if current_value != new_value:
                has_changes = True
                setattr(existing, field_name, new_value)
        if not has_changes:
            unchanged_count += 1
            continue
        existing.source_last_sync_at = defaults["source_last_sync_at"]

        records_to_update.append(existing)

    created_count = 0
    updated_count = 0

    for chunk in _chunked(records_to_create):
        try:
            with transaction.atomic():
                model.objects.bulk_create(chunk, batch_size=BULK_SYNC_BATCH_SIZE)
            created_count += len(chunk)
            continue
        except (IntegrityError, DataError, ProgrammingError, OperationalError):
            pass

        for row in chunk:
            lookup_value = getattr(row, unique_field)
            existing = model.objects.filter(**{unique_field: lookup_value}).first()
            if existing is None:
                try:
                    with transaction.atomic():
                        row.save(force_insert=True)
                    created_count += 1
                    continue
                except IntegrityError:
                    existing = model.objects.filter(**{unique_field: lookup_value}).first()
                    if existing is None:
                        report_row_error(row, IntegrityError("Duplicate record could not be resolved during import."))
                        continue
                except (DataError, ProgrammingError, OperationalError) as exc:
                    report_row_error(row, exc)
                    continue

            has_changes = False
            for field_name in compare_fields:
                current_value = getattr(existing, field_name)
                new_value = getattr(row, field_name)
                if current_value != new_value:
                    setattr(existing, field_name, new_value)
                    has_changes = True
            if not has_changes:
                unchanged_count += 1
                continue
            existing.source_last_sync_at = row.source_last_sync_at
            try:
                with transaction.atomic():
                    existing.save(update_fields=update_fields)
                updated_count += 1
            except (DataError, ProgrammingError, OperationalError) as exc:
                report_row_error(row, exc)

    for chunk in _chunked(records_to_update):
        try:
            with transaction.atomic():
                model.objects.bulk_update(chunk, update_fields, batch_size=BULK_SYNC_BATCH_SIZE)
            updated_count += len(chunk)
        except (IntegrityError, DataError, ProgrammingError, OperationalError):
            for row in chunk:
                try:
                    with transaction.atomic():
                        row.save(update_fields=update_fields)
                    updated_count += 1
                except (IntegrityError, DataError, ProgrammingError, OperationalError) as exc:
                    report_row_error(row, exc)

    return created_count, updated_count, unchanged_count


def _reporting_date_from_records(records: list[dict[str, Any]]) -> date | None:
    for record in records:
        parsed_date = _parse_date(record.get("reporting_date"))
        if parsed_date:
            return parsed_date
    return None


def _successful_import_runs_for_reporting_date(reporting_date: date) -> dict[str, ApiImportRun]:
    reporting_date_value = reporting_date.isoformat()
    source_slots = _main_sync_source_slots()
    runs = (
        ApiImportRun.objects.filter(status=ApiImportRun.STATUS_SUCCESS)
        .select_related("endpoint")
        .order_by("-completed_at", "-id")
    )
    latest_by_target: dict[str, ApiImportRun] = {}
    for run in runs:
        parameters_used = run.parameters_used or {}
        if parameters_used.get("reporting_date") != reporting_date_value:
            continue
        target_table = run.endpoint.target_table
        for slot in source_slots:
            if slot["key"] in latest_by_target:
                continue
            configured_endpoint = slot["endpoint"]
            if configured_endpoint is not None:
                if run.endpoint_id == configured_endpoint.id:
                    latest_by_target[slot["key"]] = run
            elif target_table == slot["target_table"]:
                latest_by_target[slot["key"]] = run
        if len(latest_by_target) == len(source_slots):
            break
    return latest_by_target


def _main_sync_delay_minutes() -> int:
    configuration = _main_sync_configuration()
    if configuration is None:
        return 30
    return max(0, configuration.delay_minutes or 0)


def _main_customer_sync_snapshot(reporting_date: date) -> dict[str, Any]:
    cache_key = _main_sync_snapshot_cache_key(reporting_date)
    cached_snapshot = cache.get(cache_key)
    if cached_snapshot is not None:
        return cached_snapshot

    source_slots = _main_sync_source_slots()
    if not _main_customer_table_ready():
        snapshot = {
            "reporting_date": reporting_date,
            "source_runs": {
                slot["key"]: {
                    "available": False,
                    "label": slot["label"],
                    "fetched": 0,
                    "loaded": 0,
                    "completed_at": "-",
                    "endpoint_name": "-",
                }
                for slot in source_slots
            },
            "missing_targets": [slot["label"] for slot in source_slots],
            "ready": False,
            "synced": False,
            "status": "waiting",
            "status_label": "Waiting For Sources",
            "detail": "Main customer table is not available yet. Run the latest scorecard migrations.",
            "main_customer_count": 0,
            "last_synced_at": "-",
        }
        cache.set(cache_key, snapshot, MAIN_SYNC_SNAPSHOT_CACHE_TTL_SECONDS)
        return snapshot

    latest_runs = _successful_import_runs_for_reporting_date(reporting_date)
    source_runs: dict[str, dict[str, Any]] = {}
    missing_targets: list[str] = []
    latest_source_completed_at: datetime | None = None

    for slot in source_slots:
        run = latest_runs.get(slot["key"])
        if run is None or run.fetched <= 0:
            missing_targets.append(slot["label"])
            source_runs[slot["key"]] = {
                "available": False,
                "label": slot["label"],
                "fetched": 0,
                "loaded": 0,
                "completed_at": "-",
                "endpoint_name": run.endpoint.name if run else "-",
            }
            continue
        if run.completed_at and (latest_source_completed_at is None or run.completed_at > latest_source_completed_at):
            latest_source_completed_at = run.completed_at
        source_runs[slot["key"]] = {
            "available": True,
            "label": slot["label"],
            "fetched": run.fetched,
            "loaded": run.created + run.updated + run.unchanged,
            "completed_at": _format_datetime_display(run.completed_at),
            "endpoint_name": run.endpoint.name,
        }

    main_customer_stats = MainCustomer.objects.filter(reporting_date=reporting_date).aggregate(
        last_synced_at=Max("last_synced_at"),
        row_count=Count("pk"),
    )
    row_count = main_customer_stats.get("row_count") or 0
    last_synced_at = main_customer_stats.get("last_synced_at")
    ready = not missing_targets
    configuration = _main_sync_configuration()
    auto_sync_active = bool(configuration and configuration.is_active)
    delay_minutes = _main_sync_delay_minutes()
    sync_due_at = (
        latest_source_completed_at + timedelta(minutes=delay_minutes)
        if ready and latest_source_completed_at is not None
        else None
    )
    synced = (
        ready
        and row_count > 0
        and last_synced_at is not None
        and latest_source_completed_at is not None
        and last_synced_at >= latest_source_completed_at
    )
    countdown_seconds = None
    if ready and sync_due_at is not None and not synced:
        countdown_seconds = max(0, int((sync_due_at - timezone.now()).total_seconds()))

    if synced:
        status = "synced"
        status_label = "Synced"
        detail = "All required source imports were available and the main customer table was updated."
    elif ready and not auto_sync_active:
        status = "paused"
        status_label = "Auto Sync Off"
        detail = "All required source imports are ready, but automatic main customer sync is currently turned off."
    elif ready and countdown_seconds is not None and countdown_seconds > 0:
        status = "countdown"
        status_label = "Countdown Running"
        detail = (
            f"All required source imports are ready. Auto sync will run after "
            f"{delay_minutes} minute(s)."
        )
    elif ready and auto_sync_active:
        status = "awaiting_scheduler"
        status_label = "Awaiting Scheduler"
        detail = "All required source imports are available and the delay has finished. The Django scheduler service should pick up this sync on its next check."
    elif ready:
        status = "ready"
        status_label = "Ready To Sync"
        detail = "All required source imports are available and the main customer sync can run now."
    else:
        status = "waiting"
        status_label = "Waiting For Sources"
        detail = "Waiting for: " + ", ".join(missing_targets)

    snapshot = {
        "reporting_date": reporting_date,
        "source_runs": source_runs,
        "missing_targets": missing_targets,
        "ready": ready,
        "synced": synced,
        "status": status,
        "status_label": status_label,
        "detail": detail,
        "main_customer_count": row_count,
        "last_synced_at": _format_datetime_display(last_synced_at),
        "last_synced_at_value": last_synced_at,
        "latest_source_completed_at": _format_datetime_display(latest_source_completed_at),
        "sync_due_at": _format_datetime_display(sync_due_at),
        "countdown_display": _format_duration(countdown_seconds) if countdown_seconds is not None else "-",
        "countdown_seconds": countdown_seconds if countdown_seconds is not None else 0,
        "delay_minutes": delay_minutes,
        "auto_sync_active": auto_sync_active,
    }
    cache.set(cache_key, snapshot, MAIN_SYNC_SNAPSHOT_CACHE_TTL_SECONDS)
    return snapshot

def _serialize_main_sync_source_details(snapshot: dict[str, Any]) -> dict[str, Any]:
    reporting_date = snapshot.get("reporting_date")
    return {
        "reporting_date": reporting_date.isoformat() if hasattr(reporting_date, "isoformat") else str(reporting_date or ""),
        "status": snapshot.get("status"),
        "status_label": snapshot.get("status_label"),
        "detail": snapshot.get("detail"),
        "latest_source_completed_at": snapshot.get("latest_source_completed_at"),
        "sync_due_at": snapshot.get("sync_due_at"),
        "countdown_display": snapshot.get("countdown_display"),
        "main_customer_count": snapshot.get("main_customer_count", 0),
        "source_runs": snapshot.get("source_runs", {}),
        "missing_targets": snapshot.get("missing_targets", []),
    }


def _format_main_sync_source_details_text(source_details: dict[str, Any] | None) -> str:
    if not isinstance(source_details, dict) or not source_details:
        return "No source details recorded."

    lines: list[str] = []
    if source_details.get("status_label"):
        lines.append(f"Snapshot status: {source_details['status_label']}")
    if source_details.get("detail"):
        lines.append(f"Detail: {source_details['detail']}")
    if source_details.get("latest_source_completed_at"):
        lines.append(f"Sources ready at: {source_details['latest_source_completed_at']}")
    if source_details.get("sync_due_at"):
        lines.append(f"Sync due at: {source_details['sync_due_at']}")
    if source_details.get("countdown_display"):
        lines.append(f"Countdown: {source_details['countdown_display']}")
    if source_details.get("main_customer_count") is not None:
        lines.append(f"Main customer rows: {source_details['main_customer_count']}")

    missing_targets = source_details.get("missing_targets") or []
    if missing_targets:
        lines.append("Missing targets: " + ", ".join(str(item) for item in missing_targets))

    source_runs = source_details.get("source_runs") or {}
    if source_runs:
        lines.append("")
        lines.append("Source runs:")
        for _, source in source_runs.items():
            label = source.get("label") or "Source"
            if source.get("available"):
                lines.append(
                    f"- {label}: Ready | Endpoint {source.get('endpoint_name', '-')} | "
                    f"Fetched {source.get('fetched', 0)} | Loaded {source.get('loaded', 0)} | "
                    f"Completed {source.get('completed_at', '-')}"
                )
            else:
                lines.append(f"- {label}: Waiting")

    return "\n".join(lines) if lines else "No source details recorded."


def _main_sync_lock_key(reporting_date: date) -> str:
    return f"scorecard:main-sync-lock:{reporting_date.isoformat()}"


def _main_sync_snapshot_cache_key(reporting_date: date) -> str:
    return f"scorecard:main-sync-snapshot:{reporting_date.isoformat()}"


def _recent_main_sync_run_queryset(limit: int | None = 15):
    if not _api_main_sync_history_ready():
        return ApiMainSyncRun.objects.none()

    queryset = ApiMainSyncRun.objects.select_related("triggered_by").order_by("-started_at", "-id")
    if limit is not None:
        queryset = queryset[:limit]
    return queryset


def _serialize_main_sync_run(run: ApiMainSyncRun) -> dict[str, Any]:
    detail_message = run.detail_message or ""
    is_recovered = "backfilled" in detail_message.lower()
    return {
        "id": run.id,
        "started_display": timezone.localtime(run.started_at).strftime("%Y-%m-%d %H:%M") if run.started_at else "-",
        "started_full": timezone.localtime(run.started_at).strftime("%Y-%m-%d %H:%M:%S") if run.started_at else "-",
        "completed_display": timezone.localtime(run.completed_at).strftime("%Y-%m-%d %H:%M:%S") if run.completed_at else "-",
        "reporting_date_display": run.reporting_date.strftime("%Y-%m-%d"),
        "source_display": run.get_run_source_display(),
        "status": run.status,
        "status_display": run.get_status_display(),
        "rows_synced": run.rows_synced,
        "duration_display": _format_duration(run.duration_seconds) if run.duration_seconds is not None else ("Recovered" if is_recovered else "-"),
        "triggered_by_display": str(run.triggered_by) if run.triggered_by else "-",
        "detail_message": detail_message or "No detail message recorded.",
        "failure_message": run.failure_message or "No failure message recorded.",
        "source_details_text": _format_main_sync_source_details_text(run.source_details),
        "history_type": "Recovered" if is_recovered else "Recorded",
    }


def _get_recent_main_sync_runs(limit: int | None = 15) -> list[dict[str, Any]]:
    return [_serialize_main_sync_run(run) for run in _recent_main_sync_run_queryset(limit=limit)]


def _backfill_missing_main_sync_history(sync_rows: list[dict[str, Any]]) -> None:
    if not _api_main_sync_history_ready():
        return

    for row in sync_rows:
        if row.get("status") != "synced":
            continue
        reporting_date = row.get("reporting_date")
        if not reporting_date:
            continue

        completed_at = row.get("last_synced_at_value") or timezone.now()
        latest_successful_run = (
            ApiMainSyncRun.objects.filter(
                reporting_date=reporting_date,
                status=ApiMainSyncRun.STATUS_SUCCESS,
            )
            .order_by("-completed_at", "-started_at", "-id")
            .first()
        )
        if latest_successful_run and latest_successful_run.completed_at:
            existing_completed_at = latest_successful_run.completed_at
            # Only backfill when the current synced state is newer than the latest saved history row.
            if abs((completed_at - existing_completed_at).total_seconds()) < 1:
                continue
            if completed_at <= existing_completed_at:
                continue

        ApiMainSyncRun.objects.create(
            reporting_date=reporting_date,
            run_source=ApiMainSyncRun.SOURCE_SCHEDULER,
            status=ApiMainSyncRun.STATUS_SUCCESS,
            started_at=completed_at,
            completed_at=completed_at,
            duration_seconds=0,
            rows_synced=row.get("main_customer_count", 0),
            source_details=_serialize_main_sync_source_details(row),
            detail_message="This sync history entry was backfilled from the latest main customer table status.",
            failure_message="",
        )


def _profile_data_by_customer_code() -> dict[str, dict[str, Any]]:
    profile_map: dict[str, dict[str, Any]] = {}

    for corporate in CustomerCorporate.objects.values(
        "client_code",
        "client_name",
        "resident_status",
        "registration_number",
        "industry_code",
        "sub_industry_code",
        "branch_code",
        "branch_name",
    ).iterator(chunk_size=BULK_SYNC_BATCH_SIZE):
        customer_code = _normalize_customer_code(corporate.get("client_code"))
        if not customer_code:
            continue
        profile_map[customer_code] = {
            "customer_name": corporate.get("client_name") or customer_code,
            "customer_type": "CORPORATE",
            "resident_status": _normalize_upper_text(corporate.get("resident_status")),
            "nationality_code": None,
            "national_id": None,
            "registration_number": _normalize_code_value(corporate.get("registration_number")),
            "industry_code": _normalize_code_value(corporate.get("industry_code")),
            "sub_industry_code": _normalize_code_value(corporate.get("sub_industry_code")),
            "occupation_code": None,
            "gender": None,
            "birth_date": None,
            "marital_status": None,
            "employment_type": None,
            "annual_income": None,
            "income_slab": None,
            "accommodation_type": None,
            "designation_code": None,
            "work_sector_code": None,
            "employer_code": None,
            "bank_relationship_flag": None,
            "pension_flag": None,
            "source_of_funds": None,
            "account_purpose": None,
            "employer_name": None,
            "mobile": None,
            "email": None,
            "branch_code": _normalize_code_value(corporate.get("branch_code")),
            "branch_name": _normalize_branch_description(corporate.get("branch_name")),
        }

    for individual in CustomerIndividual.objects.values(
        "client_code",
        "first_name",
        "middle_name",
        "surname",
        "last_name",
        "resident_status",
        "nationality_code",
        "pid_inv_number",
        "occupation_code",
        "gender",
        "birth_date",
        "marital_status",
        "employment_type",
        "annual_income",
        "income_slab",
        "accommodation_type",
        "designation_code",
        "work_sector_code",
        "employer_code",
        "bank_relationship_flag",
        "pension_flag",
        "source_of_funds",
        "account_purpose",
        "employer_name",
        "mobile_number",
        "phone_home",
        "email_primary",
        "email_secondary",
        "branch_code",
        "branch_name",
    ).iterator(chunk_size=BULK_SYNC_BATCH_SIZE):
        customer_code = _normalize_customer_code(individual.get("client_code"))
        if not customer_code or customer_code in profile_map:
            continue
        mobile = _coalesce(individual.get("mobile_number"), individual.get("phone_home"))
        email = _coalesce(individual.get("email_primary"), individual.get("email_secondary"))
        profile_map[customer_code] = {
            "customer_name": _build_individual_name(
                {
                    "first_name": individual.get("first_name"),
                    "middle_name": individual.get("middle_name"),
                    "surname": individual.get("surname"),
                    "last_name": individual.get("last_name"),
                    "client_code": customer_code,
                }
            )
            or customer_code,
            "customer_type": "INDIVIDUAL",
            "resident_status": _normalize_upper_text(individual.get("resident_status")),
            "nationality_code": _normalize_code_value(individual.get("nationality_code")),
            "national_id": _normalize_code_value(individual.get("pid_inv_number")),
            "registration_number": None,
            "industry_code": None,
            "sub_industry_code": None,
            "occupation_code": _normalize_code_value(individual.get("occupation_code")),
            "gender": _normalize_upper_text(individual.get("gender")),
            "birth_date": individual.get("birth_date"),
            "marital_status": _normalize_upper_text(individual.get("marital_status")),
            "employment_type": _normalize_upper_text(individual.get("employment_type")),
            "annual_income": _parse_decimal(individual.get("annual_income"), max_digits=22, decimal_places=3),
            "income_slab": _normalize_upper_text(individual.get("income_slab")),
            "accommodation_type": _normalize_upper_text(individual.get("accommodation_type")),
            "designation_code": _normalize_code_value(individual.get("designation_code")),
            "work_sector_code": _normalize_code_value(individual.get("work_sector_code")),
            "employer_code": _normalize_code_value(individual.get("employer_code")),
            "bank_relationship_flag": _normalize_upper_text(individual.get("bank_relationship_flag")),
            "pension_flag": _normalize_upper_text(individual.get("pension_flag")),
            "source_of_funds": _normalize_upper_text(individual.get("source_of_funds")),
            "account_purpose": _normalize_upper_text(individual.get("account_purpose")),
            "employer_name": _normalize_upper_text(individual.get("employer_name")),
            "mobile": _normalize_code_value(mobile),
            "email": _normalize_upper_text(email),
            "branch_code": _normalize_code_value(individual.get("branch_code")),
            "branch_name": _normalize_branch_description(individual.get("branch_name")),
        }

    return profile_map


def _rebuild_main_customers_for_reporting_date(reporting_date: date | None) -> None:
    if reporting_date is None or not _main_customer_table_ready():
        return

    from scorecard.functions_view.customers import sync_branch_master_from_rows

    profile_map = _profile_data_by_customer_code()
    unassigned_branch_name = "UNASSIGNED"
    unassigned_branch_code = "UNASSIGNED"

    existing_rows = {
        (row.reporting_date, row.customer_ref_code, (row.branch_code or "").strip().upper()): row
        for row in MainCustomer.objects.filter(reporting_date=reporting_date)
    }

    grouped_rows: dict[tuple[date, str, str], dict[str, Any]] = {}
    for customer_code, profile_data in profile_map.items():
        profile_branch_code = _normalize_code_value(profile_data.get("branch_code"))
        profile_branch_name = _normalize_branch_description(profile_data.get("branch_name"))
        customer_name = profile_data.get("customer_name") or customer_code
        resolved_branch_code = profile_branch_code or unassigned_branch_code
        resolved_branch_name = profile_branch_name or unassigned_branch_name
        key = (reporting_date, customer_code, resolved_branch_code)
        grouped_rows[key] = {
            "reporting_date": reporting_date,
            "customer_ref_code": customer_code,
            "branch_code": resolved_branch_code,
            "branch_name": resolved_branch_name,
            "branch_description": resolved_branch_name,
            "customer_name": customer_name,
            "loan_count": 0,
            "overdraft_count": 0,
            "primary_loan_id": None,
            "primary_account_number": None,
        }

    if not grouped_rows:
        if existing_rows:
            MainCustomer.objects.filter(reporting_date=reporting_date).delete()
        return

    # Keep Branch Master aligned with the same branch rows feeding MainCustomer,
    # so newly imported customer branches are available immediately in the UI.
    sync_branch_master_from_rows(list(grouped_rows.values()))

    synced_at = timezone.now()

    rows_to_create: list[MainCustomer] = []
    rows_to_update: list[MainCustomer] = []

    for key, row_data in grouped_rows.items():
        profile_data = profile_map.get(row_data["customer_ref_code"], {})
        defaults = {
            "customer_name": profile_data.get("customer_name") or row_data["customer_name"],
            "customer_type": profile_data.get("customer_type"),
            "resident_status": profile_data.get("resident_status"),
            "nationality_code": profile_data.get("nationality_code"),
            "national_id": profile_data.get("national_id"),
            "registration_number": profile_data.get("registration_number"),
            "industry_code": profile_data.get("industry_code"),
            "sub_industry_code": profile_data.get("sub_industry_code"),
            "occupation_code": profile_data.get("occupation_code"),
            "gender": profile_data.get("gender"),
            "birth_date": profile_data.get("birth_date"),
            "marital_status": profile_data.get("marital_status"),
            "employment_type": profile_data.get("employment_type"),
            "annual_income": profile_data.get("annual_income"),
            "income_slab": profile_data.get("income_slab"),
            "accommodation_type": profile_data.get("accommodation_type"),
            "designation_code": profile_data.get("designation_code"),
            "work_sector_code": profile_data.get("work_sector_code"),
            "employer_code": profile_data.get("employer_code"),
            "bank_relationship_flag": profile_data.get("bank_relationship_flag"),
            "pension_flag": profile_data.get("pension_flag"),
            "source_of_funds": profile_data.get("source_of_funds"),
            "account_purpose": profile_data.get("account_purpose"),
            "employer_name": profile_data.get("employer_name"),
            "mobile": profile_data.get("mobile"),
            "email": profile_data.get("email"),
            "loan_count": row_data["loan_count"],
            "overdraft_count": row_data["overdraft_count"],
            "has_loan": row_data["loan_count"] > 0,
            "has_overdraft": row_data["overdraft_count"] > 0,
            "primary_loan_id": row_data["primary_loan_id"],
            "primary_account_number": row_data["primary_account_number"],
            "last_synced_at": synced_at,
            "is_active_for_scoring": True,
        }
        existing = existing_rows.get(key)
        if existing is None:
            rows_to_create.append(
                MainCustomer(
                    reporting_date=row_data["reporting_date"],
                    customer_ref_code=row_data["customer_ref_code"],
                    branch_code=row_data["branch_code"],
                    branch_name=row_data["branch_name"],
                    branch_description=row_data["branch_description"],
                    **defaults,
                )
            )
            continue

            has_changes = False
            if existing.branch_code != row_data["branch_code"]:
                existing.branch_code = row_data["branch_code"]
                has_changes = True
            if existing.branch_name != row_data["branch_name"]:
                existing.branch_name = row_data["branch_name"]
                has_changes = True
            if existing.branch_description != row_data["branch_description"]:
                existing.branch_description = row_data["branch_description"]
                has_changes = True
            for field_name, new_value in defaults.items():
                if getattr(existing, field_name) != new_value:
                    setattr(existing, field_name, new_value)
                    has_changes = True
            if has_changes:
                rows_to_update.append(existing)

    stale_keys = set(existing_rows.keys()) - set(grouped_rows.keys())
    if stale_keys:
        stale_ids = [existing_rows[key].id for key in stale_keys]
        MainCustomer.objects.filter(id__in=stale_ids).delete()

    for chunk in _chunked(rows_to_create):
        _safe_bulk_create_main_customers(chunk)
        if rows_to_update:
            update_fields = [
                "branch_code",
                "branch_name",
                "branch_description",
                "customer_name",
                "customer_type",
                "resident_status",
                "nationality_code",
                "national_id",
            "registration_number",
            "industry_code",
            "sub_industry_code",
            "occupation_code",
            "gender",
            "birth_date",
            "marital_status",
            "employment_type",
            "annual_income",
            "income_slab",
            "accommodation_type",
            "designation_code",
            "work_sector_code",
            "employer_code",
            "bank_relationship_flag",
            "pension_flag",
            "source_of_funds",
            "account_purpose",
            "employer_name",
            "mobile",
            "email",
            "loan_count",
            "overdraft_count",
            "has_loan",
            "has_overdraft",
            "primary_loan_id",
            "primary_account_number",
            "last_synced_at",
            "is_active_for_scoring",
        ]
        for chunk in _chunked(rows_to_update):
            _safe_bulk_update_main_customers(chunk, update_fields)


def _safe_bulk_create_main_customers(rows: list[MainCustomer]) -> None:
    if not rows:
        return
    try:
        MainCustomer.objects.bulk_create(rows, batch_size=BULK_SYNC_BATCH_SIZE)
        return
    except DataError:
        pass

    for row in rows:
        try:
            with transaction.atomic():
                row.save(force_insert=True)
        except DataError as exc:
            if row.annual_income is not None:
                row.annual_income = None
                try:
                    with transaction.atomic():
                        row.save(force_insert=True)
                    continue
                except DataError as retry_exc:
                    raise DataError(
                        "MainCustomer insert failed after clearing annual_income "
                        f"for customer_ref_code={row.customer_ref_code}, "
                        f"branch_code={row.branch_code}, branch_name={row.branch_name}, "
                        f"loan_count={row.loan_count}, overdraft_count={row.overdraft_count}."
                    ) from retry_exc
            raise DataError(
                "MainCustomer insert failed "
                f"for customer_ref_code={row.customer_ref_code}, "
                f"branch_code={row.branch_code}, branch_name={row.branch_name}, "
                f"annual_income={row.annual_income!r}, "
                f"loan_count={row.loan_count}, overdraft_count={row.overdraft_count}."
            ) from exc


def _safe_bulk_update_main_customers(
    rows: list[MainCustomer],
    update_fields: list[str],
) -> None:
    if not rows:
        return
    try:
        MainCustomer.objects.bulk_update(rows, update_fields, batch_size=BULK_SYNC_BATCH_SIZE)
        return
    except DataError:
        pass

    for row in rows:
        try:
            with transaction.atomic():
                row.save(update_fields=update_fields)
        except DataError as exc:
            if row.annual_income is not None:
                row.annual_income = None
                retry_fields = list(dict.fromkeys([*update_fields, "annual_income"]))
                try:
                    with transaction.atomic():
                        row.save(update_fields=retry_fields)
                    continue
                except DataError as retry_exc:
                    raise DataError(
                        "MainCustomer update failed after clearing annual_income "
                        f"for customer_ref_code={row.customer_ref_code}, "
                        f"branch_code={row.branch_code}, branch_name={row.branch_name}, "
                        f"loan_count={row.loan_count}, overdraft_count={row.overdraft_count}."
                    ) from retry_exc
            raise DataError(
                "MainCustomer update failed "
                f"for customer_ref_code={row.customer_ref_code}, "
                f"branch_code={row.branch_code}, branch_name={row.branch_name}, "
                f"annual_income={row.annual_income!r}, "
                f"loan_count={row.loan_count}, overdraft_count={row.overdraft_count}."
            ) from exc

def _attempt_main_customer_sync(
    reporting_date: date | None,
    *,
    run_source: str = ApiMainSyncRun.SOURCE_SCHEDULER,
    triggered_by: Any = None,
) -> dict[str, Any]:
    if reporting_date is None:
        return {
            "performed": False,
            "status": "waiting",
            "detail": "No reporting date was available for the main customer sync.",
        }
    if not _main_customer_table_ready():
        return {
            "performed": False,
            "status": "waiting",
            "detail": "Main customer table is not available yet. Run the latest scorecard migrations.",
        }

    snapshot = _main_customer_sync_snapshot(reporting_date)
    if not snapshot["ready"]:
        return {
            "performed": False,
            "status": snapshot["status"],
            "detail": snapshot["detail"],
        }

    lock_key = _main_sync_lock_key(reporting_date)
    if not cache.add(lock_key, "1", timeout=MAIN_SYNC_LOCK_SECONDS):
        return {
            "performed": False,
            "status": "running",
            "detail": "A main customer sync for this reporting date is already starting or running. Please wait for it to finish.",
        }

    sync_run = None
    started_at = timezone.now()
    try:
        if _api_main_sync_history_ready():
            sync_run = ApiMainSyncRun.objects.create(
                reporting_date=reporting_date,
                run_source=run_source,
                triggered_by=triggered_by if getattr(triggered_by, "is_authenticated", False) else None,
                status=ApiMainSyncRun.STATUS_RUNNING,
                started_at=started_at,
                source_details=_serialize_main_sync_source_details(snapshot),
                detail_message=snapshot["detail"],
            )

        _rebuild_main_customers_for_reporting_date(reporting_date)
        refreshed_snapshot = _main_customer_sync_snapshot(reporting_date)
        completed_at = timezone.now()
        duration_seconds = max(0.0, (completed_at - started_at).total_seconds())
        if sync_run is not None:
            sync_run.status = ApiMainSyncRun.STATUS_SUCCESS
            sync_run.completed_at = completed_at
            sync_run.duration_seconds = duration_seconds
            sync_run.rows_synced = refreshed_snapshot.get("main_customer_count", 0)
            sync_run.source_details = _serialize_main_sync_source_details(refreshed_snapshot)
            sync_run.detail_message = refreshed_snapshot["detail"]
            sync_run.failure_message = ""
            sync_run.save(
                update_fields=[
                    "status",
                    "completed_at",
                    "duration_seconds",
                    "rows_synced",
                    "source_details",
                    "detail_message",
                    "failure_message",
                    "updated_at",
                ]
            )
            notify_main_sync_result(sync_run)
        return {
            "performed": True,
            "status": refreshed_snapshot["status"],
            "detail": refreshed_snapshot["detail"],
            "rows_synced": refreshed_snapshot.get("main_customer_count", 0),
            "run_id": sync_run.id if sync_run else None,
        }
    except Exception as exc:
        if sync_run is not None:
            sync_run.status = ApiMainSyncRun.STATUS_FAILED
            sync_run.completed_at = timezone.now()
            sync_run.duration_seconds = max(0.0, (sync_run.completed_at - started_at).total_seconds())
            sync_run.failure_message = str(exc)
            sync_run.save(
                update_fields=[
                    "status",
                    "completed_at",
                    "duration_seconds",
                    "failure_message",
                    "updated_at",
                ]
            )
            notify_main_sync_result(sync_run)
        raise
    finally:
        cache.delete(lock_key)


def run_due_main_customer_syncs() -> list[dict[str, Any]]:
    if not _api_main_sync_config_ready() or not _main_customer_table_ready() or not _api_import_history_ready():
        return []

    configuration = _main_sync_configuration()
    if configuration is None or not configuration.is_active:
        return []

    recent_runs = _get_recent_import_runs(limit=200)
    reporting_dates: set[date] = set()
    for run in recent_runs:
        params = run.parameters_used or {}
        reporting_date_value = _parse_date(params.get("reporting_date"))
        if reporting_date_value:
            reporting_dates.add(reporting_date_value)

    results: list[dict[str, Any]] = []
    for reporting_date in sorted(reporting_dates):
        snapshot = _main_customer_sync_snapshot(reporting_date)
        if snapshot["status"] not in {"ready", "awaiting_scheduler"}:
            continue
        sync_result = _attempt_main_customer_sync(
            reporting_date,
            run_source=ApiMainSyncRun.SOURCE_SCHEDULER,
        )
        results.append(
            {
                "reporting_date": reporting_date,
                "status": sync_result["status"],
                "detail": sync_result["detail"],
            }
        )

    if results:
        configuration.last_run_at = timezone.now()
        configuration.last_status = "success"
        configuration.last_message = f"Auto sync updated {len(results)} reporting date(s)."
        configuration.save(update_fields=["last_run_at", "last_status", "last_message", "updated_at"])
    return results


def _import_endpoint_records(endpoint: ApiEndpoint, records: list[dict[str, Any]]) -> SyncStats:
    if endpoint.target_table == ApiEndpoint.TARGET_CUSTOMER_CORPORATE:
        return upsert_corporate_clients(records)
    if endpoint.target_table == ApiEndpoint.TARGET_CUSTOMER_INDIVIDUAL:
        return upsert_individual_clients(records)
    if endpoint.target_table == ApiEndpoint.TARGET_CUSTOMER_LOAN:
        return upsert_loan_accounts(records)
    if endpoint.target_table == ApiEndpoint.TARGET_CUSTOMER_OVERDRAFT:
        return upsert_overdraft_accounts(records)
    raise ValueError("This endpoint does not have a valid target table configured.")


def _make_import_row_error_handler(
    stats: SyncStats,
    *,
    label: str,
    unique_field: str,
) -> Callable[[Any, Exception], None]:
    def handler(row: Any, exc: Exception) -> None:
        stats.skipped += 1
        lookup_value = _clean_str(getattr(row, unique_field, None)) or "-"
        _append_failure_sample(
            stats,
            reason=f"{label} row skipped after database validation",
            client_code=lookup_value,
            detail=str(exc)[:300],
        )

    return handler


@transaction.atomic
def upsert_corporate_clients(records: list[dict[str, Any]]) -> SyncStats:
    stats = SyncStats(fetched=len(records))
    reporting_date = _reporting_date_from_records(records)
    valid_entries: list[dict[str, Any]] = []

    for record in records:
        client_code = _normalize_code_value(record.get("client_code"))
        client_name = _normalize_upper_text(record.get("client_name"))
        if not client_code or not client_name:
            stats.skipped += 1
            stats.missing_required_skipped += 1
            missing_fields: list[str] = []
            if not client_code:
                missing_fields.append("client_code")
            if not client_name:
                missing_fields.append("client_name")
            _append_failure_sample(
                stats,
                reason="Missing required corporate fields",
                client_code=client_code,
                detail=f"Missing: {', '.join(missing_fields)}",
            )
            continue
        valid_entries.append(
            {
                "client_code": client_code,
                "record": record,
                "customer_defaults": {
                    "customer_name": client_name,
                    "customer_type": "Corporate",
                    "email": None,
                    "mobile": None,
                    "landline": None,
                    "national_id": None,
                },
            }
        )

    valid_entries, duplicate_count, duplicate_samples = _deduplicate_entries(valid_entries)
    stats.skipped += duplicate_count
    stats.duplicate_skipped += duplicate_count
    for sample in duplicate_samples:
        _append_failure_sample(
            stats,
            reason=sample["reason"],
            client_code=sample["client_code"],
            detail=sample["detail"],
        )
    stats.created, stats.updated, stats.unchanged = _bulk_upsert_detail_records(
        model=CustomerCorporate,
        entries=valid_entries,
        defaults_builder=_build_corporate_detail_defaults,
        row_error_handler=_make_import_row_error_handler(
            stats,
            label="Corporate client",
            unique_field="client_code",
        ),
    )
    return stats


@transaction.atomic
def upsert_individual_clients(records: list[dict[str, Any]]) -> SyncStats:
    stats = SyncStats(fetched=len(records))
    reporting_date = _reporting_date_from_records(records)
    valid_entries: list[dict[str, Any]] = []

    for record in records:
        client_code = _normalize_code_value(record.get("client_code"))
        if not client_code:
            stats.skipped += 1
            stats.missing_required_skipped += 1
            _append_failure_sample(
                stats,
                reason="Missing required individual fields",
                client_code=client_code,
                detail="Missing: client_code",
            )
            continue
        valid_entries.append(
            {
                "client_code": client_code,
                "record": record,
                "customer_defaults": {
                    "customer_name": _build_individual_name(record),
                    "customer_type": "Individual",
                    "email": _coalesce(record.get("email_primary"), record.get("email_secondary")),
                    "mobile": _coalesce(record.get("mobile_number"), record.get("phone_home")),
                    "landline": _coalesce(record.get("phone_office"), record.get("phone_office_alt")),
                    "national_id": _clean_str(record.get("pid_inv_number")),
                },
            }
        )

    valid_entries, duplicate_count, duplicate_samples = _deduplicate_entries(valid_entries)
    stats.skipped += duplicate_count
    stats.duplicate_skipped += duplicate_count
    for sample in duplicate_samples:
        _append_failure_sample(
            stats,
            reason=sample["reason"],
            client_code=sample["client_code"],
            detail=sample["detail"],
        )
    stats.created, stats.updated, stats.unchanged = _bulk_upsert_detail_records(
        model=CustomerIndividual,
        entries=valid_entries,
        defaults_builder=_build_individual_detail_defaults,
        row_error_handler=_make_import_row_error_handler(
            stats,
            label="Individual client",
            unique_field="client_code",
        ),
    )
    return stats


LOAN_TEXT_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "branch_code": ("branch_code", "v_branch_code"),
    "branch_name": ("branch_name", "v_branch_name"),
    "account_number": ("account_number", "account_no", "v_account_number"),
    "currency_code": ("currency_code", "ccy_code", "v_ccy_code"),
    "sector_code": ("sector_code", "v_sector_code"),
    "facility_sector": ("facility_sector", "v_facility_sector"),
    "portfolio_name": ("portfolio_name", "v_portfolio_name"),
    "portfolio_code": ("portfolio_code", "n_portfolio_code"),
    "loan_type": ("loan_type", "v_loan_type"),
    "collateral_type": ("collateral_type", "v_collateral_type"),
    "past_due_indicator": ("past_due_indicator", "v_past_due_indicator"),
    "npl_indicator_current": ("npl_indicator_current", "v_npl_indicator_current"),
    "npl_indicator_prev": ("npl_indicator_prev", "v_npl_indicator_prev"),
    "npl_indicator_additions": ("npl_indicator_additions", "v_npl_indicator_additions"),
    "interest_frequency_unit": ("interest_frequency_unit", "interest_freq_unit", "v_interest_freq_unit"),
    "interest_payment_type": ("interest_payment_type", "v_interest_payment_type"),
    "day_count_indicator": ("day_count_indicator", "day_count_ind", "v_day_count_ind"),
    "amortization_repayment_type": ("amortization_repayment_type", "amrt_repayment_type", "v_amrt_repayment_type"),
    "amortization_term_unit": ("amortization_term_unit", "amrt_term_unit", "v_amrt_term_unit"),
    "repayment_month": ("repayment_month", "n_repayment_month"),
}

LOAN_DATE_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "start_date": ("start_date", "d_start_date"),
    "maturity_date": ("maturity_date", "d_maturity_date"),
    "next_payment_date": ("next_payment_date", "d_next_payment_date"),
    "last_payment_date": ("last_payment_date", "d_last_payment_date"),
    "restructure_date": ("restructure_date", "d_restructure_date"),
    "final_disbursement_date": ("final_disbursement_date", "d_final_disbursement_date"),
}

LOAN_DECIMAL_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "customer_target": ("customer_target", "n_cust_target"),
    "current_interest_rate": ("current_interest_rate", "curr_interest_rate", "n_curr_interest_rate"),
    "effective_interest_rate": ("effective_interest_rate", "n_effective_interest_rate"),
    "accrued_interest": ("accrued_interest", "n_accrued_interest"),
    "suspended_interest": ("suspended_interest", "suspedend_interest", "n_suspedend_interest", "n_suspended_interest"),
    "penalty_interest": ("penalty_interest", "n_penalty_interest"),
    "percent": ("percent", "n_percent"),
    "loan_amount": ("loan_amount", "n_loan_amount", "n_sanctioned_limit", "sanctioned_limit"),
    "loan_balance": ("loan_balance", "n_loan_balance"),
    "outstanding_balance": ("outstanding_balance", "n_outstanding_balance"),
    "current_outstanding_balance": ("current_outstanding_balance", "n_curr_outstanding_balance"),
    "undrawn_amount": ("undrawn_amount", "n_undrawn_amount"),
    "collateral_amount": ("collateral_amount", "n_collateral_amount"),
    "overdue_amount": ("overdue_amount", "n_overdue_amount"),
    "repayment_amount": ("repayment_amount", "n_repayment_amount"),
    "installment_amount": ("installment_amount", "n_installment_amount"),
    "pd_percent": ("pd_percent", "n_pd_percent"),
    "lgd_percent": ("lgd_percent", "n_lgd_percent"),
}

LOAN_INTEGER_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "delinquent_days": ("delinquent_days", "n_delinquent_days"),
}

LOAN_CODE_FIELDS = {
    "account_number",
    "branch_code",
    "ccy_code",
    "client_code",
    "currency_code",
    "customer_code",
    "customer_ref_code",
    "loan_id",
    "product_code",
    "sector_code",
    "v_account_number",
    "v_branch_code",
    "v_ccy_code",
    "v_cust_ref_code",
    "v_loan_id",
    "v_prod_code",
    "v_sector_code",
}


def _first_present_value(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record and record.get(key) not in (None, ""):
            return record.get(key)
    return None


def _parse_integer(value: Any) -> int | None:
    parsed = _parse_decimal(value, decimal_places=0)
    if parsed is None:
        return None
    try:
        return int(parsed)
    except (TypeError, ValueError):
        return None


def _normalize_loan_record_for_storage(record: dict[str, Any]) -> dict[str, Any]:
    normalized_record = _normalize_record_for_storage(record, code_fields=LOAN_CODE_FIELDS)
    normalized_record.update(
        {
            "reporting_date": _clean_str(
                _first_present_value(record, "reporting_date", "fic_mis_date", "d_reporting_date")
            ),
            "customer_code": _normalize_customer_code(
                _first_present_value(record, "customer_code", "v_cust_ref_code", "client_code", "customer_ref_code")
            ),
            "customer_name": _normalize_upper_text(
                _first_present_value(record, "customer_name", "v_cust_name", "client_name")
            ),
            "branch_description": _normalize_branch_description(
                _first_present_value(record, "branch_description", "branch_name", "v_branch_name")
            ),
            "product_code": _normalize_code_value(
                _first_present_value(record, "product_code", "v_prod_code")
            ),
            "product_name": _normalize_upper_text(
                _first_present_value(record, "product_name", "v_prod_name")
            ),
            "product_category": _normalize_upper_text(
                _first_present_value(record, "product_category", "v_prod_category")
            ),
            "loan_id": _normalize_code_value(
                _first_present_value(record, "loan_id", "v_loan_id", "account_number")
            ),
        }
    )
    return normalized_record


def _build_loan_detail_defaults(
    record: dict[str, Any],
    synced_at: datetime,
) -> dict[str, Any]:
    normalized_record = _normalize_loan_record_for_storage(record)
    defaults = {
        "reporting_date": _parse_date(normalized_record.get("reporting_date")),
        "customer_code": normalized_record.get("customer_code"),
        "customer_name": normalized_record.get("customer_name"),
        "branch_description": normalized_record.get("branch_description"),
        "product_code": normalized_record.get("product_code"),
        "product_name": normalized_record.get("product_name"),
        "product_category": normalized_record.get("product_category"),
        "loan_id": normalized_record.get("loan_id"),
        "raw_payload": normalized_record,
        "source_last_sync_at": synced_at,
    }
    for field_name, aliases in LOAN_TEXT_FIELD_ALIASES.items():
        defaults[field_name] = _normalize_whitespace(_first_present_value(normalized_record, *aliases))
    for field_name, aliases in LOAN_DATE_FIELD_ALIASES.items():
        defaults[field_name] = _parse_date(_first_present_value(normalized_record, *aliases))
    for field_name, aliases in LOAN_DECIMAL_FIELD_ALIASES.items():
        defaults[field_name] = _parse_decimal(_first_present_value(normalized_record, *aliases), max_digits=24, decimal_places=6)
    for field_name, aliases in LOAN_INTEGER_FIELD_ALIASES.items():
        defaults[field_name] = _parse_integer(_first_present_value(normalized_record, *aliases))
    return defaults


def _build_overdraft_detail_defaults(
    record: dict[str, Any],
    synced_at: datetime,
) -> dict[str, Any]:
    filtered_record = {
        "reporting_date": _clean_str(record.get("reporting_date")),
        "customer_code": _normalize_customer_code(record.get("customer_code")),
        "customer_name": _normalize_upper_text(record.get("customer_name")),
        "branch_description": _normalize_branch_description(record.get("branch_description")),
        "ac_category": _normalize_code_value(record.get("ac_category")),
        "account_number": _normalize_code_value(record.get("account_number")),
    }
    return {
        "reporting_date": _parse_date(filtered_record.get("reporting_date")),
        "customer_code": filtered_record.get("customer_code"),
        "customer_name": filtered_record.get("customer_name"),
        "branch_description": filtered_record.get("branch_description"),
        "ac_category": filtered_record.get("ac_category"),
        "account_number": filtered_record.get("account_number"),
        "raw_payload": filtered_record,
        "source_last_sync_at": synced_at,
    }


@transaction.atomic
def upsert_loan_accounts(records: list[dict[str, Any]]) -> SyncStats:
    stats = SyncStats(fetched=len(records))
    reporting_date = _reporting_date_from_records(records)
    valid_entries: list[dict[str, Any]] = []

    for record in records:
        customer_code = _normalize_customer_code(
            _first_present_value(record, "customer_code", "v_cust_ref_code", "client_code", "customer_ref_code")
        )
        customer_name = _normalize_upper_text(
            _first_present_value(record, "customer_name", "v_cust_name", "client_name")
        )
        loan_id = _normalize_code_value(
            _first_present_value(record, "loan_id", "v_loan_id", "account_number")
        )
        if not customer_code or not loan_id:
            stats.skipped += 1
            stats.missing_required_skipped += 1
            missing_fields: list[str] = []
            if not customer_code:
                missing_fields.append("customer_code")
            if not loan_id:
                missing_fields.append("loan_id")
            _append_failure_sample(
                stats,
                reason="Missing required loan fields",
                client_code=loan_id or customer_code,
                detail=f"Missing: {', '.join(missing_fields)}",
            )
            continue
        valid_entries.append(
            {
                "customer_code": customer_code,
                "loan_id": loan_id,
                "record": record,
                "customer_defaults": {
                    "customer_name": customer_name or customer_code,
                    "customer_type": "Borrower",
                    "email": None,
                    "mobile": None,
                    "landline": None,
                    "national_id": None,
                },
            }
        )

    valid_entries, duplicate_count, duplicate_samples = _deduplicate_entries(
        [{**entry, "client_code": entry["loan_id"]} for entry in valid_entries]
    )
    stats.skipped += duplicate_count
    stats.duplicate_skipped += duplicate_count
    for sample in duplicate_samples:
        _append_failure_sample(
            stats,
            reason="Duplicate loan_id from API",
            client_code=sample["client_code"],
            detail=sample["detail"],
        )
    normalized_entries = [
        {
            "customer_code": entry["customer_code"],
            "loan_id": entry["loan_id"],
            "record": entry["record"],
        }
        for entry in valid_entries
    ]
    stats.created, stats.updated, stats.unchanged = _bulk_upsert_detail_records(
        model=CustomerLoan,
        entries=normalized_entries,
        defaults_builder=_build_loan_detail_defaults,
        unique_field="loan_id",
        entry_key="loan_id",
        row_error_handler=_make_import_row_error_handler(
            stats,
            label="Loan",
            unique_field="loan_id",
        ),
    )
    return stats


@transaction.atomic
def upsert_overdraft_accounts(records: list[dict[str, Any]]) -> SyncStats:
    stats = SyncStats(fetched=len(records))
    reporting_date = _reporting_date_from_records(records)
    valid_entries: list[dict[str, Any]] = []

    for record in records:
        customer_code = _normalize_customer_code(record.get("customer_code"))
        customer_name = _normalize_upper_text(record.get("customer_name"))
        account_number = _normalize_code_value(record.get("account_number"))
        if not customer_code or not account_number:
            stats.skipped += 1
            stats.missing_required_skipped += 1
            missing_fields: list[str] = []
            if not customer_code:
                missing_fields.append("customer_code")
            if not account_number:
                missing_fields.append("account_number")
            _append_failure_sample(
                stats,
                reason="Missing required overdraft fields",
                client_code=account_number or customer_code,
                detail=f"Missing: {', '.join(missing_fields)}",
            )
            continue
        valid_entries.append(
            {
                "customer_code": customer_code,
                "account_number": account_number,
                "record": record,
                "customer_defaults": {
                    "customer_name": customer_name or customer_code,
                    "customer_type": "Borrower",
                    "email": None,
                    "mobile": None,
                    "landline": None,
                    "national_id": None,
                },
            }
        )

    valid_entries, duplicate_count, duplicate_samples = _deduplicate_entries(
        [{**entry, "client_code": entry["account_number"]} for entry in valid_entries]
    )
    stats.skipped += duplicate_count
    stats.duplicate_skipped += duplicate_count
    for sample in duplicate_samples:
        _append_failure_sample(
            stats,
            reason="Duplicate account_number from API",
            client_code=sample["client_code"],
            detail=sample["detail"],
        )
    normalized_entries = [
        {
            "customer_code": entry["customer_code"],
            "account_number": entry["account_number"],
            "record": entry["record"],
        }
        for entry in valid_entries
    ]
    stats.created, stats.updated, stats.unchanged = _bulk_upsert_detail_records(
        model=CustomerOverdraft,
        entries=normalized_entries,
        defaults_builder=_build_overdraft_detail_defaults,
        unique_field="account_number",
        entry_key="account_number",
        row_error_handler=_make_import_row_error_handler(
            stats,
            label="Overdraft",
            unique_field="account_number",
        ),
    )
    return stats
