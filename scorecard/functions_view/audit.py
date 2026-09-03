from __future__ import annotations

import hashlib
import logging
import re

from django.db import DatabaseError
from django.db.models import Q

from scorecard.models import ScorecardUserAuditTrail


logger = logging.getLogger(__name__)
SCORECARD_AUDIT_OBJECT_ID_SAFE_LENGTH = 50


SCORECARD_AUDIT_MODELS = (
    "ScorecardPermission",
    "ScorecardWorkflowApprovalSetting",
    "ScorecardBaselScore",
    "ScorecardIFRS9Score",
    "ScorecardIFRS9SupportingData",
    "ScorecardBaselTemplate",
    "ScorecardIFRS9Template",
    "ScorecardEvaluationDocument",
    "ScorecardDocumentLibrary",
    "ScorecardUpload",
    "ScorecardEmail",
    "ScorecardApi",
    "ScorecardCheckerApprovals",
    "ScorecardIFRS9Results",
    "ScorecardHistoricalScore",
)


def scorecard_audit_filter() -> Q:
    query = Q()
    for prefix in SCORECARD_AUDIT_MODELS:
        query |= Q(model_name__istartswith=prefix)
    return query


def _extract_branch_name(change_description: str) -> str:
    description = (change_description or "").strip()
    if not description:
        return ""

    match = re.search(r"Branch:\s*([^;.\n]+)", description, flags=re.IGNORECASE)
    if match:
        return (match.group(1) or "").strip()

    match = re.search(r"branch\s+'([^']+)'", description, flags=re.IGNORECASE)
    if match:
        return (match.group(1) or "").strip()

    for pattern in (
        r"New branch access:\s*(.+?)(?:\.|$)",
        r"New branches:\s*(.+?)(?:\.|$)",
        r"Branch access:\s*(.+?)(?:\.|$)",
    ):
        match = re.search(pattern, description, flags=re.IGNORECASE)
        if not match:
            continue
        value = (match.group(1) or "").strip()
        if not value:
            return ""
        parts = [item.strip() for item in value.split(",") if item.strip()]
        if len(parts) <= 1:
            return parts[0] if parts else ""
        return "Multiple branches"

    return ""


def _fit_audit_value(value, max_length):
    if value is None:
        return None
    cleaned = str(value).strip()
    if len(cleaned) <= max_length:
        return cleaned
    digest = hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:12]
    prefix_length = max(max_length - len(digest) - 1, 0)
    return f"{cleaned[:prefix_length]}_{digest}"[:max_length]


def log_scorecard_audit(
    user,
    model_name: str,
    action: str,
    object_id=None,
    change_description: str = "",
    branch_name: str = "",
) -> None:
    resolved_branch_name = (branch_name or "").strip() or _extract_branch_name(change_description)
    try:
        ScorecardUserAuditTrail.objects.create(
            user=user if getattr(user, "is_authenticated", False) else None,
            model_name=_fit_audit_value(model_name, 100) or "ScorecardAudit",
            action=_fit_audit_value(action, 50) or "unknown",
            object_id=_fit_audit_value(object_id, SCORECARD_AUDIT_OBJECT_ID_SAFE_LENGTH),
            branch_name=_fit_audit_value(resolved_branch_name, 150) or "",
            change_description=change_description or "",
        )
    except (DatabaseError, Exception) as exc:
        logger.warning("Scorecard audit logging skipped: %s", exc)


def _append_details(*parts: str) -> str:
    cleaned = [part.strip() for part in parts if part and str(part).strip()]
    return "; ".join(cleaned)


def log_basel_score_audit(user, action: str, evaluation, details: str = "") -> None:
    summary = _append_details(
        f"Customer: {evaluation.customer_name or '-'}",
        f"Customer ID: {evaluation.customer_id or '-'}",
        f"Branch: {evaluation.branch_name or '-'}",
        f"Template: {getattr(evaluation.template, 'code', '') or evaluation.template_section_name or 'Unassigned'}",
        details,
    )
    log_scorecard_audit(
        user,
        "ScorecardBaselScore",
        action,
        evaluation.pk,
        summary,
        branch_name=getattr(evaluation, "branch_name", "") or "",
    )


