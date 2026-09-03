from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from io import StringIO
from typing import Any, Iterable

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from scorecard.functions_view.notifications import create_notifications_for_users
from scorecard.models import ScorecardDocument, ScorecardNotification


AUTO_UPDATE_EVENT_CODE = "score_auto_update_completed"
AUTO_UPDATE_DOCUMENT_CATEGORY = "Auto Score Updates"


def _value(source: Any, *names: str, default: Any = "") -> Any:
    if source is None:
        return default
    if isinstance(source, dict):
        for name in names:
            value = source.get(name)
            if value not in (None, ""):
                return value
        return default
    for name in names:
        value = getattr(source, name, None)
        if value not in (None, ""):
            return value
    return default


def _display(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    return str(value)


def _percent(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{Decimal(str(value)):.2f}"
    except (InvalidOperation, ValueError):
        return _display(value)


def _display_user(user: Any) -> str:
    if not user:
        return ""
    for attr in ("email", "username", "user_name"):
        value = getattr(user, attr, "")
        if value:
            return str(value)
    return str(user)


def _resolve_user_candidate(candidate: Any) -> Any:
    if candidate is None:
        return None
    if getattr(candidate, "pk", None) is not None or getattr(candidate, "id", None) is not None:
        return candidate
    if isinstance(candidate, str) and "@" in candidate:
        try:
            return get_user_model().objects.filter(email__iexact=candidate.strip(), is_active=True).first()
        except Exception:
            return None
    return None


def _permission_codes_for_item(item: Any) -> tuple[str, ...]:
    score_type = _score_type(item).lower()
    if "basel" in score_type:
        return ("scorecard.reopen_basel_scores",)
    if "ifrs9" in score_type or "ifrs 9" in score_type:
        return ("scorecard.reopen_ifrs9_scores",)
    return ()


def _user_can_see_branch(user: Any, branch_name: str) -> bool:
    if getattr(user, "is_superuser", False):
        return True
    branch_name = (branch_name or "").strip()
    if not branch_name:
        return True

    get_accessible_branches = getattr(user, "get_accessible_branches", None)
    if not callable(get_accessible_branches):
        return False

    branches = get_accessible_branches()
    if hasattr(branches, "filter"):
        return branches.filter(branch_name__iexact=branch_name).exists()

    return any(
        (getattr(branch, "branch_name", "") or "").strip().lower() == branch_name.lower()
        for branch in branches
    )


def _collect_permission_users(updated_items: Iterable[Any]) -> list[Any]:
    items = list(updated_items or [])
    if not items:
        return []

    try:
        users = get_user_model().objects.filter(is_active=True).prefetch_related(
            "groups__permissions",
            "user_permissions",
            "scorecard_branch_access_entries__branch",
        )
    except Exception:
        return []

    recipients: list[Any] = []
    item_specs = [
        (_permission_codes_for_item(item), _branch_name(item))
        for item in items
    ]
    for user in users:
        if getattr(user, "is_superuser", False):
            recipients.append(user)
            continue
        for permission_codes, branch_name in item_specs:
            if not permission_codes:
                continue
            if not any(user.has_perm(permission_code) for permission_code in permission_codes):
                continue
            if not _user_can_see_branch(user, branch_name):
                continue
            recipients.append(user)
            break
    return recipients


def _customer_code(item: Any) -> str:
    customer = _value(item, "customer", default=None)
    if customer is not None:
        value = _value(customer, "customer_code", "customer_id", "customer_number", "id")
        if value not in (None, ""):
            return _display(value)
    return _display(_value(item, "customer_code", "customer_id", "customer_number"))


def _customer_name(item: Any) -> str:
    customer = _value(item, "customer", default=None)
    if customer is not None:
        value = _value(customer, "customer_name", "name", "full_name")
        if value not in (None, ""):
            return _display(value)
    return _display(_value(item, "customer_name", "customer"))


def _branch_name(item: Any) -> str:
    branch = _value(item, "branch", default=None)
    if branch is not None:
        return _display(_value(branch, "branch_name", "name", default=branch))
    return _display(_value(item, "branch_name", "branch"))


def _template_label(item: Any) -> str:
    template = _value(item, "template", default=None)
    if template is not None:
        code = _display(_value(template, "code", "template_code"))
        name = _display(_value(template, "name", "template_name"))
        return " - ".join(part for part in (code, name) if part) or _display(template)
    code = _display(_value(item, "template_code", "template"))
    name = _display(_value(item, "template_name"))
    return " - ".join(part for part in (code, name) if part)


def _template_parts(item: Any) -> tuple[str, str]:
    template = _value(item, "template", default=None)
    if template is not None and not isinstance(template, str):
        return (
            _display(_value(template, "code", "template_code")),
            _display(_value(template, "name", "template_name")),
        )
    return (
        _display(_value(item, "template_code", "template")),
        _display(_value(item, "template_name")),
    )


def _score_type(item: Any) -> str:
    value = _value(item, "score_type", "type", "scorecard_type")
    return _display(value) or item.__class__.__name__


def _changed_fields(item: Any) -> str:
    value = _value(item, "changed_fields", "changed_auto_fields", "changes")
    if isinstance(value, dict):
        return "; ".join(f"{key}: {detail}" for key, detail in value.items())
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(part) for part in value)
    return _display(value)


def _auto_update_report_row(item: Any, completed_at) -> list[str]:
    return [
        _score_type(item),
        _customer_code(item),
        _customer_name(item),
        _branch_name(item),
        _template_label(item),
        _percent(_value(item, "previous_weighted_score", "old_weighted_score", "previous_total_weighted_percent")),
        _percent(_value(item, "new_weighted_score", "weighted_score", "total_weighted_percent", "new_total_weighted_percent")),
        _display(_value(item, "previous_grade", "old_grade")),
        _display(_value(item, "new_grade", "grade", "final_grade")),
        _changed_fields(item),
        timezone.localtime(completed_at).strftime("%Y-%m-%d %H:%M:%S"),
        _display(_value(item, "version", "version_number")),
        _display_user(_value(item, "updated_by", "maker", "submitted_by", default=None)),
    ]


def _serialize_user_reference(candidate: Any) -> str:
    user = _resolve_user_candidate(candidate)
    if user is not None:
        return _display_user(user)
    return _display_user(candidate)


def serialize_auto_update_item(item: Any) -> dict[str, Any]:
    template_code, template_name = _template_parts(item)
    return {
        "score_type": _score_type(item),
        "customer_code": _customer_code(item),
        "customer_name": _customer_name(item),
        "branch_name": _branch_name(item),
        "template_code": template_code,
        "template_name": template_name,
        "previous_weighted_score": _percent(
            _value(item, "previous_weighted_score", "old_weighted_score", "previous_total_weighted_percent")
        ),
        "new_weighted_score": _percent(
            _value(item, "new_weighted_score", "weighted_score", "total_weighted_percent", "new_total_weighted_percent")
        ),
        "previous_grade": _display(_value(item, "previous_grade", "old_grade")),
        "new_grade": _display(_value(item, "new_grade", "grade", "final_grade")),
        "changed_fields": _changed_fields(item),
        "version": _display(_value(item, "version", "version_number")),
        "updated_by": _serialize_user_reference(
            _value(item, "updated_by", "maker", "submitted_by", default=None)
        ),
        "maker": _serialize_user_reference(_value(item, "maker", default=None)),
        "submitted_by": _serialize_user_reference(_value(item, "submitted_by", default=None)),
        "checker": _serialize_user_reference(_value(item, "checker", default=None)),
        "approved_by": _serialize_user_reference(_value(item, "approved_by", default=None)),
    }


def _collect_notification_users(updated_items: Iterable[Any], actor: Any = None) -> list[Any]:
    users: list[Any] = []
    try:
        user_model = get_user_model()
        users.extend(user_model.objects.filter(is_active=True, is_superuser=True))
    except Exception:
        pass
    users.extend(_collect_permission_users(updated_items))

    unique_users: list[Any] = []
    seen: set[Any] = set()
    for user in users:
        if not getattr(user, "is_active", True):
            continue
        key = getattr(user, "pk", None) or getattr(user, "id", None) or _display_user(user)
        if key in seen:
            continue
        seen.add(key)
        unique_users.append(user)
    return unique_users


def create_auto_update_report_document(
    updated_items: Iterable[Any],
    *,
    actor: Any = None,
    completed_at=None,
    summary: dict[str, Any] | None = None,
) -> tuple[ScorecardDocument, int]:
    completed_at = completed_at or timezone.now()
    rows = [_auto_update_report_row(item, completed_at) for item in updated_items]
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "Score Type",
            "Customer Code",
            "Customer Name",
            "Branch",
            "Template",
            "Previous Weighted Score %",
            "New Weighted Score %",
            "Previous Grade",
            "New Grade",
            "Changed Auto Fields",
            "Updated At",
            "Version",
            "Updated By",
        ]
    )
    writer.writerows(rows)
    if summary:
        writer.writerow([])
        writer.writerow(["Summary"])
        for key, value in summary.items():
            writer.writerow([key, value])

    filename = f"auto_score_updates_{timezone.localtime(completed_at):%Y%m%d_%H%M%S}.csv"
    document = ScorecardDocument(
        title=f"Auto score update list - {timezone.localtime(completed_at):%Y-%m-%d %H:%M:%S}",
        description="Customer scores updated automatically from the latest auto-populated source data.",
        file_name=filename,
        category=AUTO_UPDATE_DOCUMENT_CATEGORY,
        uploaded_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    document.file.save(filename, ContentFile(output.getvalue().encode("utf-8-sig")), save=True)
    return document, len(rows)


def notify_auto_update_completed(
    updated_items: Iterable[Any],
    *,
    actor: Any = None,
    completed_at=None,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    items = list(updated_items or [])
    if not items:
        return {"notified": False, "reason": "no_updates", "updated": 0}

    completed_at = completed_at or timezone.now()
    document, row_count = create_auto_update_report_document(
        items,
        actor=actor,
        completed_at=completed_at,
        summary=summary,
    )
    try:
        action_url = reverse("scorecard:scorecard_document_download", kwargs={"document_id": document.pk})
    except NoReverseMatch:
        action_url = f"/scorecard/documents/scorecard/{document.pk}/download/"

    recipients = _collect_notification_users(items, actor=actor)
    created = create_notifications_for_users(
        users=recipients,
        category=ScorecardNotification.CATEGORY_SCORING,
        level=ScorecardNotification.LEVEL_INFO,
        event_code=AUTO_UPDATE_EVENT_CODE,
        title="Auto score update completed",
        message=(
            f"Auto score refresh updated {row_count} customer score"
            f"{'' if row_count == 1 else 's'}. Download the attached update list to review the changes."
        ),
        actor=actor,
        action_url=action_url,
        action_label="Download Update List",
        metadata={
            "document_id": document.pk,
            "document_name": document.file_name,
            "updated_count": row_count,
            "summary": summary or {},
        },
    )
    return {
        "notified": bool(created),
        "updated": row_count,
        "document_id": document.pk,
        "document_name": document.file_name,
        "notifications_sent": len(created),
        "recipient_count": len(recipients),
    }
