from __future__ import annotations

import calendar
import inspect
from collections.abc import Iterable
from datetime import time as datetime_time
from importlib import import_module
from typing import Any, Callable

from django.utils import timezone

from scorecard.functions_view.score_auto_refresh_notifications import (
    notify_auto_update_completed,
    serialize_auto_update_item,
)
from scorecard.workflow_approval import get_scorecard_workflow_approval_settings


AUTO_REFRESH_ENGINE_CANDIDATES = (
    ("scorecard.functions_view.score_auto_refresh_engine", "run_autofilled_score_auto_update"),
    ("scorecard.functions_view.score_auto_refresh_engine", "refresh_autofilled_scores"),
    ("scorecard.functions_view.scorecard_autofill", "run_autofilled_score_auto_update"),
    ("scorecard.functions_view.scorecard_autofill", "refresh_autofilled_scores"),
)
VALID_AUTO_REFRESH_FREQUENCIES = {"daily", "weekly", "monthly"}
AUTO_REFRESH_BATCH_DEFAULT = 1000
AUTO_REFRESH_BATCH_MIN = 1
AUTO_REFRESH_BATCH_MAX = 10000


def _clean_positive_int(
    value: Any,
    default: int,
    minimum: int = AUTO_REFRESH_BATCH_MIN,
    maximum: int = AUTO_REFRESH_BATCH_MAX,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _local_target_for_today(settings_obj: Any, now) -> Any:
    configured_time = (
        getattr(settings_obj, "auto_refresh_autofilled_scores_time", None)
        or datetime_time(2, 0)
    )
    local_now = timezone.localtime(now)
    return local_now.replace(
        hour=configured_time.hour,
        minute=configured_time.minute,
        second=0,
        microsecond=0,
    )


def _auto_refresh_due_reason(settings_obj: Any, now) -> str:
    local_now = timezone.localtime(now)
    target = _local_target_for_today(settings_obj, now)
    if local_now < target:
        return "not_due_time"

    frequency = (
        getattr(settings_obj, "auto_refresh_autofilled_scores_frequency", "daily")
        or "daily"
    ).lower()
    if frequency not in VALID_AUTO_REFRESH_FREQUENCIES:
        frequency = "daily"

    if frequency == "weekly":
        try:
            weekday = int(getattr(settings_obj, "auto_refresh_autofilled_scores_weekday", 0) or 0)
        except (TypeError, ValueError):
            weekday = 0
        if local_now.weekday() != weekday:
            return "not_due_weekday"

    if frequency == "monthly":
        try:
            requested_day = int(
                getattr(settings_obj, "auto_refresh_autofilled_scores_month_day", 1) or 1
            )
        except (TypeError, ValueError):
            requested_day = 1
        requested_day = min(max(requested_day, 1), 31)
        due_day = min(requested_day, calendar.monthrange(local_now.year, local_now.month)[1])
        if local_now.day != due_day:
            return "not_due_month_day"

    last_run = getattr(settings_obj, "auto_refresh_autofilled_scores_last_run_at", None)
    if not last_run:
        return ""

    local_last_run = timezone.localtime(last_run)
    last_run_slot = local_last_run.replace(second=0, microsecond=0)
    target_slot = target.replace(second=0, microsecond=0)
    if frequency == "daily" and last_run_slot == target_slot:
        return "already_ran_today"
    if frequency == "weekly" and last_run_slot == target_slot:
        return "already_ran_this_week"
    if frequency == "monthly" and last_run_slot == target_slot:
        return "already_ran_this_month"
    return ""


def _auto_refresh_schedule_context(settings_obj: Any, now) -> dict[str, Any]:
    configured_time = (
        getattr(settings_obj, "auto_refresh_autofilled_scores_time", None)
        or datetime_time(2, 0)
    )
    frequency = (
        getattr(settings_obj, "auto_refresh_autofilled_scores_frequency", "daily")
        or "daily"
    ).lower()
    if frequency not in VALID_AUTO_REFRESH_FREQUENCIES:
        frequency = "daily"

    last_run = getattr(settings_obj, "auto_refresh_autofilled_scores_last_run_at", None)
    context = {
        "enabled": bool(getattr(settings_obj, "auto_refresh_autofilled_scores_enabled", False)),
        "frequency": frequency,
        "scheduled_time": configured_time.strftime("%H:%M"),
        "scheduled_for": _local_target_for_today(settings_obj, now),
        "checked_at": timezone.localtime(now),
        "last_run_at": timezone.localtime(last_run) if last_run else None,
        "batch_size": _clean_positive_int(
            getattr(settings_obj, "auto_refresh_autofilled_scores_batch_size", AUTO_REFRESH_BATCH_DEFAULT),
            AUTO_REFRESH_BATCH_DEFAULT,
        ),
        "basel_cursor_id": _clean_positive_int(
            getattr(settings_obj, "auto_refresh_autofilled_scores_basel_cursor_id", 0),
            0,
            minimum=0,
            maximum=2147483647,
        ),
        "ifrs9_cursor_id": _clean_positive_int(
            getattr(settings_obj, "auto_refresh_autofilled_scores_ifrs9_cursor_id", 0),
            0,
            minimum=0,
            maximum=2147483647,
        ),
    }
    if frequency == "weekly":
        context["weekday"] = int(getattr(settings_obj, "auto_refresh_autofilled_scores_weekday", 0) or 0)
    if frequency == "monthly":
        context["month_day"] = int(getattr(settings_obj, "auto_refresh_autofilled_scores_month_day", 1) or 1)
    return context


def _load_auto_refresh_engine() -> Callable[..., Any] | None:
    for module_path, function_name in AUTO_REFRESH_ENGINE_CANDIDATES:
        try:
            module = import_module(module_path)
        except ModuleNotFoundError as exc:
            if exc.name == module_path:
                continue
            raise
        engine = getattr(module, function_name, None)
        if callable(engine):
            return engine
    return None


def _call_engine(engine: Callable[..., Any], now, schedule_context: dict[str, Any]) -> Any:
    try:
        parameters = inspect.signature(engine).parameters
    except (TypeError, ValueError):
        parameters = {}

    if not parameters:
        return engine()

    kwargs: dict[str, Any] = {}
    if "now" in parameters:
        kwargs["now"] = now
    if "batch_size" in parameters:
        kwargs["batch_size"] = schedule_context.get("batch_size")
    if "basel_after_id" in parameters:
        kwargs["basel_after_id"] = schedule_context.get("basel_cursor_id")
    if "ifrs9_after_id" in parameters:
        kwargs["ifrs9_after_id"] = schedule_context.get("ifrs9_cursor_id")
    return engine(**kwargs)


def _save_auto_refresh_completion_state(
    settings_obj: Any,
    schedule_context: dict[str, Any],
    result: dict[str, Any],
    pending_updates: list[dict[str, Any]] | None = None,
) -> None:
    update_fields = ["auto_refresh_autofilled_scores_last_run_at"]
    settings_obj.auto_refresh_autofilled_scores_last_run_at = schedule_context["scheduled_for"]

    for field_name, result_key in (
        ("auto_refresh_autofilled_scores_basel_cursor_id", "basel_cursor_id"),
        ("auto_refresh_autofilled_scores_ifrs9_cursor_id", "ifrs9_cursor_id"),
    ):
        if hasattr(settings_obj, field_name):
            setattr(
                settings_obj,
                field_name,
                _clean_positive_int(
                    result.get(result_key),
                    0,
                    minimum=0,
                    maximum=2147483647,
                ),
            )
            update_fields.append(field_name)

    if pending_updates is not None and hasattr(settings_obj, "auto_refresh_autofilled_scores_pending_updates"):
        settings_obj.auto_refresh_autofilled_scores_pending_updates = pending_updates
        update_fields.append("auto_refresh_autofilled_scores_pending_updates")

    settings_obj.save(update_fields=update_fields)


def _list_or_empty(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
        return list(value)
    return []


def _normalize_engine_result(raw_result: Any) -> dict[str, Any]:
    if raw_result is None:
        return {"updated_items": []}
    if isinstance(raw_result, dict):
        result = dict(raw_result)
        updated_items: list[Any] = []
        for key in ("updated_items", "updates", "changed_customers", "customers", "changed_items"):
            if key in result:
                updated_items = _list_or_empty(result.pop(key))
                break
        result["updated_items"] = updated_items
        return result
    return {"updated_items": _list_or_empty(raw_result)}


def _pending_auto_refresh_updates(settings_obj: Any) -> list[dict[str, Any]]:
    value = getattr(settings_obj, "auto_refresh_autofilled_scores_pending_updates", None)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _save_pending_auto_refresh_updates(settings_obj: Any, pending_updates: list[dict[str, Any]]) -> None:
    if not hasattr(settings_obj, "auto_refresh_autofilled_scores_pending_updates"):
        return
    settings_obj.auto_refresh_autofilled_scores_pending_updates = pending_updates
    settings_obj.save(update_fields=["auto_refresh_autofilled_scores_pending_updates"])


def _auto_refresh_update_key(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(item.get("score_type", "") or ""),
        str(item.get("customer_code", "") or ""),
        str(item.get("branch_name", "") or ""),
        str(item.get("template_code", "") or item.get("template", "") or ""),
        str(item.get("version", "") or ""),
    )


def _merge_auto_refresh_updates(
    existing_items: list[dict[str, Any]],
    new_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    positions: dict[tuple[str, str, str, str, str], int] = {}
    for item in [*existing_items, *new_items]:
        key = _auto_refresh_update_key(item)
        if key in positions:
            merged[positions[key]] = item
            continue
        positions[key] = len(merged)
        merged.append(item)
    return merged


def _auto_refresh_cycle_complete(result: dict[str, Any]) -> bool:
    return bool(result.get("basel_cycle_complete")) and bool(result.get("ifrs9_cycle_complete"))


def run_due_autofilled_score_refresh(now=None) -> dict[str, Any]:
    now = now or timezone.now()
    settings_obj = get_scorecard_workflow_approval_settings()
    schedule_context = _auto_refresh_schedule_context(settings_obj, now)
    if not bool(getattr(settings_obj, "auto_refresh_autofilled_scores_enabled", False)):
        return {"performed": False, "reason": "disabled", **schedule_context}

    due_reason = _auto_refresh_due_reason(settings_obj, now)
    if due_reason:
        return {"performed": False, "reason": due_reason, **schedule_context}

    engine = _load_auto_refresh_engine()
    if engine is None:
        return {"performed": False, "reason": "no_engine", **schedule_context}

    normalized = _normalize_engine_result(_call_engine(engine, now, schedule_context))
    updated_items = normalized.pop("updated_items", [])
    serialized_updates = [serialize_auto_update_item(item) for item in updated_items]
    pending_updates = _merge_auto_refresh_updates(
        _pending_auto_refresh_updates(settings_obj),
        serialized_updates,
    )
    _save_pending_auto_refresh_updates(settings_obj, pending_updates)

    cycle_complete = _auto_refresh_cycle_complete(normalized)
    if cycle_complete:
        notification_summary = {
            **normalized,
            "cycle_complete": True,
            "cycle_updated_count": len(pending_updates),
        }
        notification_result = notify_auto_update_completed(
            pending_updates,
            completed_at=now,
            summary=notification_summary,
        )
        pending_updates_to_save: list[dict[str, Any]] = []
    else:
        notification_result = {
            "notified": False,
            "reason": "cycle_in_progress",
            "updated": len(serialized_updates),
            "pending_updates": len(pending_updates),
        }
        pending_updates_to_save = pending_updates

    _save_auto_refresh_completion_state(
        settings_obj,
        schedule_context,
        normalized,
        pending_updates=pending_updates_to_save,
    )

    return {
        "performed": True,
        "reason": "completed",
        "updated": len(updated_items),
        "cycle_complete": cycle_complete,
        "cycle_updates_pending": len(pending_updates_to_save),
        "cycle_updated_count": len(pending_updates),
        "notification": notification_result,
        **schedule_context,
        **normalized,
    }
