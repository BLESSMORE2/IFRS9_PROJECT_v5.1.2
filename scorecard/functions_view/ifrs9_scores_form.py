from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Case, CharField, Count, DecimalField, IntegerField, Max, OuterRef, Q, Subquery, When
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from urllib.parse import urlencode

from scorecard.functions_view.main_customer_lookup import (
    get_request_branch_names,
    get_request_branch_scope,
    is_all_branches_selected,
    get_main_customer,
    get_main_customer_any_branch,
    resolve_branch_context,
    search_main_customers,
)
from scorecard.functions_view.customers import get_active_exposure_customer_codes_for_branch_scope
from scorecard.functions_view.scorecard_autofill import (
    build_ifrs9_autofill,
    compare_profile_snapshots,
)
from scorecard.functions_view.notifications import (
    notify_ifrs9_approved,
    notify_ifrs9_submitted,
)
from scorecard.functions_view.audit import log_ifrs9_score_audit
from scorecard.workflow_approval import (
    is_counterpart_completion_enforced,
    is_cross_branch_duplicate_scoring_prevented,
    should_auto_approve_scorecard_workflow,
)
from scorecard.models import (
    BankBranch,
    CreditEvaluation,
    IFRS9Attribute,
    IFRS9AttributeResponse,
    IFRS9AttributeResponseDocument,
    IFRS9Evaluation,
    IFRS9EvaluationHistory,
    IFRS9EvaluationVersion,
    IFRS9EvaluationVersionAttributeResponse,
    IFRS9EvaluationVersionDriverScore,
    IFRS9EvaluationVersionSectionScore,
    IFRS9EvaluationWorkflowHistory,
    IFRS9GradeBand,
    IFRS9Option,
    IFRS9RiskDriver,
    IFRS9RiskDriverScore,
    IFRS9Section,
    IFRS9SectionScore,
    IFRS9ScoreSheetTemplate,
    IFRS9TemplateVersion,
)

# Import the _build_configuration function from ifrs9_score_config
from scorecard.functions_view.ifrs9_score_config import _build_configuration, _field_name_for_attribute


LIST_PAGE_SIZE_OPTIONS = (20, 50, 100)
BULK_WRITE_BATCH_SIZE = 500
SUBMITTED_LIST_SUMMARY_CACHE_TTL_SECONDS = 120


def _can_auto_approve_ifrs9_submission(user) -> bool:
    return should_auto_approve_scorecard_workflow(
        user,
        "ifrs9_scores",
        "scorecard.review_ifrs9_scores",
    )


def _finalize_ifrs9_auto_approval(
    evaluation: IFRS9Evaluation,
    acting_user,
    *,
    old_status: str,
    comments: str,
) -> None:
    approval_time = timezone.now()
    latest_unapproved_version = (
        evaluation.versions.filter(is_approved=False).order_by("-version_number").first()
    )

    if latest_unapproved_version:
        evaluation.approved_weighted_percent = latest_unapproved_version.total_weighted_percent
        evaluation.approved_grade = ""
        evaluation.total_weighted_percent = latest_unapproved_version.total_weighted_percent
        evaluation.final_grade = ""
        evaluation.total_raw_score = latest_unapproved_version.total_raw_score

        latest_unapproved_version.is_approved = True
        latest_unapproved_version.approved_at = approval_time
        latest_unapproved_version.approved_by = acting_user
        latest_unapproved_version.save()

        # Keep older unapproved/returned versions for a complete customer version history.
    else:
        evaluation.approved_weighted_percent = evaluation.total_weighted_percent
        evaluation.approved_grade = ""

    evaluation.status = "approved"
    evaluation.approved_by = acting_user
    evaluation.approved_at = approval_time
    evaluation.save()

    IFRS9EvaluationWorkflowHistory.objects.create(
        evaluation=evaluation,
        action="approved",
        from_status=old_status,
        to_status="approved",
        performed_by=acting_user,
        comments=comments,
    )


def _normalize_list_page_size(raw_value, default: int = 20) -> int:
    try:
        page_size = int(raw_value)
    except (TypeError, ValueError):
        return default
    return page_size if page_size in LIST_PAGE_SIZE_OPTIONS else default


def _build_list_query_string(
    *,
    search_query: str,
    page_size: int,
    status_filter: str = "",
    exposure_filter: str = "",
) -> str:
    params = {}
    if search_query:
        params["q"] = search_query
    if status_filter:
        params["status"] = status_filter
    if exposure_filter:
        params["exposure"] = exposure_filter
    if page_size != LIST_PAGE_SIZE_OPTIONS[0]:
        params["page_size"] = page_size
    return urlencode(params)


def _paginate_list_queryset(request: HttpRequest, queryset, *, default_page_size: int = 20):
    search_query = (request.GET.get("q") or "").strip()
    page_size = _normalize_list_page_size(request.GET.get("page_size"), default=default_page_size)
    paginator = Paginator(queryset, page_size)
    page_obj = paginator.get_page(request.GET.get("p") or "1")
    return page_obj, search_query, page_size


VALID_EXPOSURE_FILTERS = {"loan", "overdraft", "active"}


def _get_exposure_filter(request: HttpRequest) -> str:
    exposure_filter = (request.GET.get("exposure") or "").strip().lower()
    return exposure_filter if exposure_filter in VALID_EXPOSURE_FILTERS else ""


def _apply_active_exposure_filter(request: HttpRequest, evaluations, exposure_filter: str):
    if not exposure_filter:
        return evaluations
    active_customer_codes = get_active_exposure_customer_codes_for_branch_scope(
        get_request_branch_scope(request),
        exposure_type=exposure_filter,
    )
    if not active_customer_codes:
        return evaluations.none()
    return evaluations.filter(customer_id__in=active_customer_codes)


def _submitted_summary_cache_key(
    prefix: str,
    request: HttpRequest,
    search_query: str,
    status_filter: str,
    exposure_filter: str = "",
) -> str:
    branch_names = get_request_branch_names(request)
    if is_all_branches_selected(request):
        branch_scope_token = "all-assigned:" + "|".join(sorted(branch_names))
    elif branch_names:
        branch_scope_token = "single:" + branch_names[0]
    else:
        current_branch = resolve_branch_context(request)
        branch_scope_token = (
            f"single:{getattr(current_branch, 'branch_name', '') or getattr(current_branch, 'branch_code', '') or 'none'}"
        )
    return (
        f"scorecard:{prefix}:submitted_summary:"
        f"{branch_scope_token}:{search_query.strip().lower()}:"
        f"{(status_filter or 'all').strip().lower()}:{(exposure_filter or 'all').strip().lower()}"
    )


def _get_submitted_status_counts(
    queryset,
    request: HttpRequest,
    search_query: str,
    status_filter: str,
    prefix: str,
    exposure_filter: str = "",
) -> dict[str, int]:
    cache_key = _submitted_summary_cache_key(prefix, request, search_query, status_filter, exposure_filter)
    cached_counts = cache.get(cache_key)
    if cached_counts is not None:
        return cached_counts

    counts = queryset.aggregate(
        submitted_total=Count("id"),
        submitted_pending_total=Count("id", filter=Q(status="submitted")),
        submitted_approved_total=Count("id", filter=Q(status="approved")),
        submitted_returned_total=Count("id", filter=Q(status="returned")),
    )
    cache.set(cache_key, counts, SUBMITTED_LIST_SUMMARY_CACHE_TTL_SECONDS)
    return counts



COUNTERPART_ENFORCEMENT_STATUSES = ("submitted", "approved", "returned", "completed")


def _get_pending_counterpart_requirements(user) -> list[dict[str, Any]]:
    if not getattr(user, "is_authenticated", False):
        return []

    candidates: list[dict[str, Any]] = []
    basel_rows = CreditEvaluation.objects.filter(
        submitted_by=user,
        status__in=COUNTERPART_ENFORCEMENT_STATUSES,
    ).exclude(status="cancelled").values(
        "customer_id",
        "customer_name",
        "branch_name",
        "submitted_at",
        "created_at",
    )
    ifrs9_rows = IFRS9Evaluation.objects.filter(
        submitted_by=user,
        status__in=COUNTERPART_ENFORCEMENT_STATUSES,
    ).exclude(status="cancelled").values(
        "customer_id",
        "customer_name",
        "branch_name",
        "submitted_at",
        "created_at",
    )

    for row in basel_rows:
        candidates.append(
            {
                "entered_side": "basel",
                "missing_side": "ifrs9",
                "customer_id": row["customer_id"],
                "customer_name": row["customer_name"],
                "branch_name": row["branch_name"],
                "timestamp": row["submitted_at"] or row["created_at"],
            }
        )
    for row in ifrs9_rows:
        candidates.append(
            {
                "entered_side": "ifrs9",
                "missing_side": "basel",
                "customer_id": row["customer_id"],
                "customer_name": row["customer_name"],
                "branch_name": row["branch_name"],
                "timestamp": row["submitted_at"] or row["created_at"],
            }
        )

    candidates.sort(key=lambda item: item["timestamp"], reverse=True)

    pending_requirements: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for candidate in candidates:
        if candidate["missing_side"] == "ifrs9":
            counterpart_exists = IFRS9Evaluation.objects.filter(
                customer_id=candidate["customer_id"],
                branch_name=candidate["branch_name"],
            ).exclude(status="cancelled").exists()
        else:
            counterpart_exists = CreditEvaluation.objects.filter(
                customer_id=candidate["customer_id"],
                branch_name=candidate["branch_name"],
            ).exclude(status="cancelled").exists()
        if not counterpart_exists:
            key = (
                candidate["missing_side"],
                candidate["customer_id"],
                candidate["branch_name"],
            )
            if key not in seen_keys:
                seen_keys.add(key)
                pending_requirements.append(candidate)
    return pending_requirements


def _maybe_block_new_customer_for_counterpart(
    request: HttpRequest,
    *,
    current_side: str,
    current_customer_id: str,
    current_branch_name: str,
) -> HttpResponse | None:
    if not is_counterpart_completion_enforced():
        return None

    pending_requirements = _get_pending_counterpart_requirements(request.user)
    if not pending_requirements:
        return None

    remaining_requirements = [
        requirement
        for requirement in pending_requirements
        if not (
            requirement["missing_side"] == current_side
            and requirement["customer_id"] == current_customer_id
            and requirement["branch_name"] == current_branch_name
        )
    ]
    if not remaining_requirements:
        return None

    first_requirement = remaining_requirements[0]
    requirement_messages: list[str] = []
    for requirement in remaining_requirements:
        missing_label = "IFRS9 score form" if requirement["missing_side"] == "ifrs9" else "Basel II score"
        entered_label = "Basel II score" if requirement["entered_side"] == "basel" else "IFRS9 score form"
        customer_name = requirement["customer_name"] or requirement["customer_id"]
        requirement_messages.append(
            f"{customer_name} ({requirement['customer_id']}) at branch '{requirement['branch_name']}' needs the missing {missing_label} to match the previously submitted {entered_label}"
        )
    messages.warning(
        request,
        "Complete the outstanding counterpart scores before starting another customer: "
        + "; ".join(requirement_messages)
        + ". You are being redirected to the first missing counterpart.",
    )
    target = (
        "scorecard:ifrs9_scores_template_select"
        if first_requirement["missing_side"] == "ifrs9"
        else "scorecard:basel_scores_template_select"
    )
    return redirect(target)


def _format_existing_ifrs9_score_timestamp(evaluation: IFRS9Evaluation) -> str:
    timestamp = (
        evaluation.approved_at
        or evaluation.submitted_at
        or evaluation.updated_at
        or evaluation.created_at
    )
    if not timestamp:
        return ""
    if timezone.is_aware(timestamp):
        timestamp = timezone.localtime(timestamp)
    return timestamp.strftime("%Y-%m-%d %H:%M")



def _build_existing_ifrs9_score_payload(
    evaluation: IFRS9Evaluation,
    *,
    action_mode: str,
    preview_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "exists": True,
        "evaluation_id": evaluation.id,
        "customer_name": evaluation.customer_name,
        "branch_name": evaluation.branch_name,
        "timestamp_label": _format_existing_ifrs9_score_timestamp(evaluation),
        "action_mode": action_mode,
        "edit_url": f"/scorecard/ifrs9-scores/edit/{evaluation.id}/",
        "view_url": f"/scorecard/ifrs9-scores/view/{evaluation.id}/?preview=1",
        "preview_records": preview_records or [],
    }



def _get_locked_existing_ifrs9_score_payload(
    customer_id: str,
    *,
    current_branch_name: str | None = None,
) -> dict[str, Any] | None:
    if not is_cross_branch_duplicate_scoring_prevented():
        return None

    evaluations = IFRS9Evaluation.objects.filter(customer_id=customer_id).exclude(status="cancelled")
    if current_branch_name:
        evaluations = evaluations.exclude(branch_name=current_branch_name)

    matching_evaluations = list(evaluations.order_by("-approved_at", "-updated_at", "-created_at"))
    if not matching_evaluations:
        return None

    preview_records = [
        {
            "evaluation_id": item.id,
            "branch_name": item.branch_name,
            "timestamp_label": _format_existing_ifrs9_score_timestamp(item),
            "view_url": f"/scorecard/ifrs9-scores/view/{item.id}/?preview=1",
        }
        for item in matching_evaluations
    ]
    return _build_existing_ifrs9_score_payload(
        matching_evaluations[0],
        action_mode="view",
        preview_records=preview_records,
    )



def _get_branch_filtered_ifrs9_evaluations(
    request: HttpRequest,
    *,
    statuses: list[str] | tuple[str, ...] | None = None,
    exclude_statuses: list[str] | tuple[str, ...] | None = None,
):
    evaluations = IFRS9Evaluation.objects.select_related("template")

    if statuses:
        evaluations = evaluations.filter(status__in=statuses)
    if exclude_statuses:
        evaluations = evaluations.exclude(status__in=exclude_statuses)

    branch_names = get_request_branch_names(request)
    if branch_names:
        evaluations = evaluations.filter(branch_name__in=branch_names)
    elif not request.user.is_superuser:
        return evaluations.none()

    return evaluations


def _redirect_if_all_branches_selected_for_score_entry(request: HttpRequest):
    if not is_all_branches_selected(request):
        return None
    messages.error(
        request,
        "Choose one specific branch before opening an IFRS9 score form. All Branches mode is for combined views only.",
    )
    return redirect("scorecard:ifrs9_scores_submitted_list")


def _build_ifrs9_autofill_payload(
    request: HttpRequest,
    template: IFRS9ScoreSheetTemplate,
    attributes_by_driver: Dict[int, List[IFRS9Attribute]],
    *,
    customer_ref_code: str = "",
    branch_code: str = "",
    branch_name: str = "",
) -> dict[str, object]:
    branch = resolve_branch_context(request)
    if not branch or not customer_ref_code:
        return {
            "attribute_values": {},
            "applied_labels": [],
            "applied_attribute_ids": [],
            "missing_required_labels": [],
            "missing_required_attribute_ids": [],
            "profile_snapshot": {},
        }

    customer = get_main_customer_any_branch(customer_ref_code)
    result = build_ifrs9_autofill(template.code, customer, attributes_by_driver)
    return {
        "attribute_values": result.attribute_values,
        "applied_labels": result.applied_labels,
        "applied_attribute_ids": result.applied_attribute_ids,
        "missing_required_labels": result.missing_required_labels,
        "missing_required_attribute_ids": result.missing_required_attribute_ids,
        "profile_snapshot": result.profile_snapshot,
        "customer_name": customer.customer_name if customer else "",
        "branch_name": branch.branch_name,
        "branch_code": branch.branch_code,
    }