def log_ifrs9_score_audit(user, action: str, evaluation, details: str = "") -> None:
    summary = _append_details(
        f"Customer: {evaluation.customer_name or '-'}",
        f"Customer ID: {evaluation.customer_id or '-'}",
        f"Branch: {evaluation.branch_name or '-'}",
        f"Template: {getattr(evaluation.template, 'code', '') or evaluation.template_section_name or 'Unassigned'}",
        details,
    )
    log_scorecard_audit(
        user,
        "ScorecardIFRS9Score",
        action,
        evaluation.pk,
        summary,
        branch_name=getattr(evaluation, "branch_name", "") or "",
    )


def log_basel_template_audit(user, action: str, template, details: str = "") -> None:
    summary = _append_details(
        f"Template: {template.code or '-'}",
        f"Name: {template.name or '-'}",
        details,
    )
    log_scorecard_audit(user, "ScorecardBaselTemplate", action, template.pk, summary)


def log_ifrs9_template_audit(user, action: str, template, details: str = "") -> None:
    summary = _append_details(
        f"Template: {template.code or '-'}",
        f"Name: {template.name or '-'}",
        details,
    )
    log_scorecard_audit(user, "ScorecardIFRS9Template", action, template.pk, summary)


def log_evaluation_document_audit(user, action: str, document, source_key: str, details: str = "") -> None:
    evaluation = document.attribute_response.evaluation
    attribute = document.attribute_response.attribute
    option = document.attribute_response.option
    summary = _append_details(
        f"Source: {'Basel Score Form' if source_key == 'basel' else 'IFRS9 Score Form'}",
        f"Customer: {evaluation.customer_name or '-'}",
        f"Customer ID: {evaluation.customer_id or '-'}",
        f"Branch: {evaluation.branch_name or '-'}",
        f"Attribute: {(attribute.group_label or f'{attribute.code} - {attribute.label}') if attribute else '-'}",
        f"Option: {option.label if option else '-'}",
        f"File: {document.file_name or '-'}",
        details,
    )
    log_scorecard_audit(
        user,
        "ScorecardEvaluationDocument",
        action,
        document.pk,
        summary,
        branch_name=getattr(evaluation, "branch_name", "") or "",
    )


def log_scorecard_document_audit(user, action: str, document, details: str = "") -> None:
    summary = _append_details(
        f"Title: {document.title or '-'}",
        f"Category: {document.category or '-'}",
        f"File: {document.file_name or '-'}",
        details,
    )
    log_scorecard_audit(user, "ScorecardDocumentLibrary", action, document.pk, summary)


def log_upload_audit(user, action: str, *, upload_kind: str, file_name: str = "", details: str = "", object_id=None) -> None:
    summary = _append_details(
        f"Upload kind: {upload_kind}",
        f"File: {file_name or '-'}",
        details,
    )
    log_scorecard_audit(user, "ScorecardUpload", action, object_id=object_id, change_description=summary)


def log_email_audit(user, action: str, details: str = "", object_id=None) -> None:
    log_scorecard_audit(user, "ScorecardEmail", action, object_id=object_id, change_description=details)


def log_api_audit(user, action: str, details: str = "", object_id=None) -> None:
    log_scorecard_audit(user, "ScorecardApi", action, object_id=object_id, change_description=details)



def log_ifrs9_results_audit(user, action: str, details: str = "", object_id=None, branch_name: str = "") -> None:
    log_scorecard_audit(
        user,
        "ScorecardIFRS9Results",
        action,
        object_id=object_id,
        change_description=details,
        branch_name=branch_name,
    )


def log_historical_score_audit(user, action: str, details: str = "", object_id=None, branch_name: str = "") -> None:
    log_scorecard_audit(
        user,
        "ScorecardHistoricalScore",
        action,
        object_id=object_id,
        change_description=details,
        branch_name=branch_name,
    )
