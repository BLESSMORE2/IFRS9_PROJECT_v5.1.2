from __future__ import annotations

import calendar
from collections.abc import Iterable
from datetime import time as datetime_time
from importlib import import_module
from typing import Any, Callable

from django.utils import timezone

from scorecard.functions_view.score_auto_refresh_notifications import notify_auto_update_completed
from scorecard.workflow_approval import get_scorecard_workflow_approval_settings


AUTO_REFRESH_ENGINE_CANDIDATES = (
    ("scorecard.functions_view.score_auto_refresh_engine", "run_autofilled_score_auto_update"),
    ("scorecard.functions_view.score_auto_refresh_engine", "refresh_autofilled_scores"),
    ("scorecard.functions_view.scorecard_autofill", "run_autofilled_score_auto_update"),
    ("scorecard.functions_view.scorecard_autofill", "refresh_autofilled_scores"),
)
VALID_AUTO_REFRESH_FREQUENCIES = {"daily", "weekly", "monthly"}


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


def _call_engine(engine: Callable[..., Any], now) -> Any:
    try:
        return engine(now=now)
    except TypeError:
        return engine()


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

    normalized = _normalize_engine_result(_call_engine(engine, now))
    updated_items = normalized.pop("updated_items", [])
    notification_result = notify_auto_update_completed(
        updated_items,
        completed_at=now,
        summary=normalized,
    )

    settings_obj.auto_refresh_autofilled_scores_last_run_at = schedule_context["scheduled_for"]
    settings_obj.save(update_fields=["auto_refresh_autofilled_scores_last_run_at"])

    return {
        "performed": True,
        "reason": "completed",
        "updated": len(updated_items),
        "notification": notification_result,
        **schedule_context,
        **normalized,
    }