def _empty_autofill_payload() -> dict[str, object]:
    return {
        "attribute_values": {},
        "applied_labels": [],
        "applied_attribute_ids": [],
        "missing_required_labels": [],
        "missing_required_attribute_ids": [],
        "profile_snapshot": {},
        "customer_name": "",
        "branch_name": "",
    }


def _flatten_attributes(attributes_by_driver: Dict[int, List[IFRS9Attribute]]) -> list[IFRS9Attribute]:
    return [attribute for attrs in attributes_by_driver.values() for attribute in attrs]


def _build_saved_autofill_metadata(
    autofill_payload: dict[str, object],
    attributes_by_driver: Dict[int, List[IFRS9Attribute]],
    submitted_values: Dict[int, str | None] | None = None,
) -> dict[str, object]:
    attributes = _flatten_attributes(attributes_by_driver)
    labels_by_id = {attribute.id: attribute.label for attribute in attributes}
    suggested_values = {
        int(attribute_id): str(option_id)
        for attribute_id, option_id in (autofill_payload.get("attribute_values", {}) or {}).items()
    }
    suggested_ids = sorted(suggested_values.keys())

    if submitted_values is None:
        applied_ids = suggested_ids
        overridden_ids: list[int] = []
        current_missing_ids = list(autofill_payload.get("missing_required_attribute_ids", []) or [])
    else:
        applied_ids = [
            attribute_id
            for attribute_id, option_id in suggested_values.items()
            if str(submitted_values.get(attribute_id, "")) == option_id
        ]
        overridden_ids = [
            attribute_id
            for attribute_id in suggested_ids
            if attribute_id not in applied_ids
        ]
        current_missing_ids = [
            attribute.id
            for attribute in attributes
            if attribute.is_required and submitted_values.get(attribute.id) is None
        ]

    return {
        "suggested_attribute_ids": suggested_ids,
        "suggested_labels": [labels_by_id.get(attribute_id, str(attribute_id)) for attribute_id in suggested_ids],
        "applied_attribute_ids": applied_ids,
        "applied_labels": [labels_by_id.get(attribute_id, str(attribute_id)) for attribute_id in applied_ids],
        "overridden_attribute_ids": overridden_ids,
        "overridden_labels": [labels_by_id.get(attribute_id, str(attribute_id)) for attribute_id in overridden_ids],
        "missing_required_attribute_ids": list(autofill_payload.get("missing_required_attribute_ids", []) or []),
        "missing_required_labels": list(autofill_payload.get("missing_required_labels", []) or []),
        "current_missing_required_ids": current_missing_ids,
        "current_missing_required_labels": [labels_by_id.get(attribute_id, str(attribute_id)) for attribute_id in current_missing_ids],
        "profile_snapshot": dict(autofill_payload.get("profile_snapshot", {}) or {}),
        "customer_name": autofill_payload.get("customer_name", ""),
        "branch_name": autofill_payload.get("branch_name", ""),
    }


def _build_profile_change_rows(
    previous_metadata: dict[str, object] | None,
    current_autofill_payload: dict[str, object],
) -> list[dict[str, str]]:
    previous_snapshot = (previous_metadata or {}).get("profile_snapshot", {}) if isinstance(previous_metadata, dict) else {}
    current_snapshot = current_autofill_payload.get("profile_snapshot", {}) or {}
    return compare_profile_snapshots(previous_snapshot, current_snapshot)


def _get_active_autofill_metadata(
    evaluation: IFRS9Evaluation,
    *,
    preferred_version: IFRS9EvaluationVersion | None = None,
    fallback_version: IFRS9EvaluationVersion | None = None,
) -> dict[str, object]:
    if preferred_version and preferred_version.autofill_metadata:
        return preferred_version.autofill_metadata
    if fallback_version and fallback_version.autofill_metadata:
        return fallback_version.autofill_metadata
    return evaluation.autofill_metadata or {}


def _build_configuration_from_version(
    template_version: IFRS9TemplateVersion,
) -> Tuple[List[IFRS9Section], Dict[int, List[IFRS9RiskDriver]], Dict[int, List[IFRS9Attribute]]]:
    """
    Builds configuration from a template version snapshot.
    This ensures IFRS9 score forms use the approved template structure, not pending changes.
    
    Args:
        template_version: The IFRS9TemplateVersion snapshot to build from
    
    Returns:
        sections: ordered list of sections (from version snapshot)
        drivers_by_section: mapping section.id -> list of IFRS9RiskDriver (from version snapshot)
        attributes_by_driver: mapping driver.id -> list of IFRS9Attribute (from version snapshot)
    """
    sections = []
    drivers_by_section: Dict[int, List[IFRS9RiskDriver]] = {}
    attributes_by_driver: Dict[int, List[IFRS9Attribute]] = {}
    
    # Build structure from version snapshots
    for section_version in template_version.sections.all().order_by('display_order'):
        section = section_version.section
        sections.append(section)
        drivers_by_section[section.id] = []
        
        for driver_version in section_version.risk_drivers.all().order_by('display_order'):
            driver = driver_version.risk_driver
            drivers_by_section[section.id].append(driver)
            attributes_by_driver[driver.id] = []
            
            for attr_version in driver_version.attributes.all().order_by('display_order'):
                attribute = attr_version.attribute
                attributes_by_driver[driver.id].append(attribute)
    
    return sections, drivers_by_section, attributes_by_driver


def _get_ifrs9_template_category(template: IFRS9ScoreSheetTemplate) -> str | None:
    """Infer the IFRS9 PD scorecard category from template metadata."""
    code = (template.code or "").upper()
    text = " ".join(
        part for part in [template.code, template.name, template.description] if part
    ).lower()

    category_checks = [
        ("consumer_loans", ("CONSUMER",), ("consumer", "salary based")),
        ("corporate", ("CORPORATE",), ("corporate",)),
        ("retail", ("RETAIL",), ("retail",)),
        ("farming", ("FARMING", "FARM"), ("farming", "farmer")),
        ("mfinance", ("MFINANCE", "MICROFINANCE"), ("microfinance", "mfinance")),
        ("local_authorities", ("LOCALAUTHORITIES", "LOCAL_AUTHORITIES"), ("local authorities", "local authority")),
        ("schools", ("SCHOOLS", "SCHOOL"), ("schools", "school")),
        ("tertiary", ("TERTIARY",), ("tertiary", "institution")),
    ]

    for category, code_tokens, text_tokens in category_checks:
        if any(token in code for token in code_tokens) or any(token in text for token in text_tokens):
            return category
    return None


def _get_active_ifrs9_template_in_same_category(
    template: IFRS9ScoreSheetTemplate,
) -> IFRS9ScoreSheetTemplate | None:
    """Find the newest active IFRS9 template in the same business category."""
    category = _get_ifrs9_template_category(template)
    if not category:
        return None

    candidates = (
        IFRS9ScoreSheetTemplate.objects.filter(is_active=True, status="approved")
        .exclude(id=template.id)
        .order_by("-updated_at", "-id")
    )
    for candidate in candidates:
        if _get_ifrs9_template_category(candidate) == category:
            return candidate
    return None


def _get_ifrs9_template_label(evaluation: IFRS9Evaluation) -> tuple[str, str]:
    if evaluation.template_id and evaluation.template is not None:
        return evaluation.template.code, evaluation.template.name
    return "EXTERNAL_IMPORT", evaluation.template_section_name or "Template not assigned"


def _can_manage_ifrs9_template(request: HttpRequest, evaluation: IFRS9Evaluation) -> bool:
    if evaluation.status == "submitted":
        return False
    return evaluation.can_be_edited_by(request.user)


def _reset_ifrs9_evaluation_for_template(
    evaluation: IFRS9Evaluation,
    template: IFRS9ScoreSheetTemplate,
    *,
    performed_by,
) -> str:
    previous_template_code = evaluation.template.code if evaluation.template_id and evaluation.template else ""
    previous_template_name = evaluation.template.name if evaluation.template_id and evaluation.template else ""
    previous_score = evaluation.total_weighted_percent
    previous_grade = evaluation.final_grade
    previous_raw_score = evaluation.total_raw_score
    previous_status = evaluation.status
    had_existing_template = bool(evaluation.template_id)
    preserved_import_version_number = ""

    if not had_existing_template and not evaluation.versions.exists():
        import_version = _create_ifrs9_evaluation_version(
            evaluation,
            version_number=1,
            user=performed_by,
            change_description="Imported IFRS9 score preserved before template assignment.",
        )
        preserved_import_version_number = str(import_version.version_number)

    metadata = dict(evaluation.autofill_metadata or {})
    metadata["template_assignment"] = {
        "previous_template_code": previous_template_code,
        "previous_template_name": previous_template_name,
        "new_template_code": template.code,
        "new_template_name": template.name,
        "changed_at": timezone.now().isoformat(),
        "changed_by": getattr(performed_by, "username", "") or getattr(performed_by, "email", ""),
        "previous_weighted_percent": str(previous_score) if previous_score is not None else "",
        "previous_grade": previous_grade or "",
        "previous_raw_score": str(previous_raw_score) if previous_raw_score is not None else "",
        "preserved_import_version_number": preserved_import_version_number,
    }

    evaluation.template = template
    evaluation.autofill_metadata = metadata
    evaluation.save()

    IFRS9EvaluationWorkflowHistory.objects.create(
        evaluation=evaluation,
        action="reassigned",
        from_status=previous_status,
        to_status=previous_status,
        performed_by=performed_by,
        comments=(
            f"{'Changed' if had_existing_template else 'Assigned'} template to '{template.code}'. "
            "Existing score, grade, status, and evaluation data were preserved."
        ),
    )
    return previous_template_code


def _normalize_mapping_text(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def _get_ifrs9_attribute_match_keys(attribute: IFRS9Attribute) -> List[Tuple[str, ...]]:
    section = getattr(attribute.risk_driver, "section", None)
    keys = [
        (
            _normalize_mapping_text(getattr(section, "code", "")),
            _normalize_mapping_text(getattr(section, "name", "")),
            _normalize_mapping_text(attribute.risk_driver.code),
            _normalize_mapping_text(attribute.risk_driver.name),
            _normalize_mapping_text(attribute.code),
            _normalize_mapping_text(attribute.label),
            _normalize_mapping_text(attribute.group_label),
        ),
        (
            _normalize_mapping_text(getattr(section, "code", "")),
            _normalize_mapping_text(attribute.risk_driver.code),
            _normalize_mapping_text(attribute.code),
        ),
        (
            _normalize_mapping_text(attribute.risk_driver.code),
            _normalize_mapping_text(attribute.code),
        ),
        (
            _normalize_mapping_text(attribute.label),
            _normalize_mapping_text(attribute.group_label),
        ),
    ]
    return list(dict.fromkeys(keys))


def _find_matching_ifrs9_option(
    new_attribute: IFRS9Attribute,
    previous_option: IFRS9Option | None,
) -> IFRS9Option | None:
    if previous_option is None:
        return None

    new_options = list(new_attribute.options.all())
    if not new_options:
        return None

    previous_label = _normalize_mapping_text(previous_option.label)
    previous_value = _normalize_mapping_text(previous_option.value)
    previous_score = Decimal(str(previous_option.allocated_score or 0))

    for candidate in new_options:
        if previous_label and _normalize_mapping_text(candidate.label) == previous_label:
            return candidate

    for candidate in new_options:
        if previous_value and _normalize_mapping_text(candidate.value) == previous_value:
            return candidate

    for candidate in new_options:
        if Decimal(str(candidate.allocated_score or 0)) == previous_score:
            return candidate

    return None


def _map_ifrs9_responses_to_replacement_template(
    attribute_responses_list: List[IFRS9AttributeResponse],
    replacement_template: IFRS9ScoreSheetTemplate,
) -> Tuple[Dict[int, str], Dict[int, object]]:
    """Re-map existing IFRS9 responses onto a replacement template."""
    replacement_attributes = list(
        IFRS9Attribute.objects.filter(risk_driver__section__template=replacement_template)
        .select_related("risk_driver__section")
        .prefetch_related("options")
        .order_by("display_order", "id")
    )

    attribute_lookup: Dict[Tuple[str, ...], IFRS9Attribute] = {}
    for attribute in replacement_attributes:
        for key in _get_ifrs9_attribute_match_keys(attribute):
            attribute_lookup.setdefault(key, attribute)

    mapped_values: Dict[int, str] = {}
    mapped_responses: Dict[int, object] = {}

    for response in attribute_responses_list:
        new_attribute = None
        for key in _get_ifrs9_attribute_match_keys(response.attribute):
            new_attribute = attribute_lookup.get(key)
            if new_attribute is not None:
                break

        if new_attribute is None:
            continue

        if _is_ifrs9_checkbox_attribute(new_attribute):
            selected_ids = _parse_ifrs9_checkbox_value(response.raw_value)
            matched_options = []
            for selected_id in selected_ids:
                source_option = response.attribute.options.filter(id=selected_id).first()
                if source_option is None:
                    continue
                matched_option = _find_matching_ifrs9_option(new_attribute, source_option)
                if matched_option is not None:
                    matched_options.append(matched_option)
            if matched_options:
                mapped_values[new_attribute.id] = [str(option.id) for option in matched_options]
                mapped_responses[new_attribute.id] = SimpleNamespace(
                    option=SimpleNamespace(id=matched_options[0].id),
                    raw_value=_serialize_ifrs9_checkbox_value([option.id for option in matched_options]),
                    allocated_score=response.allocated_score,
                    documents=response.documents.all(),
                )
        elif response.option_id:
            matched_option = _find_matching_ifrs9_option(new_attribute, response.option)
            if matched_option is None:
                continue

            mapped_values[new_attribute.id] = str(matched_option.id)
            mapped_responses[new_attribute.id] = SimpleNamespace(
                option=SimpleNamespace(id=matched_option.id),
                raw_value=str(matched_option.id),
                allocated_score=response.allocated_score,
                documents=response.documents.all(),
            )
        elif response.raw_value == "" and response.option_id is None:
            mapped_values[new_attribute.id] = ""

    return mapped_values, mapped_responses


def _create_ifrs9_evaluation_version(
    evaluation: IFRS9Evaluation,
    version_number: int,
    user=None,
    change_description: str = "",
    total_weighted_percent=None,
    final_grade=None,
    total_raw_score=None,
    attribute_version_payload: Optional[list[dict[str, Any]]] = None,
    driver_version_payload: Optional[list[dict[str, Any]]] = None,
    section_version_payload: Optional[list[dict[str, Any]]] = None,
) -> IFRS9EvaluationVersion:
    """
    Create a complete snapshot of an IFRS9 evaluation at a specific point in time.
    Saves all attribute responses, driver scores, section scores, and summary data.
    
    Args:
        evaluation: The IFRS9Evaluation to snapshot
        version_number: Version number (1 for initial, increments on edits)
        user: User who created this version
        change_description: Description of what changed in this version
        total_weighted_percent: Optional - use this score instead of evaluation.total_weighted_percent
        final_grade: Optional - use this grade instead of evaluation.final_grade
        total_raw_score: Optional - use this raw score instead of evaluation.total_raw_score
    
    Returns:
        The created IFRS9EvaluationVersion instance
    """
    with transaction.atomic():
        if user is None:
            user = getattr(evaluation, "submitted_by", None) or getattr(evaluation, "maker", None)

        # Use provided scores if available, otherwise use evaluation scores
        version_weighted_percent = total_weighted_percent if total_weighted_percent is not None else evaluation.total_weighted_percent
        version_grade = final_grade if final_grade is not None else evaluation.final_grade
        version_raw_score = total_raw_score if total_raw_score is not None else evaluation.total_raw_score
        
        # Create version record with current evaluation values (or provided scores)
        try:
            version, created = IFRS9EvaluationVersion.objects.get_or_create(
                evaluation=evaluation,
                version_number=version_number,
                defaults={
                    "total_raw_score": version_raw_score,
                    "total_weighted_percent": version_weighted_percent,
                    "final_grade": version_grade,
                    "created_by": user,
                    "change_description": change_description,
                    "autofill_metadata": evaluation.autofill_metadata or {},
                },
            )
        except IntegrityError:
            version = IFRS9EvaluationVersion.objects.get(
                evaluation=evaluation,
                version_number=version_number,
            )
            created = False

        if not created:
            if user is not None and version.created_by_id is None:
                version.created_by = user
                version.save(update_fields=["created_by"])
            return version

        if attribute_version_payload is None:
            attribute_version_payload = list(
                evaluation.attribute_responses.values(
                    "attribute_id",
                    "option_id",
                    "raw_value",
                    "allocated_score",
                )
            )
        attribute_version_rows = [
            IFRS9EvaluationVersionAttributeResponse(
                version=version,
                attribute_id=row["attribute_id"],
                option_id=row["option_id"],
                raw_value=row["raw_value"],
                allocated_score=row["allocated_score"],
            )
            for row in attribute_version_payload
        ]
        if attribute_version_rows:
            IFRS9EvaluationVersionAttributeResponse.objects.bulk_create(
                attribute_version_rows,
                batch_size=BULK_WRITE_BATCH_SIZE,
            )

        if driver_version_payload is None:
            driver_version_payload = list(
                evaluation.driver_scores.values(
                    "risk_driver_id",
                    "raw_score",
                    "weighted_percent",
                    "proof",
                )
            )
        driver_version_rows = [
            IFRS9EvaluationVersionDriverScore(
                version=version,
                risk_driver_id=row["risk_driver_id"],
                raw_score=row["raw_score"],
                weighted_percent=row["weighted_percent"],
                proof=row["proof"],
            )
            for row in driver_version_payload
        ]
        if driver_version_rows:
            IFRS9EvaluationVersionDriverScore.objects.bulk_create(
                driver_version_rows,
                batch_size=BULK_WRITE_BATCH_SIZE,
            )

        if section_version_payload is None:
            section_version_payload = list(
                evaluation.section_scores.values(
                    "section_id",
                    "raw_score",
                    "weighted_percent",
                )
            )
        section_version_rows = [
            IFRS9EvaluationVersionSectionScore(
                version=version,
                section_id=row["section_id"],
                raw_score=row["raw_score"],
                weighted_percent=row["weighted_percent"],
            )
            for row in section_version_payload
        ]
        if section_version_rows:
            IFRS9EvaluationVersionSectionScore.objects.bulk_create(
                section_version_rows,
                batch_size=BULK_WRITE_BATCH_SIZE,
            )

    return version


def _is_ifrs9_checkbox_attribute(attribute: IFRS9Attribute) -> bool:
    return getattr(attribute, "input_type", "radio") == "checkbox"


def _parse_ifrs9_checkbox_value(raw_value: Any) -> list[str]:
    if raw_value in (None, ""):
        return []
    if isinstance(raw_value, list):
        return [str(value) for value in raw_value if str(value)]
    if isinstance(raw_value, tuple):
        return [str(value) for value in raw_value if str(value)]
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(value) for value in parsed if str(value)]
        return [value for value in raw_value.split(",") if value]
    return [str(raw_value)]


