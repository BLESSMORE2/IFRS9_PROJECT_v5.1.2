from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

from django.core.cache import cache
from django.db.models import Count, Max


EXPORT_CACHE_SECONDS = 30 * 60
EXPORT_CACHE_VERSION = "4"
PAYLOAD_CACHE_SECONDS = 15 * 60
PAYLOAD_CACHE_VERSION = "3"


def _validation_data_fingerprint(queryset, previous_date: date, current_date: date) -> dict[str, Any]:
    return queryset.filter(reporting_date__in=(previous_date, current_date)).aggregate(
        row_count=Count("pk"),
        latest_update=Max("updated_at"),
    )


def validation_payload_cache_key(
    *,
    report_name: str,
    queryset,
    previous_date: date,
    current_date: date,
    branch_names: list[str],
    selected_branch: str,
) -> str:
    fingerprint = _validation_data_fingerprint(queryset, previous_date, current_date)
    latest_update = fingerprint["latest_update"]
    payload = {
        "version": PAYLOAD_CACHE_VERSION,
        "report": report_name,
        "previous_date": previous_date.isoformat(),
        "current_date": current_date.isoformat(),
        "branches": sorted({str(name).strip().casefold() for name in branch_names if str(name).strip()}),
        "selected_branch": selected_branch.strip().casefold(),
        "row_count": fingerprint["row_count"],
        "latest_update": latest_update.isoformat() if latest_update else "",
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"validation-payload:{report_name}:{digest}"


def get_cached_validation_payload(cache_key: str) -> dict[str, Any] | None:
    try:
        cached = cache.get(cache_key)
    except Exception:
        return None
    return cached if isinstance(cached, dict) else None


def cache_validation_payload(cache_key: str, payload: dict[str, Any]) -> None:
    try:
        cache.set(cache_key, payload, timeout=PAYLOAD_CACHE_SECONDS)
    except Exception:
        # Report generation must remain available if the configured cache is unavailable.
        return


def validation_export_cache_key(
    *,
    report_name: str,
    queryset,
    previous_date: date,
    current_date: date,
    branch_names: list[str],
    selected_branch: str,
    branch_scope_label: str,
    movement_filter: str,
    search: str,
) -> str:
    fingerprint = _validation_data_fingerprint(queryset, previous_date, current_date)
    latest_update = fingerprint["latest_update"]
    payload = {
        "version": EXPORT_CACHE_VERSION,
        "report": report_name,
        "previous_date": previous_date.isoformat(),
        "current_date": current_date.isoformat(),
        "branches": sorted({str(name).strip().casefold() for name in branch_names if str(name).strip()}),
        "selected_branch": selected_branch.strip().casefold(),
        "scope_label": branch_scope_label.strip(),
        "movement": movement_filter,
        "search": search.casefold(),
        "row_count": fingerprint["row_count"],
        "latest_update": latest_update.isoformat() if latest_update else "",
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"validation-export:{report_name}:{digest}"


def get_cached_validation_export(cache_key: str) -> tuple[bytes, int] | None:
    try:
        cached = cache.get(cache_key)
    except Exception:
        return None
    if not isinstance(cached, dict):
        return None
    content = cached.get("content")
    customer_rows = cached.get("customer_rows")
    if not isinstance(content, bytes) or not isinstance(customer_rows, int):
        return None
    return content, customer_rows


def cache_validation_export(cache_key: str, content: bytes, customer_rows: int) -> None:
    try:
        cache.set(
            cache_key,
            {"content": content, "customer_rows": customer_rows},
            timeout=EXPORT_CACHE_SECONDS,
        )
    except Exception:
        # Export generation must remain available if the configured cache is unavailable.
        return