def _build_ifrs9_form_values_from_responses(
    responses: list[Any],
) -> dict[int, Any]:
    values: dict[int, Any] = {}
    for response in responses:
        if _is_ifrs9_checkbox_attribute(response.attribute):
            parsed_value = _parse_ifrs9_checkbox_value(response.raw_value)
            values[response.attribute_id] = parsed_value if parsed_value else None
        elif response.option_id:
            values[response.attribute_id] = str(response.option_id)
        elif response.raw_value == "" and response.option_id is None:
            values[response.attribute_id] = ""
    return values


def _serialize_ifrs9_checkbox_value(selected_option_ids: list[int]) -> str:
    return json.dumps(selected_option_ids)


def _get_ifrs9_attribute_highest_possible_score(attribute: IFRS9Attribute) -> Decimal:
    max_option = attribute.options.aggregate(Max("allocated_score"))
    return Decimal(str(max_option["allocated_score__max"] or 0))


def _resolve_ifrs9_attribute_submission(
    attribute: IFRS9Attribute,
    submitted_value: Any,
) -> dict[str, Any]:
    option_queryset = attribute.options.all().order_by("display_order", "id")

    if _is_ifrs9_checkbox_attribute(attribute):
        option_map = {str(option.id): option for option in option_queryset}
        selected_ids: list[int] = []
        selected_options: list[IFRS9Option] = []
        for raw_id in _parse_ifrs9_checkbox_value(submitted_value):
            option = option_map.get(str(raw_id))
            if option is None:
                continue
            selected_ids.append(option.id)
            selected_options.append(option)

        selected_ids = list(dict.fromkeys(selected_ids))
        selected_options = sorted(
            {option.id: option for option in selected_options}.values(),
            key=lambda option: (option.display_order, option.id),
        )
        raw_score = sum(
            (Decimal(str(option.allocated_score or 0)) for option in selected_options),
            Decimal("0"),
        )
        highest_score = _get_ifrs9_attribute_highest_possible_score(attribute)
        weight_percent = Decimal(str(attribute.weight_percent or 0))
        weighted_score = (
            (raw_score / highest_score) * weight_percent
            if highest_score > 0 and raw_score != Decimal("0")
            else Decimal("0")
        )
        return {
            "attribute": attribute,
            "selected_option": selected_options[0] if selected_options else None,
            "selected_options": selected_options,
            "selected_option_ids": selected_ids,
            "stored_raw_value": _serialize_ifrs9_checkbox_value(selected_ids) if selected_ids else "",
            "allocated_score": raw_score,
            "weighted_score": weighted_score,
            "highest_score": highest_score,
            "has_selection": bool(selected_options),
        }

    raw_value = submitted_value
    selected_option = None
    allocated_score = Decimal("0")

    if attribute.data_type == "choice":
        if raw_value:
            try:
                selected_option = attribute.options.filter(id=int(raw_value)).first()
            except (TypeError, ValueError):
                selected_option = None
        if selected_option is not None:
            allocated_score = Decimal(str(selected_option.allocated_score or 0))
            stored_raw_value = selected_option.value or str(selected_option.id)
        else:
            stored_raw_value = ""
    else:
        stored_raw_value = raw_value or ""
        try:
            allocated_score = Decimal(stored_raw_value or "0")
        except Exception:
            allocated_score = Decimal("0")

    highest_score = _get_ifrs9_attribute_highest_possible_score(attribute)
    weight_percent = Decimal(str(attribute.weight_percent or 0))
    weighted_score = (
        (allocated_score / highest_score) * weight_percent
        if highest_score > 0 and allocated_score > 0
        else Decimal("0")
    )
    return {
        "attribute": attribute,
        "selected_option": selected_option,
        "selected_options": [selected_option] if selected_option is not None else [],
        "selected_option_ids": [selected_option.id] if selected_option is not None else [],
        "stored_raw_value": stored_raw_value,
        "allocated_score": allocated_score,
        "weighted_score": weighted_score,
        "highest_score": highest_score,
        "has_selection": (selected_option is not None) or (attribute.data_type != "choice" and allocated_score > 0),
    }


def _bulk_create_ifrs9_attribute_responses(
    *,
    evaluation: IFRS9Evaluation,
    attribute_specs: list[dict[str, Any]],
    uploaded_files,
    uploaded_by,
) -> None:
    if not attribute_specs:
        return

    response_rows = [
        IFRS9AttributeResponse(
            evaluation=evaluation,
            attribute=spec["attribute"],
            option=spec["selected_option"],
            raw_value=spec["stored_raw_value"],
            allocated_score=spec["allocated_score"],
        )
        for spec in attribute_specs
    ]
    IFRS9AttributeResponse.objects.bulk_create(
        response_rows,
        batch_size=BULK_WRITE_BATCH_SIZE,
    )

    document_specs = []
    for spec in attribute_specs:
        attribute = spec["attribute"]
        selected_options = spec.get("selected_options", [])
        if not attribute.requires_document or not selected_options:
            continue
        combined_files = []
        for option in selected_options:
            file_key = f"doc_attr_{attribute.id}_option_{option.id}"
            combined_files.extend(uploaded_files.getlist(file_key))
        if combined_files:
            document_specs.append((attribute, combined_files))

    if not document_specs:
        return

    response_map = {
        response.attribute_id: response
        for response in IFRS9AttributeResponse.objects.filter(
            evaluation=evaluation,
            attribute_id__in=[attribute.id for attribute, _uploaded_file_list in document_specs],
        )
    }

    for attribute, uploaded_file_list in document_specs:
        attribute_response = response_map.get(attribute.id)
        if attribute_response is None:
            continue
        for uploaded_file in uploaded_file_list:
            IFRS9AttributeResponseDocument.objects.create(
                attribute_response=attribute_response,
                file=uploaded_file,
                file_name=uploaded_file.name,
                uploaded_by=uploaded_by,
            )


@login_required
def customer_search_api(request: HttpRequest) -> JsonResponse:
    """
    API endpoint to search customers by customer_ref_code or customer_name.
    Returns JSON list of matching customers, prioritizing exact matches.
    Uses the active branch context to filter customers by branch.
    Case-insensitive partial matching for both code and name.
    """
    import logging
    logger = logging.getLogger(__name__)
    
    query = request.GET.get("q", "").strip()
    
    logger.info(f"Customer search API called - Query: '{query}'")
    
    # Allow search with just 1 character for better user experience
    if not query or len(query) < 1:
        logger.warning("Empty query received")
        return JsonResponse({"customers": []})
    
    results = search_main_customers(query=query, limit=20)
    logger.info(f"Found {len(results)} customers matching query '{query}' across all branches")
    
    logger.info(f"Returning {len(results)} customers")
    return JsonResponse({"customers": results})


@login_required
def ifrs9_autofill_api(request: HttpRequest, template_id: int) -> JsonResponse:
    template = get_object_or_404(IFRS9ScoreSheetTemplate, pk=template_id)
    _, _, attributes_by_driver = _build_configuration(template, use_approved_version=True)
    payload = _build_ifrs9_autofill_payload(
        request,
        template,
        attributes_by_driver,
        customer_ref_code=request.GET.get("customer_code", "").strip(),
        branch_code=request.GET.get("branch_code", "").strip(),
        branch_name=request.GET.get("branch_name", "").strip(),
    )
    return JsonResponse(
        {
            "attribute_values": payload.get("attribute_values", {}),
            "applied_labels": payload.get("applied_labels", []),
            "applied_attribute_ids": payload.get("applied_attribute_ids", []),
            "missing_required_labels": payload.get("missing_required_labels", []),
            "missing_required_attribute_ids": payload.get("missing_required_attribute_ids", []),
            "customer_name": payload.get("customer_name", ""),
            "branch_name": payload.get("branch_name", ""),
            "branch_code": payload.get("branch_code", ""),
        }
    )


@login_required
def ifrs9_scores_template_select_view(request: HttpRequest) -> HttpResponse:
    """
    View to select which template to use for the IFRS9 score form.
    Shows list of all active templates.
    """
    all_branches_redirect = _redirect_if_all_branches_selected_for_score_entry(request)
    if all_branches_redirect is not None:
        return all_branches_redirect

    templates = IFRS9ScoreSheetTemplate.objects.filter(is_active=True, status="approved").order_by("code", "name")

    context = {
        "templates": templates,
    }

    return render(
        request,
        "ifrs9_score_config/ifrs9_form/template_select.html",
        context,
    )


@login_required
def ifrs9_scores_form_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """
    Main IFRS9 score form view.
    - Renders the score form based on the selected template
    - Accepts branch & customer details and attribute responses
    - Calculates raw and weighted scores plus final grade
    - Persists the full audit trail
    """
    all_branches_redirect = _redirect_if_all_branches_selected_for_score_entry(request)
    if all_branches_redirect is not None:
        return all_branches_redirect

    template = get_object_or_404(IFRS9ScoreSheetTemplate, id=template_id, is_active=True, status="approved")

    # CRITICAL: Use approved template version structure for score forms
    # This ensures that pending template changes don't affect new score forms until approved
    sections, drivers_by_section, attributes_by_driver = _build_configuration(template, use_approved_version=True)
    
    # Get current branch from session (set by context processor)
    # The branch is automatically displayed in the form from the context processor

    if request.method == "POST":
        # Resolve the active branch from session/accessible branches without forcing branch_code.
        customer_ref_code = request.POST.get("customer_ref_code", "").strip()
        
        branch = resolve_branch_context(request)
        customer = None
        
        if customer_ref_code:
            customer = get_main_customer_any_branch(customer_ref_code)
        
        branch_name = branch.branch_name if branch else ""
        branch_code = branch.branch_code if branch else request.session.get('current_branch_code', '').strip()
        customer_name = customer.customer_name if customer else ""
        customer_id = customer.customer_ref_code if customer else ""
        autofill_payload = (
            _build_ifrs9_autofill_payload(
                request,
                template,
                attributes_by_driver,
                customer_ref_code=customer_id,
                branch_code=branch_code,
                branch_name=branch_name,
            )
            if customer_id
            else _empty_autofill_payload()
        )

        # Check if this is a draft save or final submission
        is_draft = request.POST.get('save_draft') == 'true'

        counterpart_block_response = (
            _maybe_block_new_customer_for_counterpart(
                request,
                current_side="ifrs9",
                current_customer_id=customer_id,
                current_branch_name=branch_name,
            )
            if customer_id and branch_name
            else None
        )
        if counterpart_block_response is not None:
            return counterpart_block_response

        locked_existing_payload = (
            _get_locked_existing_ifrs9_score_payload(customer_id, current_branch_name=branch_name)
            if customer_id
            else None
        )
        if locked_existing_payload is not None:
            locked_branch_name = locked_existing_payload.get("branch_name") or "Unknown branch"
            locked_timestamp = locked_existing_payload.get("timestamp_label") or "an earlier time"
            messages.error(
                request,
                f"Customer '{customer_name}' ({customer_id}) was already scored in branch '{locked_branch_name}' on {locked_timestamp}. Workflow Rules are blocking any new IFRS9 scoring for this customer. Opening the saved IFRS9 score form in read-only mode.",
            )
            return redirect(
                "scorecard:ifrs9_scores_view_detail",
                evaluation_id=locked_existing_payload["evaluation_id"],
            )

        # Always require customer and branch info (even for drafts)
        missing_meta = not (customer_ref_code and branch and customer)

        missing_attributes = []
        attribute_values: Dict[int, Any] = {}

        # Collect attribute values (always collect, but only validate if not a draft)
        for driver_id, attrs in attributes_by_driver.items():
            for attribute in attrs:
                field_name = _field_name_for_attribute(attribute)
                if _is_ifrs9_checkbox_attribute(attribute):
                    value = [raw for raw in request.POST.getlist(field_name) if raw != ""]
                    attribute_values[attribute.id] = value
                    if not is_draft and attribute.is_required and not value:
                        missing_attributes.append(attribute)
                else:
                    value = request.POST.get(field_name)

                    # Store the value:
                    # - None: No selection made
                    # - "": User explicitly selected "None" (valid selection)
                    # - option_id: User selected an actual option
                    attribute_values[attribute.id] = value if value is not None else None

                    # Only validate required attributes if NOT saving as draft
                    # "None" (value == "") is a valid selection, so only fail if value is None (no selection made)
                    if not is_draft and attribute.is_required and value is None:
                        missing_attributes.append(attribute)

        # For drafts: only check missing_meta (customer/branch info)
        # For submissions: check both missing_meta and missing_attributes
        if is_draft:
            # Drafts only need customer/branch info
            if missing_meta:
                context = {
                    "template": template,
                    "sections": sections,
                    "drivers_by_section": drivers_by_section,
                    "attributes_by_driver": attributes_by_driver,
                    "errors": {
                        "missing_meta": missing_meta,
                        "missing_attributes": [],  # No attribute validation for drafts
                    },
                    # Preserve entered values for re-display
                    "form_data": {
                        "customer_ref_code": customer_ref_code,
                        "branch_name": branch_name,
                        "branch_code": branch_code,
                        "customer_name": customer_name,
                        "customer_id": customer_id,
                        "attribute_values": attribute_values,
                    },
                }
                return render(
                    request,
                    "ifrs9_score_config/ifrs9_form/ifrs9_scores_form.html",
                    context,
                )
        elif missing_meta or missing_attributes:
            context = {
                "template": template,
                "sections": sections,
                "drivers_by_section": drivers_by_section,
                "attributes_by_driver": attributes_by_driver,
                "errors": {
                    "missing_meta": missing_meta,
                    "missing_attributes": missing_attributes,
                },
                # Preserve entered values for re-display
                "form_data": {
                    "customer_ref_code": customer_ref_code,
                    "branch_name": branch_name,
                    "branch_code": branch_code,
                    "customer_name": customer_name,
                    "customer_id": customer_id,
                    "attribute_values": attribute_values,
                },
            }
            return render(
                request,
                "ifrs9_score_config/ifrs9_form/ifrs9_scores_form.html",
                context,
            )

        # is_draft is already checked above
        action = 'draft' if is_draft else 'submit'
        
        # Check if customer already has a score form (completed or draft)
        existing_evaluation = IFRS9Evaluation.objects.filter(
            customer_id=customer_id,
            branch_name=branch_name
        ).exclude(status='completed').first()  # Allow multiple completed, but only one draft/in_progress
        
        # If submitting (not draft), check for completed score forms
        if not is_draft:
            completed_evaluation = IFRS9Evaluation.objects.filter(
                customer_id=customer_id,
                branch_name=branch_name,
                status='completed'
            ).first()
            
            if completed_evaluation:
                messages.warning(
                    request,
                    f"A completed IFRS9 score form for customer '{customer_name}' ({customer_id}) at branch '{branch_name}' already exists. "
                    f"Please use the Edit functionality to make changes."
                )
                return redirect("scorecard:ifrs9_scores_edit", evaluation_id=completed_evaluation.id)
        
        # If draft exists, update it; otherwise create new
        # Process and save the evaluation
        with transaction.atomic():
            if existing_evaluation:
                evaluation = IFRS9Evaluation.objects.select_for_update().get(
                    pk=existing_evaluation.pk
                )
                if (
                    not is_draft
                    and evaluation.status in {"submitted", "approved"}
                    and evaluation.versions.filter(version_number=1).exists()
                ):
                    messages.info(
                        request,
                        "This IFRS9 score form was already submitted. Showing the saved record instead of submitting it again.",
                    )
                    return redirect(
                        "scorecard:ifrs9_scores_view_detail",
                        evaluation_id=evaluation.id,
                    )

                evaluation.template = template
                evaluation.branch_name = branch_name
                evaluation.customer_name = customer_name
                evaluation.customer_id = customer_id
                if not evaluation.maker:
                    evaluation.maker = request.user
            else:
                evaluation = IFRS9Evaluation.objects.create(
                    template=template,
                    branch_name=branch_name,
                    customer_name=customer_name,
                    customer_id=customer_id,
                    status='draft' if is_draft else 'in_progress',
                    maker=request.user,
                )
                evaluation = IFRS9Evaluation.objects.select_for_update().get(
                    pk=evaluation.pk
                )

            evaluation.autofill_metadata = _build_saved_autofill_metadata(
                autofill_payload,
                attributes_by_driver,
                attribute_values,
            )

            # Delete existing responses and scores if updating
            if existing_evaluation:
                evaluation.attribute_responses.all().delete()
                evaluation.driver_scores.all().delete()
                evaluation.section_scores.all().delete()

            # For drafts, only save attribute responses - don't calculate scores
            if is_draft:
                draft_attribute_specs: list[dict[str, Any]] = []
                for driver_id, attrs in attributes_by_driver.items():
                    for attribute in attrs:
                        raw_value = attribute_values.get(attribute.id, None)
                        if raw_value is None:
                            continue
                        if _is_ifrs9_checkbox_attribute(attribute) and not raw_value:
                            continue
                        if attribute.data_type != "choice" and raw_value == "":
                            continue

                        spec = _resolve_ifrs9_attribute_submission(attribute, raw_value)
                        if attribute.data_type == "choice" and _is_ifrs9_checkbox_attribute(attribute) and raw_value and not spec["selected_option_ids"]:
                            continue
                        if attribute.data_type == "choice" and not _is_ifrs9_checkbox_attribute(attribute) and raw_value not in ("", None) and spec["selected_option"] is None:
                            continue
                        draft_attribute_specs.append(spec)

                _bulk_create_ifrs9_attribute_responses(
                    evaluation=evaluation,
                    attribute_specs=draft_attribute_specs,
                    uploaded_files=request.FILES,
                    uploaded_by=request.user,
                )
                
                # For drafts: Don't calculate scores, don't set final grade
                evaluation.total_raw_score = None
                evaluation.total_weighted_percent = None
                evaluation.final_grade = ""
                evaluation.status = 'draft'
                evaluation.save()
                log_ifrs9_score_audit(
                    request.user,
                    "save_draft",
                    evaluation,
                    "Draft IFRS9 score form created.",
                )
                
                messages.success(
                    request,
                    f"Draft saved successfully! You can continue working on it later.",
                )
                return redirect("scorecard:ifrs9_scores_draft_list")
            
            # For completed submissions: Calculate all scores
            # Calculate per-attribute weighted scores and aggregate at driver/section level
            # This matches the frontend calculation: WEIGHTED_SCORE = (ACTUAL_SCORE / Highest_Possible_Score) * WEIGHT per attribute
            # IMPORTANT: Frontend only includes attributes with selected options in calculations
            # We must match this behavior to avoid discrepancies
            driver_raw_scores: Dict[int, Decimal] = {}
            driver_weighted_scores: Dict[int, Decimal] = {}
            submitted_attribute_specs: list[dict[str, Any]] = []

            for driver_id, attrs in attributes_by_driver.items():
                driver_total_raw = Decimal("0")
                driver_total_weighted = Decimal("0")

                for attribute in attrs:
                    raw_value = attribute_values.get(attribute.id, [] if _is_ifrs9_checkbox_attribute(attribute) else "")
                    spec = _resolve_ifrs9_attribute_submission(attribute, raw_value)
                    submitted_attribute_specs.append(spec)

                    if spec["has_selection"]:
                        driver_total_raw += spec["allocated_score"]
                        driver_total_weighted += spec["weighted_score"]

                driver_raw_scores[driver_id] = driver_total_raw
                driver_weighted_scores[driver_id] = driver_total_weighted

            _bulk_create_ifrs9_attribute_responses(
                evaluation=evaluation,
                attribute_specs=submitted_attribute_specs,
                uploaded_files=request.FILES,
                uploaded_by=request.user,
            )
            attribute_version_payload = [
                {
                    "attribute_id": spec["attribute"].id,
                    "option_id": spec["selected_option"].id if spec["selected_option"] is not None else None,
                    "raw_value": spec["stored_raw_value"],
                    "allocated_score": spec["allocated_score"],
                }
                for spec in submitted_attribute_specs
            ]

            total_raw_score = Decimal("0")
            total_weighted_percent = Decimal("0")

            # Persist driver and section scores
            # Formulas (same for all sections):
            # ACTUAL_SCORE = ALLOCATED_SCORE (sum of allocated scores for the driver)
            # WEIGHTED_SCORE = Sum of (ACTUAL_SCORE / Highest Possible Score * WEIGHT) for each attribute
            # PROOF = IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE <> ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE
            section_weighted_totals = {}  # Track section totals to match frontend calculation
            driver_score_rows: list[IFRS9RiskDriverScore] = []
            section_score_rows: list[IFRS9SectionScore] = []
            
            for section in sections:
                section_total = Decimal("0")
                section_raw_total = Decimal("0")
                
                for driver in drivers_by_section.get(section.id, []):
                    # ACTUAL_SCORE = ALLOCATED_SCORE (sum of allocated scores)
                    actual_score = driver_raw_scores.get(driver.id, Decimal("0"))
                    
                    # WEIGHTED_SCORE = Sum of per-attribute weighted scores (already calculated above)
                    weighted_percent = driver_weighted_scores.get(driver.id, Decimal("0"))

                    # PROOF validation according to Excel formula:
                    # PROOF = IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE <> ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE
                    # ALLOCATED_SCORE for driver = max possible score (sum of highest scores from all attributes)
                    max_score = driver.get_max_score()

                    if actual_score == Decimal("0") or actual_score is None:
                        # If ACTUAL_SCORE is empty, return '' + ALLOCATED_SCORE = ALLOCATED_SCORE
                        proof = str(max_score) if max_score else ""
                    elif actual_score != max_score:
                        # If ACTUAL_SCORE != ALLOCATED_SCORE, return 'ERROR' + ALLOCATED_SCORE
                        proof = f"ERROR{max_score}" if max_score else "ERROR"
                    else:
                        # If ACTUAL_SCORE == ALLOCATED_SCORE, return '' (empty string) instead of ALLOCATED_SCORE
                        proof = ""

                    driver_score_rows.append(
                        IFRS9RiskDriverScore(
                        evaluation=evaluation,
                        risk_driver=driver,
                        raw_score=actual_score,
                        weighted_percent=weighted_percent,
                        proof=proof,
                        )
                    )

                    total_raw_score += actual_score
                    section_total += weighted_percent
                    section_raw_total += actual_score

                # Store section weighted total
                section_weighted_totals[section.id] = section_total

                section_score_rows.append(
                    IFRS9SectionScore(
                        evaluation=evaluation,
                        section=section,
                        raw_score=section_raw_total,
                        weighted_percent=section_total,
                    )
                )

            if driver_score_rows:
                IFRS9RiskDriverScore.objects.bulk_create(
                    driver_score_rows,
                    batch_size=BULK_WRITE_BATCH_SIZE,
                )
            if section_score_rows:
                IFRS9SectionScore.objects.bulk_create(
                    section_score_rows,
                    batch_size=BULK_WRITE_BATCH_SIZE,
                )
            driver_version_payload = [
                {
                    "risk_driver_id": row.risk_driver_id,
                    "raw_score": row.raw_score,
                    "weighted_percent": row.weighted_percent,
                    "proof": row.proof,
                }
                for row in driver_score_rows
            ]
            section_version_payload = [
                {
                    "section_id": row.section_id,
                    "raw_score": row.raw_score,
                    "weighted_percent": row.weighted_percent,
                }
                for row in section_score_rows
            ]
            
            # Calculate total_weighted_percent as sum of section totals (matching frontend)
            total_weighted_percent = sum(section_weighted_totals.values())

            final_grade = ""
            
            # CRITICAL: Check if there are already approved scores before updating
            # If approved scores exist, preserve them - don't overwrite with unapproved scores
            # Only update scores if there are no approved scores yet (first submission)
            if evaluation.approved_weighted_percent is not None:
                # There are approved scores - preserve them, don't overwrite with unapproved scores
                # The new unapproved scores will be stored in the version snapshot
                # Keep the approved scores in total_weighted_percent and final_grade
                pass  # Don't update total_weighted_percent/final_grade - keep approved scores
            else:
                # No approved scores yet - this is a first submission
                # Store scores temporarily (they're not approved yet, but we need to store them somewhere)
                # They will be moved to approved fields when checker approves
                evaluation.total_raw_score = total_raw_score
                evaluation.total_weighted_percent = total_weighted_percent
                evaluation.final_grade = final_grade
            
            # Use maker-checker workflow: set status to 'submitted' instead of 'completed'
            # Ensure maker is set
            if not evaluation.maker:
                evaluation.maker = request.user
            
            # IMPORTANT: For first submission, DO NOT set approved scores yet
            # Approved scores should only be set when checker approves, not on first submission
            # This way, "current" will be empty/null and only "pending" will show until approval
            
            auto_approve = _can_auto_approve_ifrs9_submission(request.user)

            evaluation.status = 'approved' if auto_approve else 'submitted'
            evaluation.submitted_by = request.user
            evaluation.submitted_at = timezone.now()
            evaluation.version = 1  # First submission
            evaluation.save()

            # Create initial version snapshot (Version 1) and keep all prior versions as history.
            # Pass the calculated scores explicitly to ensure version has the correct scores
            _create_ifrs9_evaluation_version(
                evaluation=evaluation,
                version_number=1,
                user=request.user,
                change_description="Initial IFRS9 score form submission",
                total_weighted_percent=total_weighted_percent,
                final_grade=final_grade,
                total_raw_score=total_raw_score,
                attribute_version_payload=attribute_version_payload,
                driver_version_payload=driver_version_payload,
                section_version_payload=section_version_payload,
            )

            if auto_approve:
                _finalize_ifrs9_auto_approval(
                    evaluation,
                    request.user,
                    old_status="in_progress",
                    comments="Initial IFRS9 score form auto-approved on submission",
                )
                evaluation.refresh_from_db()
                log_ifrs9_score_audit(
                    request.user,
                    "approve",
                    evaluation,
                    f"Initial IFRS9 score form auto-approved on submission with score {evaluation.total_weighted_percent:.2f}%.",
                )
                messages.success(
                    request,
                    f"IFRS9 score form approved automatically on submit. Total Score: {evaluation.total_weighted_percent}%.",
                )
                notify_ifrs9_approved(evaluation, request.user)
            else:
                IFRS9EvaluationWorkflowHistory.objects.create(
                    evaluation=evaluation,
                    action='submitted',
                    from_status='in_progress',
                    to_status='submitted',
                    performed_by=request.user,
                    comments='Initial IFRS9 score form submission'
                )
                log_ifrs9_score_audit(
                    request.user,
                    "submit",
                    evaluation,
                    f"Initial IFRS9 score form submitted for review with score {total_weighted_percent:.2f}%.",
                )
                messages.success(
                    request,
                    f"IFRS9 score form submitted for review! Total Score: {total_weighted_percent}%. "
                    f"Waiting for checker approval.",
                )
                notify_ifrs9_submitted(evaluation)
            return redirect("scorecard:maker_ifrs9_scores_submitted_list")

    # GET request - show form

    # Check if there's a customer_ref_code in the URL or form data to check for existing draft/in-progress evaluation
    existing_evaluation = None
    customer_ref_code_from_get = request.GET.get('customer_ref_code', '')
    if customer_ref_code_from_get:
        current_branch = resolve_branch_context(request)
        if current_branch is not None:
            # Check for draft or in_progress evaluations (not completed ones)
            existing_evaluation = IFRS9Evaluation.objects.filter(
                customer_id=customer_ref_code_from_get,
                branch_name=current_branch.branch_name
            ).exclude(status='completed').first()

    autofill_payload = _empty_autofill_payload()
    if customer_ref_code_from_get and existing_evaluation is None:
        autofill_payload = _build_ifrs9_autofill_payload(
            request,
            template,
            attributes_by_driver,
            customer_ref_code=customer_ref_code_from_get,
        )

    context = {
        "template": template,
        "sections": sections,
        "drivers_by_section": drivers_by_section,
        "attributes_by_driver": attributes_by_driver,
        "existing_evaluation": existing_evaluation,
        # Empty form data for initial load
        "form_data": {
            "customer_ref_code": customer_ref_code_from_get or "",
            "branch_name": autofill_payload.get("branch_name", ""),
            "branch_code": autofill_payload.get("branch_code", ""),
            "customer_name": autofill_payload.get("customer_name", ""),
            "customer_id": customer_ref_code_from_get or "",
            "attribute_values": autofill_payload.get("attribute_values", {}),
        },
        "autofill_applied_labels": autofill_payload.get("applied_labels", []),
        "autofill_applied_attribute_ids": autofill_payload.get("applied_attribute_ids", []),
        "autofill_missing_labels": autofill_payload.get("missing_required_labels", []),
        "autofill_missing_attribute_ids": autofill_payload.get("missing_required_attribute_ids", []),
        "autofill_profile_changes": [],
    }

    return render(
        request,
        "ifrs9_score_config/ifrs9_form/ifrs9_scores_form.html",
        context,
    )


@login_required
def ifrs9_scores_submitted_list_view(request: HttpRequest) -> HttpResponse:
    """
    View to list all submitted IFRS9 score forms/evaluations.
    Filters by current branch for both regular users and admins.
    """
    search_query = (request.GET.get("q") or "").strip()
    status_filter = (request.GET.get("status") or "").strip().lower()
    exposure_filter = _get_exposure_filter(request)
    pending_versions = IFRS9EvaluationVersion.objects.filter(
        evaluation_id=OuterRef("pk"),
        is_approved=False,
    ).order_by("-version_number")

    evaluations = (
        _get_branch_filtered_ifrs9_evaluations(
            request,
            exclude_statuses=["draft", "in_progress"],
        )
        .annotate(
            pending_weighted_percent=Subquery(
                pending_versions.values("total_weighted_percent")[:1],
                output_field=DecimalField(max_digits=6, decimal_places=2),
            ),
            pending_grade=Subquery(
                pending_versions.values("final_grade")[:1],
                output_field=CharField(),
            ),
        )
        .defer("autofill_metadata")
        .only(
            "id",
            "template_id",
            "template__code",
            "template__name",
            "branch_name",
            "customer_name",
            "customer_id",
            "total_weighted_percent",
            "final_grade",
            "status",
            "submitted_at",
            "created_at",
        )
        .order_by("-submitted_at", "-created_at", "-id")
    )

    if search_query:
        evaluations = evaluations.filter(
            Q(template__code__icontains=search_query)
            | Q(template__name__icontains=search_query)
            | Q(branch_name__icontains=search_query)
            | Q(customer_name__icontains=search_query)
            | Q(customer_id__icontains=search_query)
            | Q(final_grade__icontains=search_query)
        )

    if status_filter in {"submitted", "approved", "returned"}:
        evaluations = evaluations.filter(status=status_filter)

    evaluations = _apply_active_exposure_filter(request, evaluations, exposure_filter)

    page_obj, search_query, page_size = _paginate_list_queryset(
        request,
        evaluations,
    )
    summary_counts = _get_submitted_status_counts(
        evaluations,
        request,
        search_query,
        status_filter,
        "ifrs9",
        exposure_filter,
    )

    context = {
        "evaluations": page_obj.object_list,
        "page_obj": page_obj,
        "search_query": search_query,
        "page_size": page_size,
        "status_filter": status_filter,
        "exposure_filter": exposure_filter,
        **summary_counts,
        "list_query_string": _build_list_query_string(
            search_query=search_query,
            page_size=page_size,
            status_filter=status_filter,
            exposure_filter=exposure_filter,
        ),
    }

    return render(
        request,
        "ifrs9_score_config/ifrs9_form/submitted_list.html",
        context,
    )


def _get_filtered_ifrs9_evaluations(request):
    """Helper to get IFRS9 evaluations filtered by branch (same logic as submitted list)."""
    search_query = (request.GET.get("q") or "").strip()
    status_filter = (request.GET.get("status") or "").strip().lower()
    exposure_filter = _get_exposure_filter(request)
    evaluations = _get_branch_filtered_ifrs9_evaluations(
        request,
        exclude_statuses=["draft", "in_progress"],
    ).order_by("-submitted_at", "-created_at", "-id")

    if search_query:
        evaluations = evaluations.filter(
            Q(template__code__icontains=search_query)
            | Q(template__name__icontains=search_query)
            | Q(branch_name__icontains=search_query)
            | Q(customer_name__icontains=search_query)
            | Q(customer_id__icontains=search_query)
            | Q(final_grade__icontains=search_query)
        )

    if status_filter in {"submitted", "approved", "returned"}:
        evaluations = evaluations.filter(status=status_filter)

    evaluations = _apply_active_exposure_filter(request, evaluations, exposure_filter)

    return evaluations


def _get_ifrs9_export_rows(request):
    page = 'all'
    evaluations = _get_filtered_ifrs9_evaluations(request)
    return list(evaluations), page


@login_required
def ifrs9_scores_export_excel(request: HttpRequest) -> HttpResponse:
    """Export IFRS9 score forms to Excel format."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        return HttpResponse("Excel export requires openpyxl library. Please install it.", status=500)
    evaluations, page = _get_ifrs9_export_rows(request)
    export_date = timezone.localdate().isoformat()
    wb = Workbook()
    ws = wb.active
    ws.title = "IFRS9 Scores"
    headers = [
        'Template Code', 'Template Name', 'Branch', 'Customer Name', 'Customer ID',
        'Weighted Score', 'Grade', 'Submitted Date', 'Submitted Time'
    ]
    header_fill = PatternFill(start_color="0066cc", end_color="0066cc", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    for col_num, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_num)
        cell.value = header
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center', vertical='center')
    for row_num, evaluation in enumerate(evaluations, 2):
        template_code, template_name = _get_ifrs9_template_label(evaluation)
        ws.cell(row=row_num, column=1, value=template_code)
        ws.cell(row=row_num, column=2, value=template_name)
        ws.cell(row=row_num, column=3, value=evaluation.branch_name or '')
        ws.cell(row=row_num, column=4, value=evaluation.customer_name or '')
        ws.cell(row=row_num, column=5, value=evaluation.customer_id or '')
        ws.cell(row=row_num, column=6, value=f"{evaluation.total_weighted_percent or 0:.2f}%")
        ws.cell(row=row_num, column=7, value=evaluation.final_grade or '-')
        ws.cell(row=row_num, column=8, value=evaluation.submitted_at.date() if evaluation.submitted_at else '')
        ws.cell(row=row_num, column=9, value=evaluation.submitted_at.time() if evaluation.submitted_at else '')
    for col in ws.columns:
        max_length = 0
        col_letter = col[0].column_letter
        for cell in col:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except Exception:
                pass
        adjusted_width = min(max_length + 2, 50)
        ws.column_dimensions[col_letter].width = adjusted_width
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = (
        f'attachment; filename="ifrs9_scores_{page}_{export_date}.xlsx"'
    )
    wb.save(response)
    return response


@login_required
def ifrs9_scores_export_csv(request: HttpRequest) -> HttpResponse:
    """Export IFRS9 score forms to CSV format."""
    import csv
    evaluations, page = _get_ifrs9_export_rows(request)
    export_date = timezone.localdate().isoformat()
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = (
        f'attachment; filename="ifrs9_scores_{page}_{export_date}.csv"'
    )
    writer = csv.writer(response)
    writer.writerow([
        'Template Code', 'Template Name', 'Branch', 'Customer Name', 'Customer ID',
        'Weighted Score', 'Grade', 'Submitted Date', 'Submitted Time'
    ])
    for evaluation in evaluations:
        template_code, template_name = _get_ifrs9_template_label(evaluation)
        writer.writerow([
            template_code,
            template_name,
            evaluation.branch_name or '',
            evaluation.customer_name or '',
            evaluation.customer_id or '',
            f"{evaluation.total_weighted_percent or 0:.2f}%",
            evaluation.final_grade or '-',
            evaluation.submitted_at.date() if evaluation.submitted_at else '',
            evaluation.submitted_at.time() if evaluation.submitted_at else '',
        ])
    return response


@login_required
def ifrs9_scores_draft_list_view(request: HttpRequest) -> HttpResponse:
    """
    View to list all draft/in-progress IFRS9 score forms.
    Filters by current branch for both regular users and admins.
    """
    search_query = (request.GET.get("q") or "").strip()
    evaluations = _get_branch_filtered_ifrs9_evaluations(
        request,
        statuses=["draft", "in_progress"],
    ).defer("autofill_metadata").only(
        "id",
        "template_id",
        "template__code",
        "template__name",
        "branch_name",
        "customer_name",
        "customer_id",
        "status",
        "updated_at",
        "created_at",
    ).order_by("-updated_at", "-id")

    if search_query:
        evaluations = evaluations.filter(
            Q(template__code__icontains=search_query)
            | Q(template__name__icontains=search_query)
            | Q(branch_name__icontains=search_query)
            | Q(customer_name__icontains=search_query)
            | Q(customer_id__icontains=search_query)
        )

    page_obj, search_query, page_size = _paginate_list_queryset(
        request,
        evaluations,
    )

    context = {
        "evaluations": page_obj.object_list,
        "page_obj": page_obj,
        "search_query": search_query,
        "page_size": page_size,
        "list_query_string": _build_list_query_string(
            search_query=search_query,
            page_size=page_size,
        ),
    }

    return render(
        request,
        "ifrs9_score_config/ifrs9_form/draft_list.html",
        context,
    )


@login_required
def ifrs9_scores_template_assignment_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    evaluation = get_object_or_404(
        IFRS9Evaluation.objects.select_related("template", "maker"),
        id=evaluation_id,
    )

    if not _can_manage_ifrs9_template(request, evaluation):
        messages.error(request, "You don't have permission to manage the template for this IFRS9 score form.")
        target = "scorecard:ifrs9_scores_draft_list" if evaluation.status in {"draft", "in_progress", "returned"} else "scorecard:ifrs9_scores_submitted_list"
        return redirect(target)

    if request.method == "POST":
        template_id = request.POST.get("template_id")
        selected_template = IFRS9ScoreSheetTemplate.objects.filter(
            id=template_id,
            is_active=True,
            status="approved",
        ).first()
        if not selected_template:
            messages.error(request, "Choose an active approved IFRS9 template before continuing.")
        elif evaluation.template_id == selected_template.id:
            messages.info(
                request,
                f"This IFRS9 score form is already linked to template '{selected_template.code}'.",
            )
            return redirect("scorecard:ifrs9_scores_edit", evaluation_id=evaluation.id)
        else:
            had_existing_template = bool(evaluation.template_id)
            previous_template_code = _reset_ifrs9_evaluation_for_template(
                evaluation,
                selected_template,
                performed_by=request.user,
            )
            log_ifrs9_score_audit(
                request.user,
                "change_template" if had_existing_template else "assign_template",
                evaluation,
                (
                    f"Template changed from {previous_template_code} to {selected_template.code}."
                    if had_existing_template
                    else f"Template assigned: {selected_template.code}."
                ),
            )
            if had_existing_template:
                messages.success(
                    request,
                    f"Template changed from '{previous_template_code}' to '{selected_template.code}'. "
                    "The existing score, grade, and status were kept unchanged.",
                )
            else:
                messages.success(
                    request,
                    f"Template '{selected_template.code}' has been assigned. "
                    "The imported score is preserved as version 1, and the edit screen will create the next version when saved.",
                )
            return redirect("scorecard:ifrs9_scores_edit", evaluation_id=evaluation.id)

    templates = IFRS9ScoreSheetTemplate.objects.filter(is_active=True, status="approved").order_by("name", "code")
    current_template = evaluation.template

    context = {
        "evaluation": evaluation,
        "templates": templates,
        "current_template": current_template,
        "is_change": bool(current_template),
        "back_url_name": "scorecard:ifrs9_scores_draft_list" if evaluation.status in {"draft", "in_progress", "returned"} else "scorecard:ifrs9_scores_submitted_list",
    }
    return render(
        request,
        "ifrs9_score_config/ifrs9_form/ifrs9_scores_template_assignment.html",
        context,
    )


@login_required
def ifrs9_scores_edit_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    View to edit an IFRS9 score form.
    Loads existing evaluation data, allows modifications, and updates the evaluation.
    """
    evaluation = get_object_or_404(
        IFRS9Evaluation.objects.select_related("template").prefetch_related(
            "attribute_responses__attribute__risk_driver__section",
            "attribute_responses__option",
            "driver_scores__risk_driver",
            "section_scores__section",
        ),
        id=evaluation_id,
    )

    # Lock editing if status is 'submitted' (must wait for checker review or withdraw)
    # Approved score forms CAN be edited - they will create a new version and go through approval again
    if evaluation.status == 'submitted':
        messages.error(
            request,
            f"This IFRS9 score form is submitted for review. "
            f"It cannot be edited until it is returned for changes or you withdraw the submission."
        )
        return redirect("scorecard:ifrs9_scores_view_detail", evaluation_id=evaluation.id)
    
    if not evaluation.can_be_edited_by(request.user):
        messages.error(request, "You don't have permission to edit this IFRS9 score form.")
        return redirect("scorecard:ifrs9_scores_view_detail", evaluation_id=evaluation.id)

    if not evaluation.template_id or evaluation.template is None:
        messages.info(
            request,
            "Choose an IFRS9 template first so this imported score form can open in the full scorecard editor.",
        )
        return redirect("scorecard:ifrs9_scores_template_assignment", evaluation_id=evaluation.id)

    original_template = evaluation.template
    template = original_template
    template_switched = False
    template_switch_notice = ""
    if template and not template.is_active:
        messages.warning(
            request,
            f"Template '{template.code}' is inactive. Choose a template from the IFRS9 template table before editing this score form.",
        )
        return redirect("scorecard:ifrs9_scores_template_assignment", evaluation_id=evaluation.id)

    # Use approved template version structure to ensure consistency with approved template
    sections, drivers_by_section, attributes_by_driver = _build_configuration(template, use_approved_version=True)

    if request.method == "POST":
        # Double-check lock status even for POST requests (security)
        # Only lock 'submitted' status - 'approved' can be edited
        if evaluation.status == 'submitted':
            messages.error(
                request,
                f"This IFRS9 score form is submitted for review. "
                f"It cannot be edited until it is returned for changes or you withdraw the submission."
            )
            return redirect("scorecard:ifrs9_scores_view_detail", evaluation_id=evaluation.id)
        
        # Check if this is a draft save or final submission
        is_draft = request.POST.get('save_draft') == 'true'
        
        # Branch, Customer Name, and Customer ID are not editable - use existing values
        branch_name = evaluation.branch_name
        customer_name = evaluation.customer_name
        customer_id = evaluation.customer_id
        autofill_payload = (
            _build_ifrs9_autofill_payload(
                request,
                template,
                attributes_by_driver,
                customer_ref_code=customer_id,
            )
            if customer_id
            else _empty_autofill_payload()
        )

        # No need to validate meta fields since they're read-only
        missing_meta = False

        missing_attributes = []
        attribute_values: Dict[int, Any] = {}

        # Collect attribute values and validate required checkbox groups server-side as well.
        for driver_id, attrs in attributes_by_driver.items():
            for attribute in attrs:
                field_name = _field_name_for_attribute(attribute)
                if _is_ifrs9_checkbox_attribute(attribute):
                    value = [raw for raw in request.POST.getlist(field_name) if raw != ""]
                    attribute_values[attribute.id] = value
                    if not is_draft and attribute.is_required and not value:
                        missing_attributes.append(attribute)
                else:
                    value = request.POST.get(field_name)
                    attribute_values[attribute.id] = value if value is not None else None
                    if not is_draft and attribute.is_required and value is None:
                        missing_attributes.append(attribute)

        if not is_draft and missing_attributes:
            context = {
                "template": template,
                "sections": sections,
                "drivers_by_section": drivers_by_section,
                "attributes_by_driver": attributes_by_driver,
                "evaluation": evaluation,
                "attribute_responses": {},
                "template_switch_notice": template_switch_notice,
                "errors": {
                    "missing_meta": False,
                    "missing_attributes": missing_attributes,
                },
                "form_data": {
                    "branch_name": evaluation.branch_name,
                    "customer_name": evaluation.customer_name,
                    "customer_id": evaluation.customer_id,
                    "attribute_values": attribute_values,
                },
                "autofill_applied_labels": (evaluation.autofill_metadata or {}).get("applied_labels", []),
                "autofill_missing_labels": (evaluation.autofill_metadata or {}).get("current_missing_required_labels", (evaluation.autofill_metadata or {}).get("missing_required_labels", [])),
                "autofill_applied_attribute_ids": (evaluation.autofill_metadata or {}).get("applied_attribute_ids", []),
                "autofill_missing_attribute_ids": (evaluation.autofill_metadata or {}).get("current_missing_required_ids", (evaluation.autofill_metadata or {}).get("missing_required_attribute_ids", [])),
                "autofill_profile_changes": autofill_profile_changes,
            }
            return render(request, "ifrs9_score_config/ifrs9_form/ifrs9_scores_edit.html", context)

        # Process and update the evaluation
        with transaction.atomic():
            evaluation.autofill_metadata = _build_saved_autofill_metadata(
                autofill_payload,
                attributes_by_driver,
                attribute_values,
            )
            # Store old status and version info BEFORE updating (for version description)
            old_status = evaluation.status
            old_version_number = evaluation.version or 0
            
            # Only create history records for completed/approved score forms (not drafts)
            # Skip history creation if saving as draft
            if evaluation.status in ['completed', 'approved'] and not is_draft:
                # Also create history record with previous values (for backward compatibility)
                if evaluation.total_weighted_percent is not None:
                    IFRS9EvaluationHistory.objects.create(
                        evaluation=evaluation,
                        weighted_percent=evaluation.total_weighted_percent,
                        grade="",
                        raw_score=evaluation.total_raw_score,
                    )
                
                # Also preserve in previous fields for backward compatibility
                if evaluation.total_weighted_percent is not None:
                    evaluation.previous_weighted_percent = evaluation.total_weighted_percent
                evaluation.previous_grade = ""
            
            # Delete existing responses and scores (will be recreated with new values)
            evaluation.attribute_responses.all().delete()
            evaluation.driver_scores.all().delete()
            evaluation.section_scores.all().delete()

            # For drafts, only save attribute responses - don't calculate scores
            if is_draft:
                draft_attribute_specs: list[dict[str, Any]] = []
                for driver_id, attrs in attributes_by_driver.items():
                    for attribute in attrs:
                        raw_value = attribute_values.get(attribute.id, None)
                        if raw_value is None:
                            continue
                        if _is_ifrs9_checkbox_attribute(attribute) and not raw_value:
                            continue
                        if attribute.data_type != "choice" and raw_value == "":
                            continue

                        spec = _resolve_ifrs9_attribute_submission(attribute, raw_value)
                        if attribute.data_type == "choice" and _is_ifrs9_checkbox_attribute(attribute) and raw_value and not spec["selected_option_ids"]:
                            continue
                        if attribute.data_type == "choice" and not _is_ifrs9_checkbox_attribute(attribute) and raw_value not in ("", None) and spec["selected_option"] is None:
                            continue
                        draft_attribute_specs.append(spec)

                _bulk_create_ifrs9_attribute_responses(
                    evaluation=evaluation,
                    attribute_specs=draft_attribute_specs,
                    uploaded_files=request.FILES,
                    uploaded_by=request.user,
                )
                
                # For drafts: Don't calculate scores, don't set final grade
                evaluation.total_raw_score = None
                evaluation.total_weighted_percent = None
                evaluation.final_grade = ""
                evaluation.status = 'draft'
                evaluation.save()
                log_ifrs9_score_audit(
                    request.user,
                    "save_draft",
                    evaluation,
                    "Draft IFRS9 score form updated from the edit screen.",
                )
                
                messages.success(
                    request,
                    f"Draft saved successfully! You can continue working on it later.",
                )
                return redirect("scorecard:ifrs9_scores_draft_list")

            # For completed submissions: Calculate all scores (same logic as form view)
            driver_raw_scores: Dict[int, Decimal] = {}
            driver_weighted_scores: Dict[int, Decimal] = {}
            submitted_attribute_specs: list[dict[str, Any]] = []

            for driver_id, attrs in attributes_by_driver.items():
                driver_total_raw = Decimal("0")
                driver_total_weighted = Decimal("0")
                
                for attribute in attrs:
                    raw_value = attribute_values.get(attribute.id, [] if _is_ifrs9_checkbox_attribute(attribute) else "")
                    spec = _resolve_ifrs9_attribute_submission(attribute, raw_value)
                    submitted_attribute_specs.append(spec)

                    # Only include in totals if there's an actual selection
                    has_selection = spec["has_selection"]
                    
                    if has_selection:
                        driver_total_raw += spec["allocated_score"]
                        driver_total_weighted += spec["weighted_score"]

                driver_raw_scores[driver_id] = driver_total_raw
                driver_weighted_scores[driver_id] = driver_total_weighted

            _bulk_create_ifrs9_attribute_responses(
                evaluation=evaluation,
                attribute_specs=submitted_attribute_specs,
                uploaded_files=request.FILES,
                uploaded_by=request.user,
            )
            attribute_version_payload = [
                {
                    "attribute_id": spec["attribute"].id,
                    "option_id": spec["selected_option"].id if spec["selected_option"] is not None else None,
                    "raw_value": spec["stored_raw_value"],
                    "allocated_score": spec["allocated_score"],
                }
                for spec in submitted_attribute_specs
            ]

            total_raw_score = Decimal("0")
            total_weighted_percent = Decimal("0")

            # Persist driver and section scores
            section_weighted_totals = {}
            driver_score_rows: list[IFRS9RiskDriverScore] = []
            section_score_rows: list[IFRS9SectionScore] = []
            
            for section in sections:
                section_total = Decimal("0")
                section_raw_total = Decimal("0")
                
                for driver in drivers_by_section.get(section.id, []):
                    actual_score = driver_raw_scores.get(driver.id, Decimal("0"))
                    weighted_percent = driver_weighted_scores.get(driver.id, Decimal("0"))

                    # PROOF validation
                    max_score = driver.get_max_score()

                    if actual_score == Decimal("0") or actual_score is None:
                        proof = str(max_score) if max_score else ""
                    elif actual_score != max_score:
                        proof = f"ERROR{max_score}" if max_score else "ERROR"
                    else:
                        proof = ""

                    driver_score_rows.append(
                        IFRS9RiskDriverScore(
                            evaluation=evaluation,
                            risk_driver=driver,
                            raw_score=actual_score,
                            weighted_percent=weighted_percent,
                            proof=proof,
                        )
                    )

                    total_raw_score += actual_score
                    section_total += weighted_percent
                    section_raw_total += actual_score

                section_weighted_totals[section.id] = section_total

                section_score_rows.append(
                    IFRS9SectionScore(
                        evaluation=evaluation,
                        section=section,
                        raw_score=section_raw_total,
                        weighted_percent=section_total,
                    )
                )

            if driver_score_rows:
                IFRS9RiskDriverScore.objects.bulk_create(
                    driver_score_rows,
                    batch_size=BULK_WRITE_BATCH_SIZE,
                )
            if section_score_rows:
                IFRS9SectionScore.objects.bulk_create(
                    section_score_rows,
                    batch_size=BULK_WRITE_BATCH_SIZE,
                )
            driver_version_payload = [
                {
                    "risk_driver_id": row.risk_driver_id,
                    "raw_score": row.raw_score,
                    "weighted_percent": row.weighted_percent,
                    "proof": row.proof,
                }
                for row in driver_score_rows
            ]
            section_version_payload = [
                {
                    "section_id": row.section_id,
                    "raw_score": row.raw_score,
                    "weighted_percent": row.weighted_percent,
                }
                for row in section_score_rows
            ]
            
            # Calculate total_weighted_percent as sum of section totals
            total_weighted_percent = sum(section_weighted_totals.values())

            final_grade = ""
            
            # Use maker-checker workflow for submissions
            if not evaluation.maker:
                evaluation.maker = request.user
            
            if 'old_status' not in locals():
                old_status = evaluation.status
                
            if not is_draft:
                # Check if there are already approved scores before updating
                if old_status == 'approved' or evaluation.approved_weighted_percent is not None:
                    if evaluation.total_weighted_percent is not None and evaluation.approved_weighted_percent is None:
                        evaluation.approved_weighted_percent = evaluation.total_weighted_percent
                        evaluation.approved_grade = ""
                    elif evaluation.approved_weighted_percent is not None:
                        evaluation.total_weighted_percent = evaluation.approved_weighted_percent
                        evaluation.final_grade = ""
                else:
                    evaluation.total_raw_score = total_raw_score
                    evaluation.total_weighted_percent = total_weighted_percent
                    evaluation.final_grade = final_grade
                
                evaluation.previous_weighted_percent = evaluation.total_weighted_percent
                evaluation.previous_grade = ""
                
                auto_approve = _can_auto_approve_ifrs9_submission(request.user)

                evaluation.status = 'approved' if auto_approve else 'submitted'
                evaluation.submitted_by = request.user
                evaluation.submitted_at = timezone.now()
                
                # Increment version number for resubmissions
                if old_status in ['approved', 'submitted', 'returned']:
                    evaluation.version = (evaluation.version or 0) + 1
                    evaluation.resubmission_count = (evaluation.resubmission_count or 0) + 1
                elif not evaluation.version:
                    evaluation.version = 1
                
            # Save the evaluation
            evaluation.save()
            
            # Create version snapshot if not a draft
            if not is_draft:
                last_version = evaluation.versions.order_by('-version_number').first()
                if last_version:
                    final_version_number = last_version.version_number + 1
                else:
                    final_version_number = evaluation.version or 1
                
                # Keep old unapproved/returned versions too; every submission is part of the audit trail.
                _create_ifrs9_evaluation_version(
                    evaluation=evaluation,
                    version_number=final_version_number,
                    user=request.user,
                    change_description=f"IFRS9 score form submitted for review - Version {final_version_number} (Score: {total_weighted_percent:.2f}%)",
                    total_weighted_percent=total_weighted_percent,
                    final_grade=final_grade,
                    total_raw_score=total_raw_score,
                    attribute_version_payload=attribute_version_payload,
                    driver_version_payload=driver_version_payload,
                    section_version_payload=section_version_payload,
                )
                if not auto_approve:
                    log_ifrs9_score_audit(
                        request.user,
                        "submit",
                        evaluation,
                        f"IFRS9 score form submitted for review with score {total_weighted_percent:.2f}% from status {old_status or 'in_progress'}.",
                    )

            if is_draft:
                messages.success(
                    request,
                    f"Draft saved successfully! You can continue working on it later.",
                )
                return redirect("scorecard:ifrs9_scores_draft_list")
            else:
                if old_status == 'approved':
                    comments = 'IFRS9 score form edited from approved state - resubmitted for review'
                    auto_comments = 'IFRS9 score form edited from approved state - auto-approved on submission'
                elif old_status == 'returned':
                    comments = 'IFRS9 score form resubmitted after being returned for changes'
                    auto_comments = 'IFRS9 score form resubmitted after return and auto-approved on submission'
                else:
                    comments = 'IFRS9 score form updated and submitted for review'
                    auto_comments = 'IFRS9 score form updated and auto-approved on submission'

                if auto_approve:
                    _finalize_ifrs9_auto_approval(
                        evaluation,
                        request.user,
                        old_status=old_status if old_status else 'in_progress',
                        comments=auto_comments,
                    )
                    evaluation.refresh_from_db()
                    log_ifrs9_score_audit(
                        request.user,
                        "approve",
                        evaluation,
                        f"IFRS9 score form auto-approved on submission with score {evaluation.total_weighted_percent:.2f}% from status {old_status or 'in_progress'}.",
                    )
                    messages.success(
                        request,
                        f"IFRS9 score form approved automatically on submit. Total Score: {evaluation.total_weighted_percent}%.",
                    )
                    notify_ifrs9_approved(evaluation, request.user)
                else:
                    IFRS9EvaluationWorkflowHistory.objects.create(
                        evaluation=evaluation,
                        action='submitted',
                        from_status=old_status if old_status else 'in_progress',
                        to_status='submitted',
                        performed_by=request.user,
                        comments=comments
                    )
                    messages.success(
                        request,
                        f"IFRS9 score form submitted for review! Total Score: {total_weighted_percent}%. "
                        f"Waiting for checker approval.",
                    )
                    notify_ifrs9_submitted(evaluation)
                return redirect("scorecard:maker_ifrs9_scores_submitted_list")

    # GET request - show edit form with existing data

    # Get existing attribute responses for pre-population
    attribute_responses_list = list(evaluation.attribute_responses.select_related('option').prefetch_related('documents').all())

    approved_evaluation_version = None
    if evaluation.status in {"approved", "completed"}:
        approved_evaluation_version = (
            evaluation.versions.filter(is_approved=True)
            .order_by("-approved_at", "-version_number")
            .first()
        )

    if template_switched and original_template is not None:
        existing_responses, attribute_responses = _map_ifrs9_responses_to_replacement_template(
            attribute_responses_list,
            template,
        )
    else:
        # Create attribute_responses dictionary for template access
        attribute_responses = {}
        for response in attribute_responses_list:
            attribute_responses[response.attribute_id] = response
        if approved_evaluation_version is not None:
            version_responses = list(
                approved_evaluation_version.attribute_responses.select_related(
                    "attribute",
                    "option",
                ).all()
            )
            existing_responses = _build_ifrs9_form_values_from_responses(
                version_responses
            )
        else:
            existing_responses = _build_ifrs9_form_values_from_responses(
                attribute_responses_list
            )

    initial_saved_weighted_percent = (
        approved_evaluation_version.total_weighted_percent
        if approved_evaluation_version is not None
        else evaluation.total_weighted_percent
    )

    current_autofill_payload = (
        _build_ifrs9_autofill_payload(
            request,
            template,
            attributes_by_driver,
            customer_ref_code=evaluation.customer_id,
        )
        if evaluation.customer_id
        else _empty_autofill_payload()
    )
    saved_autofill_metadata = evaluation.autofill_metadata or {}
    autofill_profile_changes = _build_profile_change_rows(
        saved_autofill_metadata,
        current_autofill_payload,
    )
    display_autofill_metadata = saved_autofill_metadata
    template_assignment_metadata = saved_autofill_metadata.get("template_assignment", {})
    should_seed_autofill_values = (
        not attribute_responses_list
        and approved_evaluation_version is None
        and not existing_responses
        and not template_assignment_metadata.get("previous_template_code")
        and bool(current_autofill_payload.get("attribute_values"))
    )
    if should_seed_autofill_values:
        existing_responses = current_autofill_payload.get("attribute_values", {}) or {}
        display_autofill_metadata = current_autofill_payload
        autofill_profile_changes = []

    context = {
        "template": template,
        "sections": sections,
        "drivers_by_section": drivers_by_section,
        "attributes_by_driver": attributes_by_driver,
        "evaluation": evaluation,
        "attribute_responses": attribute_responses,
        "template_switch_notice": template_switch_notice,
        "initial_saved_weighted_percent": initial_saved_weighted_percent,
        "form_data": {
            "branch_name": evaluation.branch_name,
            "customer_name": evaluation.customer_name,
            "customer_id": evaluation.customer_id,
            "attribute_values": existing_responses,
        },
        "autofill_applied_labels": display_autofill_metadata.get("applied_labels", []),
        "autofill_missing_labels": display_autofill_metadata.get(
            "current_missing_required_labels",
            display_autofill_metadata.get("missing_required_labels", []),
        ),
        "autofill_applied_attribute_ids": display_autofill_metadata.get("applied_attribute_ids", []),
        "autofill_missing_attribute_ids": display_autofill_metadata.get(
            "current_missing_required_ids",
            display_autofill_metadata.get("missing_required_attribute_ids", []),
        ),
        "autofill_profile_changes": autofill_profile_changes,
    }

    return render(
        request,
        "ifrs9_score_config/ifrs9_form/ifrs9_scores_edit.html",
        context,
    )


@login_required
def ifrs9_scores_view_detail(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    View to display an IFRS9 score form in read-only mode.
    Shows the same structure as the form but with submitted data.
    
    For approved score forms: Shows the approved version's data
    For submitted score forms: Shows current data (pending approval) with indication
    """
    evaluation = get_object_or_404(
        IFRS9Evaluation.objects.select_related("template").prefetch_related(
            "section_scores__section",
            "driver_scores__risk_driver__section",
            "attribute_responses__attribute__risk_driver",
            "attribute_responses__option",
            "attribute_responses__attribute__options",
            "attribute_responses__documents__uploaded_by",
            "versions__attribute_responses__attribute",
            "versions__attribute_responses__option",
            "versions__driver_scores__risk_driver",
            "versions__section_scores__section",
        ),
        id=evaluation_id,
    )

    template = evaluation.template
    if not evaluation.template_id or template is None:
        messages.info(
            request,
            "Choose an IFRS9 template first to view this score form in the full template layout.",
        )
        return redirect("scorecard:ifrs9_scores_template_assignment", evaluation_id=evaluation.id)

    # Use approved template version structure to ensure consistency
    sections, drivers_by_section, attributes_by_driver = _build_configuration(template, use_approved_version=True)

    is_preview_mode = request.GET.get("preview") == "1"

    # For approved score forms, get the latest approved version's data
    # For submitted/other statuses, use current evaluation data
    approved_version = None
    latest_unapproved_version = evaluation.versions.filter(is_approved=False).order_by('-version_number').first()
    if evaluation.status == 'approved':
        # Get the latest approved version
        approved_version = evaluation.versions.filter(is_approved=True).order_by('-approved_at', '-version_number').first()
    
    if approved_version:
        # Use approved version's data, but include documents from current evaluation
        current_responses = {}
        for response in evaluation.attribute_responses.prefetch_related('documents__uploaded_by').all():
            current_responses[response.attribute_id] = response
        
        attribute_responses = {}
        for version_response in approved_version.attribute_responses.all():
            current_response = current_responses.get(version_response.attribute_id)
            attribute_responses[version_response.attribute_id] = {
                'option_id': version_response.option_id,
                'option': version_response.option,
                'allocated_score': version_response.allocated_score,
                'weighted_percent': getattr(version_response, 'weighted_percent', None),
                'raw_value': version_response.raw_value,
                'documents': current_response.documents.all() if current_response else [],
            }
        # Ensure every attribute in the template has an entry (sentinel when no response)
        _sentinel = {
            'option_id': None,
            'option': None,
            'allocated_score': None,
            'weighted_percent': None,
            'documents': [],
            'raw_value': None,
        }
        for _section in sections:
            for _driver in drivers_by_section.get(_section.id, []):
                for _attr in attributes_by_driver.get(_driver.id, []):
                    if _attr.id not in attribute_responses:
                        attribute_responses[_attr.id] = _sentinel
        
        driver_scores = {}
        for version_score in approved_version.driver_scores.all():
            driver_scores[version_score.risk_driver_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
        
        section_scores = {}
        for version_score in approved_version.section_scores.all():
            section_scores[version_score.section_id] = {
                'raw_score': version_score.raw_score,
                'weighted_percent': version_score.weighted_percent,
            }
        
        # Use approved version's total score
        display_weighted_percent = approved_version.total_weighted_percent
    else:
        # Use current evaluation data
        attribute_responses = {}
        for response in evaluation.attribute_responses.prefetch_related('documents__uploaded_by').all():
            attribute_responses[response.attribute_id] = response
        # Ensure every attribute in the template has an entry (sentinel when no response)
        _sentinel = {
            'option_id': None,
            'option': None,
            'allocated_score': None,
            'weighted_percent': None,
            'documents': [],
            'raw_value': None,
        }
        for _section in sections:
            for _driver in drivers_by_section.get(_section.id, []):
                for _attr in attributes_by_driver.get(_driver.id, []):
                    if _attr.id not in attribute_responses:
                        attribute_responses[_attr.id] = _sentinel
        
        driver_scores = {}
        for score in evaluation.driver_scores.all():
            driver_scores[score.risk_driver_id] = score
        
        section_scores = {}
        for score in evaluation.section_scores.all():
            section_scores[score.section_id] = score
        
        display_weighted_percent = evaluation.total_weighted_percent

    # Get all history records
    history_records = evaluation.history_records.all().order_by("-recorded_at")
    
    # Get all versions for this evaluation
    all_versions = list(evaluation.versions.all().order_by('-version_number'))
    
    # Get the latest version to show who last modified the current version
    latest_version = all_versions[0] if all_versions else None
    
    # Check if there's a pending submission (status is 'submitted')
    has_pending_submission = evaluation.status == 'submitted'
    workflow_history = list(
        evaluation.workflow_history.select_related("performed_by").all()
    )[:10]

    context = {
        "evaluation": evaluation,
        "template": template,
        "sections": sections,
        "drivers_by_section": drivers_by_section,
        "attributes_by_driver": attributes_by_driver,
        "attribute_responses": attribute_responses,
        "driver_scores": driver_scores,
        "section_scores": section_scores,
        "history_records": history_records,
        "all_versions": all_versions,
        "latest_version": latest_version,
        "approved_version": approved_version,
        "display_weighted_percent": display_weighted_percent,
        "has_pending_submission": has_pending_submission,
        "active_autofill_metadata": _get_active_autofill_metadata(
            evaluation,
            preferred_version=approved_version,
            fallback_version=latest_unapproved_version,
        ),
        "is_preview_mode": is_preview_mode,
        "workflow_history": workflow_history,
    }

    template_name = "ifrs9_score_config/ifrs9_form/ifrs9_scores_view.html"
    if getattr(request.resolver_match, "url_name", "") == "checker_my_approvals_ifrs9_score_view":
        template_name = "maker_checker/checker_ifrs9_score_approval_view.html"

    return render(
        request,
        template_name,
        context,
    )


@login_required
def ifrs9_scores_compare_versions(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    View to compare two versions of an IFRS9 evaluation.
    Shows side-by-side comparison of all attribute responses, driver scores, section scores, and summary.
    """
    evaluation = get_object_or_404(
        IFRS9Evaluation.objects.select_related("template").prefetch_related("versions"),
        id=evaluation_id,
    )
    
    template = evaluation.template
    if not evaluation.template_id or template is None:
        messages.info(
            request,
            "Choose an IFRS9 template first before comparing versions for this score form.",
        )
        return redirect("scorecard:ifrs9_scores_template_assignment", evaluation_id=evaluation.id)

    # Use approved template version structure to ensure consistency
    sections, drivers_by_section, attributes_by_driver = _build_configuration(template, use_approved_version=True)
    
    # Get version IDs from query parameters
    version1_id = request.GET.get('version1')
    version2_id = request.GET.get('version2')
    
    # Get all versions for this evaluation
    all_versions = list(evaluation.versions.all().order_by('-version_number'))
    versions_by_id = {version.id: version for version in all_versions}
    total_versions = len(all_versions)
    latest_version = all_versions[0] if all_versions else None
    
    version1 = None
    version2 = None
    
    if version1_id:
        try:
            version1 = versions_by_id.get(int(version1_id))
        except ValueError:
            pass
    
    if version2_id:
        try:
            version2 = versions_by_id.get(int(version2_id))
        except ValueError:
            pass
    
    # If no versions selected, default to latest approved version vs latest version
    approved_versions = sorted(
        [version for version in all_versions if version.is_approved],
        key=lambda version: (version.approved_at or version.created_at, version.version_number),
        reverse=True,
    )
    latest_approved_version = approved_versions[0] if approved_versions else None
    
    if not version1 and not version2:
        if latest_approved_version and total_versions >= 2:
            version2 = latest_version  # Latest version
            if latest_approved_version != version2:
                version1 = latest_approved_version
            else:
                if total_versions >= 2:
                    version1 = all_versions[1]
        elif total_versions >= 2:
            version2 = latest_version
            version1 = all_versions[1]
        elif total_versions >= 1:
            version2 = latest_version
    elif not version1 and total_versions >= 1:
        if latest_approved_version and latest_approved_version != version2:
            version1 = latest_approved_version
        else:
            version2 = latest_version
    elif not version2 and total_versions >= 1:
        version2 = latest_version
    
    # Ensure version1 is always older than version2
    if version1 and version2:
        v1_is_newer = False
        
        if hasattr(version1, 'version_number') and hasattr(version2, 'version_number'):
            if version1.version_number > version2.version_number:
                v1_is_newer = True
            elif version1.version_number == version2.version_number:
                if hasattr(version1, 'created_at') and hasattr(version2, 'created_at'):
                    if version1.created_at > version2.created_at:
                        v1_is_newer = True
        elif hasattr(version1, 'created_at') and hasattr(version2, 'created_at'):
            if version1.created_at > version2.created_at:
                v1_is_newer = True
        
        if v1_is_newer:
            version1, version2 = version2, version1
    
    # Prepare comparison data
    comparison_data = {
        'version1': None,
        'version2': None,
        'attribute_differences': {},
        'driver_differences': {},
        'section_differences': {},
        'summary_differences': {},
    }
    
    if version1:
        v1_attrs = {r.attribute_id: r for r in version1.attribute_responses.all()}
        v1_drivers = {s.risk_driver_id: s for s in version1.driver_scores.all()}
        v1_sections = {s.section_id: s for s in version1.section_scores.all()}
        
        comparison_data['version1'] = {
            'version': version1,
            'attributes': v1_attrs,
            'drivers': v1_drivers,
            'sections': v1_sections,
            'summary': {
                'total_raw_score': version1.total_raw_score,
                'total_weighted_percent': version1.total_weighted_percent,
            }
        }
    
    if not version2:
        v2_attrs = {r.attribute_id: r for r in evaluation.attribute_responses.all()}
        v2_drivers = {s.risk_driver_id: s for s in evaluation.driver_scores.all()}
        v2_sections = {s.section_id: s for s in evaluation.section_scores.all()}
        
        comparison_data['version2'] = {
            'version': None,
            'attributes': v2_attrs,
            'drivers': v2_drivers,
            'sections': v2_sections,
            'summary': {
                'total_raw_score': evaluation.total_raw_score,
                'total_weighted_percent': evaluation.total_weighted_percent,
            }
        }
    else:
        v2_attrs = {r.attribute_id: r for r in version2.attribute_responses.all()}
        v2_drivers = {s.risk_driver_id: s for s in version2.driver_scores.all()}
        v2_sections = {s.section_id: s for s in version2.section_scores.all()}
        
        comparison_data['version2'] = {
            'version': version2,
            'attributes': v2_attrs,
            'drivers': v2_drivers,
            'sections': v2_sections,
            'summary': {
                'total_raw_score': version2.total_raw_score,
                'total_weighted_percent': version2.total_weighted_percent,
            }
        }
    
    # Compare attributes, drivers, sections, and summary
    if comparison_data['version1'] and comparison_data['version2']:
        v1_attrs = comparison_data['version1']['attributes']
        v2_attrs = comparison_data['version2']['attributes']
        
        all_attr_ids = set(v1_attrs.keys()) | set(v2_attrs.keys())
        
        for attr_id in all_attr_ids:
            v1_attr = v1_attrs.get(attr_id)
            v2_attr = v2_attrs.get(attr_id)
            
            attribute = None
            for driver_id, attrs in attributes_by_driver.items():
                for attr in attrs:
                    if attr.id == attr_id:
                        attribute = attr
                        break
                if attribute:
                    break
            
            v1_weighted_score = None
            v2_weighted_score = None
            weighted_diff = None
            
            if attribute:
                max_option = attribute.options.aggregate(Max('allocated_score'))
                highest_possible_score = Decimal(str(max_option['allocated_score__max'] or 0))
                weight_percent = Decimal(str(attribute.weight_percent or 0))
                
                if v1_attr and highest_possible_score > 0:
                    actual_score_v1 = Decimal(str(v1_attr.allocated_score))
                    v1_weighted_score = float((actual_score_v1 / highest_possible_score) * weight_percent)
                
                if v2_attr and highest_possible_score > 0:
                    actual_score_v2 = Decimal(str(v2_attr.allocated_score))
                    v2_weighted_score = float((actual_score_v2 / highest_possible_score) * weight_percent)
                
                if v1_weighted_score is not None and v2_weighted_score is not None:
                    weighted_diff = v2_weighted_score - v1_weighted_score
                elif v1_weighted_score is not None:
                    weighted_diff = -v1_weighted_score
                elif v2_weighted_score is not None:
                    weighted_diff = v2_weighted_score
            
            score_diff = None
            if v1_attr and v2_attr:
                score_diff = float(v2_attr.allocated_score) - float(v1_attr.allocated_score)
            elif v1_attr:
                score_diff = -float(v1_attr.allocated_score)
            elif v2_attr:
                score_diff = float(v2_attr.allocated_score)
            
            if v1_attr and v2_attr:
                if (v1_attr.option_id != v2_attr.option_id or 
                    v1_attr.raw_value != v2_attr.raw_value or
                    v1_attr.allocated_score != v2_attr.allocated_score):
                    comparison_data['attribute_differences'][attr_id] = {
                        'v1': v1_attr,
                        'v2': v2_attr,
                        'score_diff': score_diff,
                        'v1_weighted_score': v1_weighted_score,
                        'v2_weighted_score': v2_weighted_score,
                        'weighted_diff': weighted_diff,
                    }
            elif v1_attr or v2_attr:
                comparison_data['attribute_differences'][attr_id] = {
                    'v1': v1_attr,
                    'v2': v2_attr,
                    'score_diff': score_diff,
                    'v1_weighted_score': v1_weighted_score,
                    'v2_weighted_score': v2_weighted_score,
                    'weighted_diff': weighted_diff,
                }
        
        # Compare drivers
        v1_drivers = comparison_data['version1']['drivers']
        v2_drivers = comparison_data['version2']['drivers']
        
        all_driver_ids = set(v1_drivers.keys()) | set(v2_drivers.keys())
        
        for driver_id in all_driver_ids:
            v1_driver = v1_drivers.get(driver_id)
            v2_driver = v2_drivers.get(driver_id)
            
            weighted_diff = None
            raw_diff = None
            if v1_driver and v2_driver:
                weighted_diff = float(v2_driver.weighted_percent) - float(v1_driver.weighted_percent)
                raw_diff = float(v2_driver.raw_score) - float(v1_driver.raw_score)
            elif v1_driver:
                weighted_diff = -float(v1_driver.weighted_percent)
                raw_diff = -float(v1_driver.raw_score)
            elif v2_driver:
                weighted_diff = float(v2_driver.weighted_percent)
                raw_diff = float(v2_driver.raw_score)
            
            if v1_driver and v2_driver:
                if (v1_driver.raw_score != v2_driver.raw_score or
                    v1_driver.weighted_percent != v2_driver.weighted_percent or
                    v1_driver.proof != v2_driver.proof):
                    comparison_data['driver_differences'][driver_id] = {
                        'v1': v1_driver,
                        'v2': v2_driver,
                        'weighted_diff': weighted_diff,
                        'raw_diff': raw_diff,
                    }
            elif v1_driver or v2_driver:
                comparison_data['driver_differences'][driver_id] = {
                    'v1': v1_driver,
                    'v2': v2_driver,
                    'weighted_diff': weighted_diff,
                    'raw_diff': raw_diff,
                }
        
        # Compare sections
        v1_sections = comparison_data['version1']['sections']
        v2_sections = comparison_data['version2']['sections']
        
        all_section_ids = set(v1_sections.keys()) | set(v2_sections.keys())
        
        for section_id in all_section_ids:
            v1_section = v1_sections.get(section_id)
            v2_section = v2_sections.get(section_id)
            
            weighted_diff = None
            raw_diff = None
            if v1_section and v2_section:
                weighted_diff = float(v2_section.weighted_percent) - float(v1_section.weighted_percent)
                raw_diff = float(v2_section.raw_score) - float(v1_section.raw_score)
            elif v1_section:
                weighted_diff = -float(v1_section.weighted_percent)
                raw_diff = -float(v1_section.raw_score)
            elif v2_section:
                weighted_diff = float(v2_section.weighted_percent)
                raw_diff = float(v2_section.raw_score)
            
            if v1_section and v2_section:
                if (v1_section.raw_score != v2_section.raw_score or
                    v1_section.weighted_percent != v2_section.weighted_percent):
                    comparison_data['section_differences'][section_id] = {
                        'v1': v1_section,
                        'v2': v2_section,
                        'weighted_diff': weighted_diff,
                        'raw_diff': raw_diff,
                    }
            elif v1_section or v2_section:
                comparison_data['section_differences'][section_id] = {
                    'v1': v1_section,
                    'v2': v2_section,
                    'weighted_diff': weighted_diff,
                    'raw_diff': raw_diff,
                }
        
        # Compare summary
        v1_summary = comparison_data['version1']['summary']
        v2_summary = comparison_data['version2']['summary']
        
        total_weighted_diff = None
        if v1_summary['total_weighted_percent'] is not None and v2_summary['total_weighted_percent'] is not None:
            total_weighted_diff = float(v2_summary['total_weighted_percent']) - float(v1_summary['total_weighted_percent'])
        
        total_raw_diff = None
        if v1_summary['total_raw_score'] is not None and v2_summary['total_raw_score'] is not None:
            total_raw_diff = float(v2_summary['total_raw_score']) - float(v1_summary['total_raw_score'])
        
        if (
            v1_summary['total_raw_score'] != v2_summary['total_raw_score']
            or v1_summary['total_weighted_percent'] != v2_summary['total_weighted_percent']
        ):
            comparison_data['summary_differences'] = {
                'v1': v1_summary,
                'v2': v2_summary,
                'total_weighted_diff': total_weighted_diff,
                'total_raw_diff': total_raw_diff,
            }
    
    # Create lookup dictionaries
    all_attributes = {}
    for driver_id, attrs in attributes_by_driver.items():
        for attr in attrs:
            all_attributes[attr.id] = attr
    
    all_drivers = {}
    for section_id, drivers in drivers_by_section.items():
        for driver in drivers:
            all_drivers[driver.id] = driver
    
    all_sections_dict = {s.id: s for s in sections}
    
    context = {
        'evaluation': evaluation,
        'template': template,
        'sections': sections,
        'all_sections_dict': all_sections_dict,
        'drivers_by_section': drivers_by_section,
        'all_drivers': all_drivers,
        'attributes_by_driver': attributes_by_driver,
        'all_attributes': all_attributes,
        'all_versions': all_versions,
        'version1': version1,
        'version2': version2,
        'comparison_data': comparison_data,
    }
    
    return render(
        request,
        'ifrs9_score_config/ifrs9_form/ifrs9_scores_compare.html',
        context,
    )


@login_required
def ifrs9_scores_delete_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    View to delete an IFRS9 score form/evaluation.
    """
    evaluation = get_object_or_404(
        IFRS9Evaluation.objects.select_related("template"),
        id=evaluation_id,
    )

    # Lock deletion if status is 'submitted' (must withdraw first)
    # Approved score forms CAN be deleted
    # Returned score forms CAN be deleted ONLY from the maker draft list
    if evaluation.status == 'submitted':
        messages.error(
            request,
            f"This IFRS9 score form is submitted for review. "
            f"It cannot be deleted. Please withdraw the submission first."
        )
        return redirect("scorecard:ifrs9_scores_view_detail", evaluation_id=evaluation.id)
    
    if evaluation.status not in ['draft', 'in_progress', 'returned', 'approved']:
        messages.error(request, "Only draft, in-progress, returned, or approved IFRS9 score forms can be deleted.")
        return redirect("scorecard:ifrs9_scores_view_detail", evaluation_id=evaluation.id)
    if not evaluation.can_be_edited_by(request.user):
        messages.error(request, "You don't have permission to delete this IFRS9 score form.")
        return redirect("scorecard:ifrs9_scores_view_detail", evaluation_id=evaluation.id)

    if request.method == "POST":
        customer_name = evaluation.customer_name
        template_code, _ = _get_ifrs9_template_label(evaluation)
        
        deleted_status = evaluation.status
        log_ifrs9_score_audit(
            request.user,
            "delete",
            evaluation,
            f"IFRS9 score form deleted from status {deleted_status}.",
        )
        
        # Delete the evaluation (cascade deletes all related objects)
        evaluation.delete()
        
        messages.success(
            request,
            f"IFRS9 score form for '{customer_name}' (Template: {template_code}) has been permanently deleted.",
        )
        
        # Redirect based on status
        if deleted_status == 'returned':
            return redirect("scorecard:maker_ifrs9_scores_draft_list")
        else:
            return redirect("scorecard:ifrs9_scores_submitted_list")

    context = {
        "evaluation": evaluation,
    }

    return render(
        request,
        "ifrs9_score_config/ifrs9_form/ifrs9_scores_delete.html",
        context,
    )


@login_required
def ifrs9_scores_check_existing(request: HttpRequest) -> JsonResponse:
    """
    API endpoint to check if a customer already has an IFRS9 score form.
    Returns JSON with exists flag and evaluation_id if found.
    """
    customer_code = request.GET.get('customer_code', '').strip()
    branch_code = request.GET.get('branch_code', '').strip()
    branch_name = request.GET.get('branch_name', '').strip()

    if not customer_code:
        return JsonResponse({
            'exists': False,
            'evaluation_id': None,
            'customer_name': None,
        })

    branch = resolve_branch_context(request, branch_code=branch_code, branch_name=branch_name)
    if branch is None:
        return JsonResponse({
            'exists': False,
            'evaluation_id': None,
            'customer_name': None,
        })

    try:
        locked_payload = _get_locked_existing_ifrs9_score_payload(customer_code, current_branch_name=branch.branch_name)
        if locked_payload is not None:
            return JsonResponse(locked_payload)

        existing_evaluation = IFRS9Evaluation.objects.filter(
            customer_id=customer_code,
            branch_name=branch.branch_name,
        ).first()

        if existing_evaluation:
            return JsonResponse(
                _build_existing_ifrs9_score_payload(
                    existing_evaluation,
                    action_mode='edit',
                )
            )

        return JsonResponse({
            'exists': False,
            'evaluation_id': None,
            'customer_name': None,
        })
    except BankBranch.DoesNotExist:
        return JsonResponse({
            'exists': False,
            'evaluation_id': None,
            'customer_name': None,
        })


@login_required
def ifrs9_customer_versions_list_view(request: HttpRequest) -> HttpResponse:
    """
    View to list all customers that have IFRS9 score forms with versions.
    Shows customers grouped by their evaluations.
    """
    search_query = (request.GET.get("q") or "").strip()
    customer_summaries = IFRS9Evaluation.objects.filter(versions__isnull=False)

    branch_names = get_request_branch_names(request)
    if branch_names:
        customer_summaries = customer_summaries.filter(branch_name__in=branch_names)
    elif not request.user.is_superuser:
        customer_summaries = customer_summaries.none()

    if search_query:
        customer_summaries = customer_summaries.filter(
            Q(customer_id__icontains=search_query)
            | Q(customer_name__icontains=search_query)
            | Q(branch_name__icontains=search_query)
        )

    customer_summaries = customer_summaries.values(
        "customer_id",
        "customer_name",
        "branch_name",
    ).annotate(
        latest_created_at=Max("created_at"),
        evaluation_count=Count("id"),
        version_total=Count("versions"),
    ).order_by("-latest_created_at", "customer_name", "customer_id")

    page_obj, search_query, page_size = _paginate_list_queryset(request, customer_summaries)
    page_customers = list(page_obj.object_list)

    evaluations_by_customer: dict[tuple[str, str, str], list[IFRS9Evaluation]] = {}
    if page_customers:
        evaluation_filters = Q(pk__in=[])
        for customer in page_customers:
            evaluation_filters |= Q(
                customer_id=customer["customer_id"],
                customer_name=customer["customer_name"],
                branch_name=customer["branch_name"],
            )
        page_evaluations = (
            IFRS9Evaluation.objects.filter(evaluation_filters, versions__isnull=False)
            .select_related("template")
            .annotate(version_count=Count("versions"))
            .order_by("-created_at", "-id")
        )
        for evaluation in page_evaluations:
            customer_key = (
                evaluation.customer_id,
                evaluation.customer_name,
                evaluation.branch_name,
            )
            evaluations_by_customer.setdefault(customer_key, []).append(evaluation)

    customers = []
    for customer in page_customers:
        customer_key = (
            customer["customer_id"],
            customer["customer_name"],
            customer["branch_name"],
        )
        customers.append(
            {
                "customer_id": customer["customer_id"],
                "customer_name": customer["customer_name"],
                "branch_name": customer["branch_name"],
                "evaluation_count": customer["evaluation_count"],
                "version_total": customer["version_total"],
                "evaluations": evaluations_by_customer.get(customer_key, []),
            }
        )

    context = {
        "customers": customers,
        "page_obj": page_obj,
        "search_query": search_query,
        "page_size": page_size,
        "list_query_string": _build_list_query_string(
            search_query=search_query,
            page_size=page_size,
        ),
    }
    
    return render(
        request,
        "ifrs9_score_config/ifrs9_form/customer_versions_list.html",
        context,
    )


@login_required
def ifrs9_customer_evaluation_versions_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    View to show all versions for a specific customer IFRS9 evaluation.
    Lists all versions with their details.
    """
    evaluation = get_object_or_404(
        IFRS9Evaluation.objects.select_related("template", "maker", "submitted_by", "approved_by"),
        id=evaluation_id,
    )
    
    # Get all versions for this evaluation, ordered by version number
    all_versions = evaluation.versions.select_related("created_by", "approved_by").order_by('-version_number')
    
    context = {
        "evaluation": evaluation,
        "all_versions": all_versions,
    }
    
    return render(
        request,
        "ifrs9_score_config/ifrs9_form/customer_evaluation_versions.html",
        context,
    )


@login_required
def ifrs9_customer_version_detail_view(request: HttpRequest, version_id: int) -> HttpResponse:
    """
    View to display a specific IFRS9 version in full detail with all selected options.
    Shows complete version snapshot with all attribute responses, scores, etc.
    """
    version = get_object_or_404(
        IFRS9EvaluationVersion.objects.select_related(
            "evaluation__template",
            "evaluation",
            "created_by",
            "approved_by",
        ).prefetch_related(
            "attribute_responses__attribute__risk_driver__section",
            "attribute_responses__option",
            "attribute_responses__attribute__options",
            "driver_scores__risk_driver__section",
            "section_scores__section",
        ),
        id=version_id,
    )
    
    evaluation = version.evaluation
    template = evaluation.template
    # Use approved template version structure to ensure consistency
    sections, drivers_by_section, attributes_by_driver = _build_configuration(template, use_approved_version=True)
    
    # Get attribute responses from version
    attribute_responses = {}
    for response in version.attribute_responses.all():
        attribute_responses[response.attribute_id] = response
    
    # Get driver scores from version
    driver_scores = {}
    for score in version.driver_scores.all():
        driver_scores[score.risk_driver_id] = score
    
    # Get section scores from version
    section_scores = {}
    for score in version.section_scores.all():
        section_scores[score.section_id] = score
    
    context = {
        "version": version,
        "evaluation": evaluation,
        "template": template,
        "sections": sections,
        "drivers_by_section": drivers_by_section,
        "attributes_by_driver": attributes_by_driver,
        "attribute_responses": attribute_responses,
        "driver_scores": driver_scores,
        "section_scores": section_scores,
        "display_weighted_percent": version.total_weighted_percent,
    }
    
    return render(
        request,
        "ifrs9_score_config/ifrs9_form/customer_version_detail.html",
        context,
    )
