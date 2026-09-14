from decimal import Decimal, ROUND_HALF_UP
import copy
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import (
    Case,
    CharField,
    Count,
    DecimalField,
    IntegerField,
    Max,
    OuterRef,
    Q,
    Subquery,
    When,
)
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
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
    build_basel_autofill,
    compare_profile_snapshots,
)
from scorecard.functions_view.notifications import (
    notify_basel_approved,
    notify_basel_submitted,
)
from scorecard.functions_view.audit import log_basel_score_audit
from scorecard.workflow_approval import (
    is_counterpart_completion_enforced,
    is_cross_branch_duplicate_scoring_prevented,
    should_auto_approve_scorecard_workflow,
)
from scorecard.models import (
    Attribute,
    AttributeResponse,
    AttributeResponseDocument,
    BankBranch,
    BaselScoreSheetTemplate,
    CreditEvaluation,
    EvaluationWorkflowHistory,
    IFRS9Evaluation,
    EvaluationHistory,
    EvaluationVersion,
    EvaluationVersionAttributeResponse,
    EvaluationVersionDriverScore,
    EvaluationVersionSectionScore,
    GradeBand,
    Option,
    RiskDriver,
    RiskDriverScore,
    Section,
    SectionScore,
    TemplateVersion,
)


BULK_WRITE_BATCH_SIZE = 500
LIST_PAGE_SIZE_OPTIONS = (20, 50, 100)
SUBMITTED_LIST_SUMMARY_CACHE_TTL_SECONDS = 120


def _can_auto_approve_basel_submission(user) -> bool:
    return should_auto_approve_scorecard_workflow(
        user,
        "basel_scores",
        "scorecard.review_basel_scores",
    )


def _can_override_basel_grade(user) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    return bool(
        getattr(user, "is_superuser", False)
        or user.has_perm("scorecard.manage_basel_scores")
        or user.has_perm("scorecard.review_basel_scores")
    )


def _finalize_basel_auto_approval(
    evaluation: CreditEvaluation,
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
        evaluation.approved_grade = latest_unapproved_version.final_grade
        evaluation.total_weighted_percent = latest_unapproved_version.total_weighted_percent
        evaluation.final_grade = latest_unapproved_version.final_grade
        evaluation.total_raw_score = latest_unapproved_version.total_raw_score

        latest_unapproved_version.is_approved = True
        latest_unapproved_version.approved_at = approval_time
        latest_unapproved_version.approved_by = acting_user
        latest_unapproved_version.save()

        # Keep older unapproved/returned versions for a complete customer version history.
    else:
        evaluation.approved_weighted_percent = evaluation.total_weighted_percent
        evaluation.approved_grade = evaluation.final_grade

    evaluation.status = "approved"
    evaluation.approved_by = acting_user
    evaluation.approved_at = approval_time
    evaluation.save()

    EvaluationWorkflowHistory.objects.create(
        evaluation=evaluation,
        action="approved",
        from_status=old_status,
        to_status="approved",
        performed_by=acting_user,
        comments=comments,
    )


def _build_basel_autofill_payload(
    request: HttpRequest,
    template: BaselScoreSheetTemplate,
    attributes_by_driver: Dict[int, List[Attribute]],
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
    result = build_basel_autofill(template.code, customer, attributes_by_driver)
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


def _flatten_attributes(attributes_by_driver: Dict[int, List[Attribute]]) -> list[Attribute]:
    return [attribute for attrs in attributes_by_driver.values() for attribute in attrs]


def _build_saved_autofill_metadata(
    autofill_payload: dict[str, object],
    attributes_by_driver: Dict[int, List[Attribute]],
    submitted_values: Dict[int, Any] | None = None,
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
            if not isinstance(submitted_values.get(attribute_id, ""), list)
            and str(submitted_values.get(attribute_id, "")) == option_id
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
    evaluation: CreditEvaluation,
    *,
    preferred_version: EvaluationVersion | None = None,
    fallback_version: EvaluationVersion | None = None,
) -> dict[str, object]:
    if preferred_version and preferred_version.autofill_metadata:
        return preferred_version.autofill_metadata
    if fallback_version and fallback_version.autofill_metadata:
        return fallback_version.autofill_metadata
    return evaluation.autofill_metadata or {}


def _build_configuration(
    template: BaselScoreSheetTemplate,
    use_approved_version: bool = False,
) -> Tuple[List[Section], Dict[int, List[RiskDriver]], Dict[int, List[Attribute]]]:
    """
    Prepares configuration objects for template rendering.
    
    Args:
        template: The template to build configuration from
        use_approved_version: If True, use the approved template version structure instead of current structure
    
    Returns:
        sections: ordered list of sections
        drivers_by_section: mapping section.id -> list of RiskDriver
        attributes_by_driver: mapping driver.id -> list of Attribute
    """
    # If using approved version, build from the approved version snapshot
    if use_approved_version:
        approved_version = template.versions.filter(is_approved=True).order_by('-approved_at', '-version_number').first()
        if approved_version:
            return _build_configuration_from_version(approved_version)
        # If no approved version exists, fall back to current structure
    
    # Build from current template structure
    sections = list(template.sections.all().order_by("display_order", "id"))

    drivers_by_section: Dict[int, List[RiskDriver]] = {}
    attributes_by_driver: Dict[int, List[Attribute]] = {}

    drivers = (
        RiskDriver.objects.filter(section__template=template)
        .select_related("section")
        .order_by("display_order", "id")
    )
    attrs = (
        Attribute.objects.filter(risk_driver__section__template=template)
        .select_related("risk_driver")
        .prefetch_related("options")
        .order_by("display_order", "id")
    )

    for driver in drivers:
        drivers_by_section.setdefault(driver.section_id, []).append(driver)

    for attr in attrs:
        attributes_by_driver.setdefault(attr.risk_driver_id, []).append(attr)

    return sections, drivers_by_section, attributes_by_driver


def _build_configuration_from_version(
    template_version: TemplateVersion,
) -> Tuple[List[Section], Dict[int, List[RiskDriver]], Dict[int, List[Attribute]]]:
    """
    Builds configuration from a template version snapshot.
    This ensures questionnaires use the approved template structure, not pending changes.
    
    Args:
        template_version: The TemplateVersion snapshot to build from
    
    Returns:
        sections: ordered list of sections (from version snapshot)
        drivers_by_section: mapping section.id -> list of RiskDriver (from version snapshot)
        attributes_by_driver: mapping driver.id -> list of Attribute (from version snapshot)
    """
    sections = []
    drivers_by_section: Dict[int, List[RiskDriver]] = {}
    attributes_by_driver: Dict[int, List[Attribute]] = {}
    
    # Build detached in-memory objects from the approved snapshot. Using the live
    # referenced objects here leaks later builder changes into existing score forms.
    for section_version in template_version.sections.all().order_by('display_order'):
        section = copy.copy(section_version.section)
        section.code = section_version.code
        section.name = section_version.name
        section.display_order = section_version.display_order
        sections.append(section)
        drivers_by_section[section.id] = []
        
        for driver_version in section_version.risk_drivers.all().order_by('display_order'):
            driver = copy.copy(driver_version.risk_driver)
            driver.section = section
            driver.code = driver_version.code
            driver.name = driver_version.name
            driver.weight_percent = driver_version.weight_percent
            driver.max_score = driver_version.max_score
            driver.display_order = driver_version.display_order
            drivers_by_section[section.id].append(driver)
            attributes_by_driver[driver.id] = []
            
            for attr_version in driver_version.attributes.all().order_by('display_order'):
                attribute = copy.copy(attr_version.attribute)
                attribute.risk_driver = driver
                attribute.code = attr_version.code
                attribute.label = attr_version.label
                attribute.help_text = attr_version.help_text
                attribute.data_type = attr_version.data_type
                attribute.input_type = attr_version.input_type
                attribute.is_required = attr_version.is_required
                attribute.group_label = attr_version.group_label
                attribute.weight_percent = attr_version.weight_percent
                attribute.display_order = attr_version.display_order
                attribute.requires_document = attr_version.requires_document

                snapshot_options = []
                for option_version in attr_version.options.all().order_by('display_order', 'id'):
                    option = copy.copy(option_version.option)
                    option.attribute = attribute
                    option.label = option_version.label
                    option.value = option_version.value
                    option.allocated_score = option_version.allocated_score
                    option.display_order = option_version.display_order
                    option.is_default = option_version.is_default
                    snapshot_options.append(option)

                attribute._prefetched_objects_cache = {"options": snapshot_options}
                attribute.get_highest_possible_score = (
                    lambda options=snapshot_options, scoring_rule=attribute.scoring_rule:
                    _highest_possible_score_from_options(options, scoring_rule)
                )
                attributes_by_driver[driver.id].append(attribute)
    
    return sections, drivers_by_section, attributes_by_driver


def _field_name_for_attribute(attribute: Attribute) -> str:
    """Generate form field name for an attribute."""
    return f"attr_{attribute.id}"


def _is_checkbox_attribute(attribute: Attribute) -> bool:
    return attribute.input_type == "checkbox" or bool(attribute.scoring_rule)


def _highest_possible_score_from_options(
    options: list[Option],
    scoring_rule: str,
) -> Decimal:
    if scoring_rule == "retail_liquidity_combo":
        return sum(
            (
                Decimal(str(option.allocated_score or 0))
                for option in options
                if option.display_order in {1, 3}
            ),
            Decimal("0"),
        )
    if scoring_rule == "retail_inventory_combo":
        return sum(
            (
                Decimal(str(option.allocated_score or 0))
                for option in options
                if option.display_order in {1, 2, 3}
            ),
            Decimal("0"),
        )
    return max(
        (Decimal(str(option.allocated_score or 0)) for option in options),
        default=Decimal("0"),
    )


def _ordered_attribute_options(attribute: Attribute) -> list[Option]:
    prefetched_options = getattr(attribute, "_prefetched_objects_cache", {}).get("options")
    if prefetched_options is not None:
        return sorted(
            prefetched_options,
            key=lambda option: (option.display_order, option.id),
        )
    return list(attribute.options.all().order_by("display_order", "id"))


def _parse_checkbox_value(raw_value: Any) -> list[str]:
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


def _serialize_checkbox_value(selected_option_ids: list[int]) -> str:
    return json.dumps(selected_option_ids)


def _get_attribute_highest_possible_score(attribute: Attribute) -> Decimal:
    return attribute.get_highest_possible_score()


def _round_displayed_weighted_score(value: Decimal) -> Decimal:
    """Match the two-decimal weighted value that the score form totals."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _validate_checkbox_rule(attribute: Attribute, selected_options: list[Option]) -> Optional[str]:
    selected_orders = {option.display_order for option in selected_options}

    if attribute.scoring_rule == "retail_liquidity_combo":
        primary_orders = {1, 2}
        additive_order = 3
        standalone_order = 4

        if selected_orders == {standalone_order}:
            return None
        if len(selected_orders) == 2 and additive_order in selected_orders and len(selected_orders & primary_orders) == 1:
            return None
        return "Select d(i) or d(ii) together with d(iii), or score d(iv) on its own."

    if attribute.scoring_rule == "retail_inventory_combo":
        positive_orders = {1, 2, 3}
        standalone_orders = {4, 5}

        if selected_orders and selected_orders.issubset(positive_orders):
            return None
        if len(selected_orders) == 1 and selected_orders.issubset(standalone_orders):
            return None
        return "Select any combination of e(i) to e(iii), or score e(iv) or e(v) on its own."

    return None


def _compute_checkbox_attribute_metrics(
    attribute: Attribute,
    selected_options: list[Option],
) -> dict[str, Any]:
    raw_score = sum(
        (Decimal(str(option.allocated_score or 0)) for option in selected_options),
        Decimal("0"),
    )
    highest_score = _get_attribute_highest_possible_score(attribute)
    weight_percent = Decimal(str(attribute.weight_percent or 0))

    if highest_score > 0 and raw_score != Decimal("0"):
        base_weighted_score = (raw_score / highest_score) * weight_percent
    else:
        base_weighted_score = Decimal("0")

    # Preserve the formula sign. Retail penalty options are intentionally
    # negative and must reduce the total exactly as they do on the form.
    weighted_score = _round_displayed_weighted_score(base_weighted_score)
    proof_value = weight_percent - abs(weighted_score)
    return {
        "raw_score": raw_score,
        "highest_score": highest_score,
        "weighted_score": weighted_score,
        "proof": f"{proof_value:.2f}" if selected_options else "",
    }


def _resolve_attribute_submission(
    attribute: Attribute,
    submitted_value: Any,
) -> dict[str, Any]:
    ordered_options = _ordered_attribute_options(attribute)
    option_map = {str(option.id): option for option in ordered_options}

    if _is_checkbox_attribute(attribute):
        selected_ids = []
        selected_options: list[Option] = []
        for raw_id in _parse_checkbox_value(submitted_value):
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
        error = _validate_checkbox_rule(attribute, selected_options) if selected_options else None
        metrics = _compute_checkbox_attribute_metrics(attribute, selected_options)
        return {
            "selected_option": selected_options[0] if selected_options else None,
            "selected_options": selected_options,
            "selected_option_ids": selected_ids,
            "stored_raw_value": _serialize_checkbox_value(selected_ids) if selected_ids else "",
            "allocated_score": metrics["raw_score"],
            "weighted_score": metrics["weighted_score"],
            "proof": metrics["proof"],
            "highest_score": metrics["highest_score"],
            "has_selection": bool(selected_options),
            "error": error,
        }

    raw_value = submitted_value
    selected_option = None
    allocated_score = Decimal("0")

    if attribute.data_type == "choice":
        if raw_value:
            try:
                selected_option = option_map.get(str(int(raw_value)))
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

    highest_score = _get_attribute_highest_possible_score(attribute)
    if highest_score > 0 and allocated_score != Decimal("0"):
        weighted_score = _round_displayed_weighted_score(
            (allocated_score / highest_score)
            * Decimal(str(attribute.weight_percent or 0))
        )
    else:
        weighted_score = Decimal("0")

    return {
        "selected_option": selected_option,
        "selected_options": [selected_option] if selected_option is not None else [],
        "selected_option_ids": [selected_option.id] if selected_option is not None else [],
        "stored_raw_value": stored_raw_value,
        "allocated_score": allocated_score,
        "weighted_score": weighted_score,
        "proof": "",
        "highest_score": highest_score,
        "has_selection": (selected_option is not None) or (attribute.data_type != "choice" and allocated_score > 0),
        "error": None,
    }


def _collect_attribute_values(
    request: HttpRequest,
    attributes_by_driver: Dict[int, List[Attribute]],
) -> Dict[int, Any]:
    attribute_values: Dict[int, Any] = {}
    for attrs in attributes_by_driver.values():
        for attribute in attrs:
            field_name = _field_name_for_attribute(attribute)
            if _is_checkbox_attribute(attribute):
                values = request.POST.getlist(field_name)
                attribute_values[attribute.id] = values if values else None
            else:
                value = request.POST.get(field_name)
                attribute_values[attribute.id] = value if value is not None else None
    return attribute_values


def _build_checkbox_form_values_from_responses(
    responses: list[Any],
) -> tuple[dict[int, Any], dict[int, Any]]:
    existing_responses: dict[int, Any] = {}
    response_map: dict[int, Any] = {}

    for response in responses:
        response_map[response.attribute_id] = response
        attribute = response.attribute
        if _is_checkbox_attribute(attribute):
            parsed_value = _parse_checkbox_value(response.raw_value)
            existing_responses[response.attribute_id] = parsed_value if parsed_value else None
        elif response.option_id:
            existing_responses[response.attribute_id] = str(response.option_id)
        elif response.raw_value == "" and response.option_id is None:
            existing_responses[response.attribute_id] = ""

    return existing_responses, response_map


def _get_basel_template_category(template: BaselScoreSheetTemplate) -> str | None:
    """
    Infer the business category of a Basel template from its code/name/description.
    This lets archived templates switch to active replacements in the same category.
    """
    code = (template.code or "").upper()
    text = " ".join(
        part for part in [template.code, template.name, template.description] if part
    ).lower()

    if code.startswith("AF") or "farmer" in text or "farming" in text:
        return "farmers"
    if code.startswith("AC") or "corporate" in text:
        return "corporate"
    if code.startswith("AR") or "retail" in text:
        return "retail"
    if code.startswith("AI") or "individual" in text or "consumer" in text:
        return "individual_consumer"
    return None


def _get_active_template_in_same_category(
    template: BaselScoreSheetTemplate,
) -> BaselScoreSheetTemplate | None:
    """Find the most recently updated active Basel template in the same category."""
    category = _get_basel_template_category(template)
    if not category:
        return None

    candidates = (
        BaselScoreSheetTemplate.objects.filter(is_active=True, status="approved")
        .exclude(id=template.id)
        .order_by("-updated_at", "-id")
    )
    for candidate in candidates:
        if _get_basel_template_category(candidate) == category:
            return candidate
    return None


def _get_basel_template_label(evaluation: CreditEvaluation) -> tuple[str, str]:
    if evaluation.template_id and evaluation.template is not None:
        return evaluation.template.code, evaluation.template.name
    return "EXTERNAL_IMPORT", evaluation.template_section_name or "Template not assigned"


def _can_manage_basel_template(request: HttpRequest, evaluation: CreditEvaluation) -> bool:
    if evaluation.status == "submitted":
        return False
    return evaluation.can_be_edited_by(request.user)


def _reset_basel_evaluation_for_template(
    evaluation: CreditEvaluation,
    template: BaselScoreSheetTemplate,
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
        import_version = _create_evaluation_version(
            evaluation,
            version_number=1,
            user=performed_by,
            change_description="Imported Basel II score preserved before template assignment.",
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

    EvaluationWorkflowHistory.objects.create(
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
    """Normalize free text so archived-template response mapping is resilient."""
    return " ".join((value or "").strip().lower().split())


def _get_attribute_match_keys(attribute: Attribute) -> List[Tuple[str, ...]]:
    """
    Build progressively looser match keys for template attributes.
    This helps preserve responses when a questionnaire moves to a replacement template
    with the same logical structure but different database IDs.
    """
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


def _find_matching_option(new_attribute: Attribute, previous_option: Option | None) -> Option | None:
    """Map an old selected option to the closest equivalent option on a replacement attribute."""
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


def _map_responses_to_replacement_template(
    attribute_responses_list: List[AttributeResponse],
    replacement_template: BaselScoreSheetTemplate,
) -> Tuple[Dict[int, str], Dict[int, object]]:
    """
    Re-map existing responses onto a replacement template with the same logical category.

    Returns:
        attribute_values: new_attribute.id -> selected option id / empty string
        mapped_attribute_responses: new_attribute.id -> response-like object for document display
    """
    replacement_attributes = list(
        Attribute.objects.filter(risk_driver__section__template=replacement_template)
        .select_related("risk_driver__section")
        .prefetch_related("options")
        .order_by("display_order", "id")
    )

    attribute_lookup: Dict[Tuple[str, ...], Attribute] = {}
    for attribute in replacement_attributes:
        for key in _get_attribute_match_keys(attribute):
            attribute_lookup.setdefault(key, attribute)

    mapped_values: Dict[int, str] = {}
    mapped_responses: Dict[int, object] = {}

    for response in attribute_responses_list:
        new_attribute = None
        for key in _get_attribute_match_keys(response.attribute):
            new_attribute = attribute_lookup.get(key)
            if new_attribute is not None:
                break

        if new_attribute is None:
            continue

        if response.option_id:
            matched_option = _find_matching_option(new_attribute, response.option)
            if matched_option is None:
                continue

            mapped_values[new_attribute.id] = str(matched_option.id)
            mapped_responses[new_attribute.id] = SimpleNamespace(
                option=SimpleNamespace(id=matched_option.id),
                documents=response.documents.all(),
            )
        elif response.raw_value == "" and response.option_id is None:
            mapped_values[new_attribute.id] = ""

    return mapped_values, mapped_responses


def _create_evaluation_version(
    evaluation: CreditEvaluation,
    version_number: int,
    user=None,
    change_description: str = "",
    total_weighted_percent=None,
    final_grade=None,
    total_raw_score=None,
    attribute_version_payload: Optional[list[dict[str, Any]]] = None,
    driver_version_payload: Optional[list[dict[str, Any]]] = None,
    section_version_payload: Optional[list[dict[str, Any]]] = None,
) -> EvaluationVersion:
    """
    Create a complete snapshot of an evaluation at a specific point in time.
    Saves all attribute responses, driver scores, section scores, and summary data.
    
    Args:
        evaluation: The CreditEvaluation to snapshot
        version_number: Version number (1 for initial, increments on edits)
        user: User who created this version
        change_description: Description of what changed in this version
        total_weighted_percent: Optional - use this score instead of evaluation.total_weighted_percent
        final_grade: Optional - use this grade instead of evaluation.final_grade
        total_raw_score: Optional - use this raw score instead of evaluation.total_raw_score
    
    Returns:
        The created EvaluationVersion instance
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
            version, created = EvaluationVersion.objects.get_or_create(
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
            version = EvaluationVersion.objects.get(
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
            EvaluationVersionAttributeResponse(
                version=version,
                attribute_id=row["attribute_id"],
                option_id=row["option_id"],
                raw_value=row["raw_value"],
                allocated_score=row["allocated_score"],
            )
            for row in attribute_version_payload
        ]
        if attribute_version_rows:
            EvaluationVersionAttributeResponse.objects.bulk_create(
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
            EvaluationVersionDriverScore(
                version=version,
                risk_driver_id=row["risk_driver_id"],
                raw_score=row["raw_score"],
                weighted_percent=row["weighted_percent"],
                proof=row["proof"],
            )
            for row in driver_version_payload
        ]
        if driver_version_rows:
            EvaluationVersionDriverScore.objects.bulk_create(
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
            EvaluationVersionSectionScore(
                version=version,
                section_id=row["section_id"],
                raw_score=row["raw_score"],
                weighted_percent=row["weighted_percent"],
            )
            for row in section_version_payload
        ]
        if section_version_rows:
            EvaluationVersionSectionScore.objects.bulk_create(
                section_version_rows,
                batch_size=BULK_WRITE_BATCH_SIZE,
            )

    return version


def _bulk_create_attribute_responses(
    *,
    evaluation: CreditEvaluation,
    attribute_specs: list[tuple[Attribute, dict[str, Any]]],
    uploaded_files,
    uploaded_by,
) -> None:
    if not attribute_specs:
        return

    response_rows = [
        AttributeResponse(
            evaluation=evaluation,
            attribute=attribute,
            option=resolved_submission["selected_option"],
            raw_value=resolved_submission["stored_raw_value"],
            allocated_score=resolved_submission["allocated_score"],
        )
        for attribute, resolved_submission in attribute_specs
    ]
    AttributeResponse.objects.bulk_create(
        response_rows,
        batch_size=BULK_WRITE_BATCH_SIZE,
    )

    document_specs = []
    for attribute, resolved_submission in attribute_specs:
        if not attribute.requires_document or _is_checkbox_attribute(attribute):
            continue
        selected_option = resolved_submission["selected_option"]
        if not selected_option:
            continue
        file_key = f"doc_attr_{attribute.id}_option_{selected_option.id}"
        uploaded_file_list = uploaded_files.getlist(file_key)
        if uploaded_file_list:
            document_specs.append((attribute, uploaded_file_list, selected_option.id))

    if not document_specs:
        return

    response_map = {
        response.attribute_id: response
        for response in AttributeResponse.objects.filter(
            evaluation=evaluation,
            attribute_id__in=[attribute.id for attribute, _, _ in document_specs],
        )
    }

    for attribute, uploaded_file_list, selected_option_id in document_specs:
        attribute_response = response_map.get(attribute.id)
        if attribute_response is None:
            continue
        for uploaded_file in uploaded_file_list:
            AttributeResponseDocument.objects.create(
                attribute_response=attribute_response,
                file=uploaded_file,
                file_name=uploaded_file.name,
                uploaded_by=uploaded_by,
            )


def _normalize_list_page_size(raw_value: Any, default: int = 20) -> int:
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
    page_number = request.GET.get("p") or "1"
    page_obj = paginator.get_page(page_number)
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


def _format_existing_basel_score_timestamp(evaluation: CreditEvaluation) -> str:
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



def _build_existing_basel_score_payload(
    evaluation: CreditEvaluation,
    *,
    action_mode: str,
    preview_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "exists": True,
        "evaluation_id": evaluation.id,
        "customer_name": evaluation.customer_name,
        "branch_name": evaluation.branch_name,
        "timestamp_label": _format_existing_basel_score_timestamp(evaluation),
        "action_mode": action_mode,
        "edit_url": f"/scorecard/basel-scores/edit/{evaluation.id}/",
        "view_url": f"/scorecard/basel-scores/view/{evaluation.id}/?preview=1",
        "preview_records": preview_records or [],
    }



def _get_locked_existing_basel_score_payload(
    customer_id: str,
    *,
    current_branch_name: str | None = None,
) -> dict[str, Any] | None:
    if not is_cross_branch_duplicate_scoring_prevented():
        return None

    evaluations = CreditEvaluation.objects.filter(customer_id=customer_id).exclude(status="cancelled")
    if current_branch_name:
        evaluations = evaluations.exclude(branch_name=current_branch_name)

    matching_evaluations = list(evaluations.order_by("-approved_at", "-updated_at", "-created_at"))
    if not matching_evaluations:
        return None

    preview_records = [
        {
            "evaluation_id": item.id,
            "branch_name": item.branch_name,
            "timestamp_label": _format_existing_basel_score_timestamp(item),
            "view_url": f"/scorecard/basel-scores/view/{item.id}/?preview=1",
        }
        for item in matching_evaluations
    ]
    return _build_existing_basel_score_payload(
        matching_evaluations[0],
        action_mode="view",
        preview_records=preview_records,
    )



def _get_branch_filtered_basel_evaluations(
    request: HttpRequest,
    *,
    statuses: list[str] | tuple[str, ...] | None = None,
    exclude_statuses: list[str] | tuple[str, ...] | None = None,
):
    evaluations = CreditEvaluation.objects.select_related("template")

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
        "Choose one specific branch before opening a Basel II score form. All Branches mode is for combined views only.",
    )
    return redirect("scorecard:basel_scores_submitted_list")


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
    if results:
        logger.info(f"First customer: {results[0]['customer_ref_code']} - {results[0]['customer_name']}")
    
    logger.info(f"Returning {len(results)} customers")
    return JsonResponse({"customers": results})


@login_required
def basel_autofill_api(request: HttpRequest, template_id: int) -> JsonResponse:
    template = get_object_or_404(BaselScoreSheetTemplate, pk=template_id)
    _, _, attributes_by_driver = _build_configuration(template, use_approved_version=True)
    payload = _build_basel_autofill_payload(
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
def questionnaire_template_select_view(request: HttpRequest) -> HttpResponse:
    """
    View to select which template to use for the questionnaire.
    Shows list of all active templates.
    """
    all_branches_redirect = _redirect_if_all_branches_selected_for_score_entry(request)
    if all_branches_redirect is not None:
        return all_branches_redirect

    templates = BaselScoreSheetTemplate.objects.filter(is_active=True, status="approved").order_by("code", "name")

    context = {
        "templates": templates,
    }

    return render(
        request,
        "credit_scoreshifts/basel_scores_form/basel_scores_template_select.html",
        context,
    )


@login_required
def questionnaire_form_view(request: HttpRequest, template_id: int) -> HttpResponse:
    """
    Main questionnaire form view.
    - Renders the questionnaire based on the selected template
    - Accepts branch & customer details and attribute responses
    - Calculates raw and weighted scores plus final grade
    - Persists the full audit trail
    """
    all_branches_redirect = _redirect_if_all_branches_selected_for_score_entry(request)
    if all_branches_redirect is not None:
        return all_branches_redirect

    template = get_object_or_404(BaselScoreSheetTemplate, id=template_id, is_active=True, status="approved")

    # CRITICAL: Use approved template version structure for questionnaires
    # This ensures that pending template changes don't affect new questionnaires until approved
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
            _build_basel_autofill_payload(
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
                current_side="basel",
                current_customer_id=customer_id,
                current_branch_name=branch_name,
            )
            if customer_id and branch_name
            else None
        )
        if counterpart_block_response is not None:
            return counterpart_block_response

        locked_existing_payload = (
            _get_locked_existing_basel_score_payload(customer_id, current_branch_name=branch_name)
            if customer_id
            else None
        )
        if locked_existing_payload is not None:
            locked_branch_name = locked_existing_payload.get("branch_name") or "Unknown branch"
            locked_timestamp = locked_existing_payload.get("timestamp_label") or "an earlier time"
            messages.error(
                request,
                f"Customer '{customer_name}' ({customer_id}) was already scored in branch '{locked_branch_name}' on {locked_timestamp}. Workflow Rules are blocking any new Basel II scoring for this customer. Opening the saved Basel II score in read-only mode.",
            )
            return redirect(
                "scorecard:basel_scores_view_detail",
                evaluation_id=locked_existing_payload["evaluation_id"],
            )

        # Always require customer and branch info (even for drafts)
        missing_meta = not (customer_ref_code and branch and customer)

        missing_attributes = []
        invalid_attribute_rules: Dict[int, str] = {}
        attribute_values = _collect_attribute_values(request, attributes_by_driver)

        # Collect attribute values (always collect, but only validate if not a draft)
        for attrs in attributes_by_driver.values():
            for attribute in attrs:
                value = attribute_values.get(attribute.id)
                if not is_draft and attribute.is_required and value is None:
                    missing_attributes.append(attribute)
                if not is_draft and value is not None:
                    resolved_submission = _resolve_attribute_submission(attribute, value)
                    if resolved_submission["has_selection"] and resolved_submission["error"]:
                        invalid_attribute_rules[attribute.id] = resolved_submission["error"]

        # For drafts: only check missing_meta (customer/branch info)
        # For submissions: check both missing_meta and missing_attributes
        if is_draft:
            # Drafts only need customer/branch info
            if missing_meta:
                # Get all grade bands for the grading system display
                grade_bands = template.grade_bands.all().order_by("display_order")

                context = {
                    "template": template,
                    "sections": sections,
                    "drivers_by_section": drivers_by_section,
                    "attributes_by_driver": attributes_by_driver,
                    "grade_bands": grade_bands,
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
                    "credit_scoreshifts/basel_scores_form/basel_scores_form.html",
                    context,
                )
        elif missing_meta or missing_attributes or invalid_attribute_rules:
            # Get all grade bands for the grading system display
            grade_bands = template.grade_bands.all().order_by("display_order")

            context = {
                "template": template,
                "sections": sections,
                "drivers_by_section": drivers_by_section,
                "attributes_by_driver": attributes_by_driver,
                "grade_bands": grade_bands,
                "errors": {
                    "missing_meta": missing_meta,
                    "missing_attributes": missing_attributes,
                    "invalid_attribute_rules": invalid_attribute_rules,
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
                "credit_scoreshifts/basel_scores_form/basel_scores_form.html",
                context,
            )

        # is_draft is already checked above
        action = 'draft' if is_draft else 'submit'
        
        # Check if customer already has a questionnaire (completed or draft)
        existing_evaluation = CreditEvaluation.objects.filter(
            customer_id=customer_id,
            branch_name=branch_name
        ).exclude(status='completed').first()  # Allow multiple completed, but only one draft/in_progress
        
        # If submitting (not draft), check for completed questionnaires
        if not is_draft:
            completed_evaluation = CreditEvaluation.objects.filter(
                customer_id=customer_id,
                branch_name=branch_name,
                status='completed'
            ).first()
            
            if completed_evaluation:
                messages.warning(
                    request,
                    f"A completed Basel II score for customer '{customer_name}' ({customer_id}) at branch '{branch_name}' already exists. "
                    f"Please use the Edit functionality to make changes."
                )
                return redirect("scorecard:basel_scores_edit", evaluation_id=completed_evaluation.id)
        
        # If draft exists, update it; otherwise create new
        # Process and save the evaluation
        with transaction.atomic():
            if existing_evaluation:
                evaluation = CreditEvaluation.objects.select_for_update().get(
                    pk=existing_evaluation.pk
                )
                if (
                    not is_draft
                    and evaluation.status in {"submitted", "approved"}
                    and evaluation.versions.filter(version_number=1).exists()
                ):
                    messages.info(
                        request,
                        "This Basel II score was already submitted. Showing the saved record instead of submitting it again.",
                    )
                    return redirect(
                        "scorecard:basel_scores_view_detail",
                        evaluation_id=evaluation.id,
                    )

                evaluation.template = template
                evaluation.branch_name = branch_name
                evaluation.customer_name = customer_name
                evaluation.customer_id = customer_id
                if not evaluation.maker:
                    evaluation.maker = request.user
            else:
                evaluation = CreditEvaluation.objects.create(
                    template=template,
                    branch_name=branch_name,
                    customer_name=customer_name,
                    customer_id=customer_id,
                    status='draft' if is_draft else 'in_progress',
                    maker=request.user,
                )
                evaluation = CreditEvaluation.objects.select_for_update().get(
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
                draft_attribute_specs: list[tuple[Attribute, dict[str, Any]]] = []
                for attrs in attributes_by_driver.values():
                    for attribute in attrs:
                        raw_value = attribute_values.get(attribute.id, None)
                        if raw_value is None:
                            continue
                        resolved_submission = _resolve_attribute_submission(attribute, raw_value)
                        if not resolved_submission["has_selection"] and resolved_submission["stored_raw_value"] != "":
                            continue
                        if not resolved_submission["has_selection"] and resolved_submission["stored_raw_value"] == "":
                            continue
                        draft_attribute_specs.append((attribute, resolved_submission))

                _bulk_create_attribute_responses(
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
                log_basel_score_audit(
                    request.user,
                    "save_draft",
                    evaluation,
                    "Draft questionnaire created from the Basel score form.",
                )
                
                messages.success(
                    request,
                    f"Draft saved successfully! You can continue working on it later.",
                )
                return redirect("scorecard:basel_scores_draft_list")
            
            # For completed submissions: Calculate all scores
            # Calculate per-attribute weighted scores and aggregate at driver/section level
            # This matches the frontend calculation: WEIGHTED_SCORE = (ACTUAL_SCORE / Highest_Possible_Score) * WEIGHT per attribute
            # IMPORTANT: Frontend only includes attributes with selected options in calculations
            # We must match this behavior to avoid discrepancies
            driver_raw_scores: Dict[int, Decimal] = {}
            driver_weighted_scores: Dict[int, Decimal] = {}
            submitted_attribute_specs: list[tuple[Attribute, dict[str, Any]]] = []

            for driver_id, attrs in attributes_by_driver.items():
                driver_total_raw = Decimal("0")
                driver_total_weighted = Decimal("0")
                
                for attribute in attrs:
                    raw_value = attribute_values.get(attribute.id, "")
                    resolved_submission = _resolve_attribute_submission(attribute, raw_value)
                    submitted_attribute_specs.append((attribute, resolved_submission))

                    # CRITICAL FIX: Only include in totals if there's an actual selection
                    # Frontend logic: only processes attributes where selectedRadio is checked
                    # If no option is selected (selected_option is None), frontend skips the attribute entirely
                    # We must match this behavior to avoid calculation discrepancies
                    has_selection = resolved_submission["has_selection"]
                    
                    if has_selection:
                        driver_total_raw += resolved_submission["allocated_score"]
                        driver_total_weighted += resolved_submission["weighted_score"]
                    # If no selection, attribute is skipped (matches frontend behavior)

                driver_raw_scores[driver_id] = driver_total_raw
                driver_weighted_scores[driver_id] = driver_total_weighted

            _bulk_create_attribute_responses(
                evaluation=evaluation,
                attribute_specs=submitted_attribute_specs,
                uploaded_files=request.FILES,
                uploaded_by=request.user,
            )
            attribute_version_payload = [
                {
                    "attribute_id": attribute.id,
                    "option_id": (
                        resolved_submission["selected_option"].id
                        if resolved_submission["selected_option"] is not None
                        else None
                    ),
                    "raw_value": resolved_submission["stored_raw_value"],
                    "allocated_score": resolved_submission["allocated_score"],
                }
                for attribute, resolved_submission in submitted_attribute_specs
            ]
            attribute_version_payload = [
                {
                    "attribute_id": attribute.id,
                    "option_id": (
                        resolved_submission["selected_option"].id
                        if resolved_submission["selected_option"] is not None
                        else None
                    ),
                    "raw_value": resolved_submission["stored_raw_value"],
                    "allocated_score": resolved_submission["allocated_score"],
                }
                for attribute, resolved_submission in submitted_attribute_specs
            ]

            total_raw_score = Decimal("0")
            total_weighted_percent = Decimal("0")

            # Persist driver and section scores
            # Formulas (same for all sections):
            # ACTUAL_SCORE = ALLOCATED_SCORE (sum of allocated scores for the driver)
            # WEIGHTED_SCORE = Sum of (ACTUAL_SCORE / Highest Possible Score * WEIGHT) for each attribute
            # PROOF = IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE <> ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE
            section_weighted_totals = {}  # Track section totals to match frontend calculation
            driver_score_rows: list[RiskDriverScore] = []
            section_score_rows: list[SectionScore] = []
            
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
                        RiskDriverScore(
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
                    SectionScore(
                        evaluation=evaluation,
                        section=section,
                        raw_score=section_raw_total,
                        weighted_percent=section_total,
                    )
                )

            if driver_score_rows:
                RiskDriverScore.objects.bulk_create(
                    driver_score_rows,
                    batch_size=BULK_WRITE_BATCH_SIZE,
                )
            if section_score_rows:
                SectionScore.objects.bulk_create(
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

            # Determine final grade and status for completed submission
            # (Drafts are handled earlier and return early, so we only reach here for completed submissions)
            grade_bands = template.grade_bands.all().order_by("-min_percent")
            final_grade = ""
            for band in grade_bands:
                if total_weighted_percent >= band.min_percent:
                    final_grade = band.grade_code
                    break
            
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
            
            auto_approve = _can_auto_approve_basel_submission(request.user)

            evaluation.status = 'approved' if auto_approve else 'submitted'
            evaluation.submitted_by = request.user
            evaluation.submitted_at = timezone.now()
            evaluation.version = 1  # First submission
            evaluation.save()

            # Create initial version snapshot (Version 1) and keep all prior versions as history.
            # Pass the calculated scores explicitly to ensure version has the correct scores
            _create_evaluation_version(
                evaluation=evaluation,
                version_number=1,
                user=request.user,
                change_description="Initial Basel II score submission",
                total_weighted_percent=total_weighted_percent,
                final_grade=final_grade,
                total_raw_score=total_raw_score,
                attribute_version_payload=attribute_version_payload,
                driver_version_payload=driver_version_payload,
                section_version_payload=section_version_payload,
            )

            if auto_approve:
                _finalize_basel_auto_approval(
                    evaluation,
                    request.user,
                    old_status="in_progress",
                    comments="Initial Basel II score auto-approved on submission",
                )
                evaluation.refresh_from_db()
                log_basel_score_audit(
                    request.user,
                    "approve",
                    evaluation,
                    f"Initial Basel II score auto-approved on submission with score {evaluation.total_weighted_percent:.2f}% and grade {evaluation.final_grade or '-'}.",
                )
                messages.success(
                    request,
                    f"Basel II score approved automatically on submit. Total Score: {evaluation.total_weighted_percent}%, Grade: {evaluation.final_grade}.",
                )
                notify_basel_approved(evaluation, request.user)
            else:
                EvaluationWorkflowHistory.objects.create(
                    evaluation=evaluation,
                    action='submitted',
                    from_status='in_progress',
                    to_status='submitted',
                    performed_by=request.user,
                    comments='Initial Basel II score submission'
                )
                log_basel_score_audit(
                    request.user,
                    "submit",
                    evaluation,
                    f"Initial Basel II score submitted for review with score {total_weighted_percent:.2f}% and grade {final_grade or '-'}.",
                )
                messages.success(
                    request,
                    f"Basel II score submitted for review! Total Score: {total_weighted_percent}%, Grade: {final_grade}. "
                    f"Waiting for checker approval.",
                )
                notify_basel_submitted(evaluation)
            return redirect("scorecard:maker_submitted_list")

    # GET request - show form
    grade_bands = template.grade_bands.all().order_by("display_order")
    
    # Check if there's a customer_ref_code in the URL or form data to check for existing draft/in-progress evaluation
    existing_evaluation = None
    customer_ref_code_from_get = request.GET.get('customer_ref_code', '')
    if customer_ref_code_from_get:
        current_branch = resolve_branch_context(request)
        if current_branch is not None:
            # Check for draft or in_progress evaluations (not completed ones)
            existing_evaluation = CreditEvaluation.objects.filter(
                customer_id=customer_ref_code_from_get,
                branch_name=current_branch.branch_name
            ).exclude(status='completed').first()

    autofill_payload = _empty_autofill_payload()
    if customer_ref_code_from_get and existing_evaluation is None:
        autofill_payload = _build_basel_autofill_payload(
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
        "grade_bands": grade_bands,
        "existing_evaluation": existing_evaluation,
        "errors": {},
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
        "credit_scoreshifts/basel_scores_form/basel_scores_form.html",
        context,
    )


@login_required
def questionnaire_submitted_list_view(request: HttpRequest) -> HttpResponse:
    """
    View to list all submitted questionnaires/evaluations.
    Filters by current branch for both regular users and admins.
    """
    search_query = (request.GET.get("q") or "").strip()
    status_filter = (request.GET.get("status") or "").strip().lower()
    exposure_filter = _get_exposure_filter(request)
    pending_versions = EvaluationVersion.objects.filter(
        evaluation_id=OuterRef("pk"),
        is_approved=False,
    ).order_by("-version_number")

    evaluations = (
        _get_branch_filtered_basel_evaluations(
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
        .select_related("override_by")
        .prefetch_related("template__grade_bands")
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
            "previous_weighted_percent",
            "previous_grade",
            "approved_weighted_percent",
            "approved_grade",
            "override_grade",
            "override_comments",
            "override_document",
            "override_document_name",
            "override_by_id",
            "override_by__email",
            "override_by__name",
            "override_by__surname",
            "override_at",
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
        "basel",
        exposure_filter,
    )

    context = {
        "evaluations": page_obj.object_list,
        "page_obj": page_obj,
        "search_query": search_query,
        "page_size": page_size,
        "status_filter": status_filter,
        "exposure_filter": exposure_filter,
        "can_override_basel_grade": _can_override_basel_grade(request.user),
        "current_list_url": request.get_full_path(),
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
        "credit_scoreshifts/basel_scores_form/basel_scores_submitted_list.html",
        context,
    )


@login_required
@require_POST
def basel_score_override_grade_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    if not _can_override_basel_grade(request.user):
        raise PermissionDenied("You do not have permission to override Basel II grades.")

    evaluation = get_object_or_404(
        _get_branch_filtered_basel_evaluations(
            request,
            exclude_statuses=["draft", "in_progress"],
        ).select_related("template", "override_by"),
        pk=evaluation_id,
    )

    override_grade = (request.POST.get("override_grade") or "").strip()
    override_comments = (request.POST.get("override_comments") or "").strip()
    next_url = (request.POST.get("next") or "").strip()
    old_grade = evaluation.override_grade or ""
    old_document_name = evaluation.override_document_name or ""

    if not override_grade:
        evaluation.override_grade = ""
        evaluation.override_comments = ""
        evaluation.override_by = None
        evaluation.override_at = None
        evaluation.override_document = None
        evaluation.override_document_name = ""
        evaluation.save(
            update_fields=[
                "override_grade",
                "override_comments",
                "override_by",
                "override_at",
                "override_document",
                "override_document_name",
                "updated_at",
            ]
        )

        log_basel_score_audit(
            request.user,
            "override_grade_removed",
            evaluation,
            (
                f"Basel override grade removed. Previous override grade: {old_grade or '-'}. "
                f"Comment: {override_comments or '-'}. "
                f"Previous document: {old_document_name or '-'}."
            ),
        )

        messages.success(
            request,
            f"Override grade removed for {evaluation.customer_name} ({evaluation.customer_id}).",
        )
        if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return redirect(next_url)
        return redirect("scorecard:basel_scores_submitted_list")

    if not override_comments:
        messages.error(request, "Please add a comment explaining the Basel override grade.")
        return redirect("scorecard:basel_scores_submitted_list")

    allowed_grade_codes = set()
    if evaluation.template_id and evaluation.template is not None:
        allowed_grade_codes = {
            (band.grade_code or "").strip().upper()
            for band in evaluation.template.grade_bands.all()
            if (band.grade_code or "").strip()
        }
    if allowed_grade_codes and override_grade.upper() not in allowed_grade_codes:
        messages.error(
            request,
            "Choose a valid Basel grade from the template grading scale.",
        )
        return redirect("scorecard:basel_scores_submitted_list")

    uploaded_document = request.FILES.get("override_document")

    evaluation.override_grade = override_grade
    evaluation.override_comments = override_comments
    evaluation.override_by = request.user
    evaluation.override_at = timezone.now()
    update_fields = [
        "override_grade",
        "override_comments",
        "override_by",
        "override_at",
        "updated_at",
    ]
    if uploaded_document:
        evaluation.override_document = uploaded_document
        evaluation.override_document_name = uploaded_document.name
        update_fields.extend(["override_document", "override_document_name"])

    evaluation.save(update_fields=update_fields)

    log_basel_score_audit(
        request.user,
        "override_grade",
        evaluation,
        (
            f"Basel override grade changed from {old_grade or '-'} to {evaluation.override_grade or '-'}. "
            f"Comment: {override_comments}. "
            f"Document: {evaluation.override_document_name or old_document_name or '-'}."
        ),
    )

    messages.success(
        request,
        f"Override grade '{evaluation.override_grade}' saved for {evaluation.customer_name} ({evaluation.customer_id}).",
    )
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect("scorecard:basel_scores_submitted_list")


@login_required
def questionnaire_draft_list_view(request: HttpRequest) -> HttpResponse:
    """
    View to list all draft/in-progress questionnaires.
    Filters by current branch for both regular users and admins.
    """
    search_query = (request.GET.get("q") or "").strip()
    evaluations = _get_branch_filtered_basel_evaluations(
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
        "credit_scoreshifts/basel_scores_form/basel_scores_draft_list.html",
        context,
    )


@login_required
def basel_scores_template_assignment_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    evaluation = get_object_or_404(
        CreditEvaluation.objects.select_related("template", "maker"),
        id=evaluation_id,
    )

    if not _can_manage_basel_template(request, evaluation):
        messages.error(request, "You don't have permission to manage the template for this Basel II score.")
        target = "scorecard:basel_scores_draft_list" if evaluation.status in {"draft", "in_progress", "returned"} else "scorecard:basel_scores_submitted_list"
        return redirect(target)

    if request.method == "POST":
        template_id = request.POST.get("template_id")
        selected_template = BaselScoreSheetTemplate.objects.filter(
            id=template_id,
            is_active=True,
            status="approved",
        ).first()
        if not selected_template:
            messages.error(request, "Choose an active approved Basel template before continuing.")
        elif evaluation.template_id == selected_template.id:
            messages.info(
                request,
                f"This Basel II score is already linked to template '{selected_template.code}'.",
            )
            return redirect("scorecard:basel_scores_edit", evaluation_id=evaluation.id)
        else:
            had_existing_template = bool(evaluation.template_id)
            previous_template_code = _reset_basel_evaluation_for_template(
                evaluation,
                selected_template,
                performed_by=request.user,
            )
            log_basel_score_audit(
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
            return redirect("scorecard:basel_scores_edit", evaluation_id=evaluation.id)

    templates = BaselScoreSheetTemplate.objects.filter(is_active=True, status="approved").order_by("name", "code")
    current_template = evaluation.template

    context = {
        "evaluation": evaluation,
        "templates": templates,
        "current_template": current_template,
        "is_change": bool(current_template),
        "back_url_name": "scorecard:basel_scores_draft_list" if evaluation.status in {"draft", "in_progress", "returned"} else "scorecard:basel_scores_submitted_list",
    }
    return render(
        request,
        "credit_scoreshifts/basel_scores_form/basel_scores_template_assignment.html",
        context,
    )


def _get_filtered_evaluations(request):
    """Helper function to get submitted Basel evaluations using the same filters as the list page."""
    search_query = (request.GET.get("q") or "").strip()
    status_filter = (request.GET.get("status") or "").strip().lower()
    exposure_filter = _get_exposure_filter(request)
    evaluations = _get_branch_filtered_basel_evaluations(
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


def _get_basel_export_rows(request):
    page = 'all'
    evaluations = _get_filtered_evaluations(request)
    return list(evaluations), page


@login_required
def questionnaire_export_csv(request: HttpRequest) -> HttpResponse:
    """Export questionnaires to CSV format."""
    import csv

    evaluations, page = _get_basel_export_rows(request)
    export_date = timezone.localdate().isoformat()

    # Create CSV response
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = (
        f'attachment; filename="basel_ii_scores_{page}_{export_date}.csv"'
    )
    
    writer = csv.writer(response)
    
    # Write header
    writer.writerow([
        'Template Code',
        'Template Name',
        'Branch',
        'Customer Name',
        'Customer ID',
        'Weighted Score',
        'Grade',
        'Override Grade',
        'Override By',
        'Override Date',
        'Override Comments',
        'Override Document',
        'Submitted Date',
        'Submitted Time'
    ])
    
    # Write data rows
    for evaluation in evaluations:
        template_code, template_name = _get_basel_template_label(evaluation)
        writer.writerow([
            template_code,
            template_name,
            evaluation.branch_name or '',
            evaluation.customer_name or '',
            evaluation.customer_id or '',
            f"{evaluation.total_weighted_percent or 0:.2f}%",
            evaluation.final_grade or '-',
            evaluation.override_grade or '',
            evaluation.override_by.email if evaluation.override_by_id and evaluation.override_by else '',
            evaluation.override_at.date() if evaluation.override_at else '',
            evaluation.override_comments or '',
            evaluation.override_document_name or '',
            evaluation.submitted_at.date() if evaluation.submitted_at else '',
            evaluation.submitted_at.time() if evaluation.submitted_at else '',
        ])
    
    return response


@login_required
def questionnaire_export_excel(request: HttpRequest) -> HttpResponse:
    """Export questionnaires to Excel format."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        return HttpResponse("Excel export requires openpyxl library. Please install it.", status=500)
    
    evaluations, page = _get_basel_export_rows(request)
    export_date = timezone.localdate().isoformat()
    
    # Create workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Basel II Scores"
    
    # Define headers
    headers = [
        'Template Code',
        'Template Name',
        'Branch',
        'Customer Name',
        'Customer ID',
        'Weighted Score',
        'Grade',
        'Override Grade',
        'Override By',
        'Override Date',
        'Override Comments',
        'Override Document',
        'Submitted Date',
        'Submitted Time'
    ]
    
    # Style header row
    header_fill = PatternFill(start_color="0066cc", end_color="0066cc", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    
    # Write headers
    for col_num, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_num)
        cell.value = header
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center', vertical='center')
    
    # Write data rows
    for row_num, evaluation in enumerate(evaluations, 2):
        template_code, template_name = _get_basel_template_label(evaluation)
        ws.cell(row=row_num, column=1, value=template_code)
        ws.cell(row=row_num, column=2, value=template_name)
        ws.cell(row=row_num, column=3, value=evaluation.branch_name or '')
        ws.cell(row=row_num, column=4, value=evaluation.customer_name or '')
        ws.cell(row=row_num, column=5, value=evaluation.customer_id or '')
        ws.cell(row=row_num, column=6, value=f"{evaluation.total_weighted_percent or 0:.2f}%")
        ws.cell(row=row_num, column=7, value=evaluation.final_grade or '-')
        ws.cell(row=row_num, column=8, value=evaluation.override_grade or '')
        ws.cell(row=row_num, column=9, value=evaluation.override_by.email if evaluation.override_by_id and evaluation.override_by else '')
        ws.cell(row=row_num, column=10, value=evaluation.override_at.date() if evaluation.override_at else '')
        ws.cell(row=row_num, column=11, value=evaluation.override_comments or '')
        ws.cell(row=row_num, column=12, value=evaluation.override_document_name or '')
        ws.cell(row=row_num, column=13, value=evaluation.submitted_at.date() if evaluation.submitted_at else '')
        ws.cell(row=row_num, column=14, value=evaluation.submitted_at.time() if evaluation.submitted_at else '')
    
    # Auto-adjust column widths
    for col in ws.columns:
        max_length = 0
        col_letter = col[0].column_letter
        for cell in col:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        adjusted_width = min(max_length + 2, 50)
        ws.column_dimensions[col_letter].width = adjusted_width
    
    # Create response
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = (
        f'attachment; filename="basel_ii_scores_{page}_{export_date}.xlsx"'
    )
    
    wb.save(response)
    return response


@login_required
def questionnaire_edit_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    View to edit a submitted questionnaire.
    Loads existing evaluation data, allows modifications, and updates the evaluation.
    """
    evaluation = get_object_or_404(
        CreditEvaluation.objects.select_related("template").prefetch_related(
            "attribute_responses__attribute__risk_driver__section",
            "attribute_responses__option",
            "driver_scores__risk_driver",
            "section_scores__section",
        ),
        id=evaluation_id,
    )

    # Lock editing if status is 'submitted' (must wait for checker review or withdraw)
    # Approved questionnaires CAN be edited - they will create a new version and go through approval again
    if evaluation.status == 'submitted':
        messages.error(
            request,
            f"This Basel II score is submitted for review. "
            f"It cannot be edited until it is returned for changes or you withdraw the submission."
        )
        return redirect("scorecard:basel_scores_view_detail", evaluation_id=evaluation.id)
    
    if not evaluation.can_be_edited_by(request.user):
        messages.error(request, "You don't have permission to edit this Basel II score.")
        return redirect("scorecard:basel_scores_view_detail", evaluation_id=evaluation.id)

    if not evaluation.template_id or evaluation.template is None:
        messages.info(
            request,
            "Choose a Basel template first so this imported Basel II score can open in the full scorecard editor.",
        )
        return redirect("scorecard:basel_scores_template_assignment", evaluation_id=evaluation.id)

    original_template = evaluation.template
    template = original_template
    template_switched = False
    template_switch_notice = ""
    if template and not template.is_active:
        messages.warning(
            request,
            f"Template '{template.code}' is inactive. Choose a template from the Basel template table before editing this Basel II score.",
        )
        return redirect("scorecard:basel_scores_template_assignment", evaluation_id=evaluation.id)

    # Use approved template version structure to ensure consistency with approved template
    sections, drivers_by_section, attributes_by_driver = _build_configuration(template, use_approved_version=True)

    if request.method == "POST":
        # Double-check lock status even for POST requests (security)
        # Only lock 'submitted' status - 'approved' can be edited
        if evaluation.status == 'submitted':
            messages.error(
                request,
                f"This questionnaire is submitted for review. "
                f"It cannot be edited until it is returned for changes or you withdraw the submission."
            )
            return redirect("scorecard:basel_scores_view_detail", evaluation_id=evaluation.id)
        
        # Check if this is a draft save or final submission
        is_draft = request.POST.get('save_draft') == 'true'
        
        # Branch, Customer Name, and Customer ID are not editable - use existing values
        branch_name = evaluation.branch_name
        customer_name = evaluation.customer_name
        customer_id = evaluation.customer_id
        autofill_payload = (
            _build_basel_autofill_payload(
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
        invalid_attribute_rules: Dict[int, str] = {}
        attribute_values = _collect_attribute_values(request, attributes_by_driver)

        # Check if this is a draft save or final submission
        is_draft = request.POST.get('save_draft') == 'true'

        # Collect attribute values (no backend validation - HTML5 handles it)
        for attrs in attributes_by_driver.values():
            for attribute in attrs:
                value = attribute_values.get(attribute.id)
                if not is_draft and attribute.is_required and value is None:
                    missing_attributes.append(attribute)
                if not is_draft and value is not None:
                    resolved_submission = _resolve_attribute_submission(attribute, value)
                    if resolved_submission["has_selection"] and resolved_submission["error"]:
                        invalid_attribute_rules[attribute.id] = resolved_submission["error"]

        # For drafts: skip all validation (HTML5 validation is disabled for drafts via JavaScript)
        # HTML5 validation handles attribute requirements for submissions
        # Drafts don't need validation - just save what's there
        # (missing_meta check removed since customer/branch is read-only in edit)
        
        # Only check missing_meta for submissions (HTML5 handles attributes)
        if (missing_meta or invalid_attribute_rules or missing_attributes) and not is_draft:
                # Get all grade bands for the grading system display
                grade_bands = template.grade_bands.all().order_by("display_order")

                # Get existing attribute responses for pre-population
                attribute_responses_list = list(evaluation.attribute_responses.select_related('option').prefetch_related('documents__uploaded_by').all())
                existing_responses, attribute_responses_dict = _build_checkbox_form_values_from_responses(attribute_responses_list)

                context = {
                    "template": template,
                    "sections": sections,
                    "drivers_by_section": drivers_by_section,
                    "attributes_by_driver": attributes_by_driver,
                    "grade_bands": grade_bands,
                    "evaluation": evaluation,
                    "attribute_responses": attribute_responses_dict,
                    "template_switch_notice": template_switch_notice,
                    "errors": {
                        "missing_meta": missing_meta,
                        "missing_attributes": missing_attributes,
                        "invalid_attribute_rules": invalid_attribute_rules,
                    },
                    # Preserve entered values for re-display
                    "form_data": {
                        "branch_name": branch_name,
                        "customer_name": customer_name,
                        "customer_id": customer_id,
                        "attribute_values": attribute_values,
                    },
                }
                return render(
                    request,
                    "credit_scoreshifts/basel_scores_form/basel_scores_edit.html",
                    context,
                )
        elif missing_meta or invalid_attribute_rules or missing_attributes:
            # Get all grade bands for the grading system display
            grade_bands = template.grade_bands.all().order_by("display_order")

            attribute_responses_list = list(evaluation.attribute_responses.select_related('option').prefetch_related('documents__uploaded_by').all())
            existing_responses, attribute_responses_dict = _build_checkbox_form_values_from_responses(attribute_responses_list)

            context = {
                "template": template,
                "sections": sections,
                "drivers_by_section": drivers_by_section,
                "attributes_by_driver": attributes_by_driver,
                "grade_bands": grade_bands,
                "evaluation": evaluation,
                "attribute_responses": attribute_responses_dict,
                "template_switch_notice": template_switch_notice,
                "errors": {
                    "missing_meta": missing_meta,
                    "missing_attributes": missing_attributes,
                    "invalid_attribute_rules": invalid_attribute_rules,
                },
                # Preserve entered values for re-display
                "form_data": {
                    "branch_name": branch_name,
                    "customer_name": customer_name,
                    "customer_id": customer_id,
                    "attribute_values": attribute_values,
                },
            }
            return render(
                request,
                "credit_scoreshifts/basel_scores_form/basel_scores_edit.html",
                context,
            )

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
            
            # When editing an approved questionnaire, we do NOT create a version snapshot before editing.
            # Versions are only created when submitting (see below).
            # This prevents creating unapproved versions that clutter the version list.
            # When a new version is approved, unapproved versions will be deleted (see approve_evaluation_view).
            
            # Only create history records for completed/approved questionnaires (not drafts)
            # Skip history creation if saving as draft
            if evaluation.status in ['completed', 'approved'] and not is_draft:
                # Also create history record with previous values (for backward compatibility)
                if evaluation.total_weighted_percent is not None or evaluation.final_grade:
                    EvaluationHistory.objects.create(
                        evaluation=evaluation,
                        weighted_percent=evaluation.total_weighted_percent,
                        grade=evaluation.final_grade,
                        raw_score=evaluation.total_raw_score,
                    )
                
                # Also preserve in previous fields for backward compatibility
                if evaluation.total_weighted_percent is not None:
                    evaluation.previous_weighted_percent = evaluation.total_weighted_percent
                if evaluation.final_grade:
                    evaluation.previous_grade = evaluation.final_grade
            
            # Note: Branch, Customer Name, and Customer ID are NOT updated - they remain read-only
            # evaluation.branch_name = branch_name  # Not editable
            # evaluation.customer_name = customer_name  # Not editable
            # evaluation.customer_id = customer_id  # Not editable

            # Delete existing responses and scores (will be recreated with new values)
            # Note: Documents are preserved - they're linked to AttributeResponse, so they'll be deleted with responses
            # If you want to preserve documents, you'd need to reassign them to new responses
            evaluation.attribute_responses.all().delete()
            evaluation.driver_scores.all().delete()
            evaluation.section_scores.all().delete()

            # For drafts, only save attribute responses - don't calculate scores
            if is_draft:
                draft_attribute_specs: list[tuple[Attribute, dict[str, Any]]] = []
                for attrs in attributes_by_driver.values():
                    for attribute in attrs:
                        raw_value = attribute_values.get(attribute.id, None)
                        if raw_value is None:
                            continue
                        resolved_submission = _resolve_attribute_submission(attribute, raw_value)
                        if not resolved_submission["has_selection"] and resolved_submission["stored_raw_value"] != "":
                            continue
                        if not resolved_submission["has_selection"] and resolved_submission["stored_raw_value"] == "":
                            continue
                        draft_attribute_specs.append((attribute, resolved_submission))

                _bulk_create_attribute_responses(
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
                log_basel_score_audit(
                    request.user,
                    "save_draft",
                    evaluation,
                    "Draft questionnaire updated from the Basel edit screen.",
                )
                
                messages.success(
                    request,
                    f"Draft saved successfully! You can continue working on it later.",
                )
                return redirect("scorecard:basel_scores_draft_list")

            # For completed submissions: Calculate all scores
            # Calculate all scores
            # Calculate scores
            # Calculate per-attribute weighted scores and aggregate at driver/section level
            # This matches the frontend calculation: WEIGHTED_SCORE = (ACTUAL_SCORE / Highest_Possible_Score) * WEIGHT per attribute
            # IMPORTANT: Frontend only includes attributes with selected options in calculations
            # We must match this behavior to avoid discrepancies
            driver_raw_scores: Dict[int, Decimal] = {}
            driver_weighted_scores: Dict[int, Decimal] = {}
            submitted_attribute_specs: list[tuple[Attribute, dict[str, Any]]] = []

            for driver_id, attrs in attributes_by_driver.items():
                driver_total_raw = Decimal("0")
                driver_total_weighted = Decimal("0")
                
                for attribute in attrs:
                    raw_value = attribute_values.get(attribute.id, "")
                    resolved_submission = _resolve_attribute_submission(attribute, raw_value)
                    submitted_attribute_specs.append((attribute, resolved_submission))

                    # CRITICAL FIX: Only include in totals if there's an actual selection
                    # Frontend logic: only processes attributes where selectedRadio is checked
                    # If no option is selected (selected_option is None), frontend skips the attribute entirely
                    # We must match this behavior to avoid calculation discrepancies
                    has_selection = resolved_submission["has_selection"]
                    
                    if has_selection:
                        driver_total_raw += resolved_submission["allocated_score"]
                        driver_total_weighted += resolved_submission["weighted_score"]
                    # If no selection, attribute is skipped (matches frontend behavior)

                driver_raw_scores[driver_id] = driver_total_raw
                driver_weighted_scores[driver_id] = driver_total_weighted

            _bulk_create_attribute_responses(
                evaluation=evaluation,
                attribute_specs=submitted_attribute_specs,
                uploaded_files=request.FILES,
                uploaded_by=request.user,
            )
            attribute_version_payload = [
                {
                    "attribute_id": attribute.id,
                    "option_id": (
                        resolved_submission["selected_option"].id
                        if resolved_submission["selected_option"] is not None
                        else None
                    ),
                    "raw_value": resolved_submission["stored_raw_value"],
                    "allocated_score": resolved_submission["allocated_score"],
                }
                for attribute, resolved_submission in submitted_attribute_specs
            ]

            total_raw_score = Decimal("0")
            total_weighted_percent = Decimal("0")

            # Persist driver and section scores
            # Formulas (same for all sections):
            # ACTUAL_SCORE = ALLOCATED_SCORE (sum of allocated scores for the driver)
            # WEIGHTED_SCORE = Sum of (ACTUAL_SCORE / Highest Possible Score * WEIGHT) for each attribute
            # PROOF = IF(ACTUAL_SCORE = '', '', IF(ACTUAL_SCORE <> ALLOCATED_SCORE, 'ERROR', '')) + ALLOCATED_SCORE
            section_weighted_totals = {}  # Track section totals to match frontend calculation
            driver_score_rows: list[RiskDriverScore] = []
            section_score_rows: list[SectionScore] = []
            
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
                        RiskDriverScore(
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
                    SectionScore(
                        evaluation=evaluation,
                        section=section,
                        raw_score=section_raw_total,
                        weighted_percent=section_total,
                    )
                )

            if driver_score_rows:
                RiskDriverScore.objects.bulk_create(
                    driver_score_rows,
                    batch_size=BULK_WRITE_BATCH_SIZE,
                )
            if section_score_rows:
                SectionScore.objects.bulk_create(
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
            
            # Debug logging to track calculation discrepancy
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"=== BACKEND CALCULATION DEBUG (EDIT) ===")
            logger.info(f"Section weighted totals: {section_weighted_totals}")
            logger.info(f"Total weighted percent (sum of sections): {float(total_weighted_percent)}")
            logger.info(f"Driver weighted scores: {dict((k, float(v)) for k, v in driver_weighted_scores.items())}")
            
            # Verify by summing all attribute weighted scores directly using the same
            # submission resolver that handles radio/select/checkbox consistently.
            direct_attribute_sum = Decimal("0")
            for driver_id, attrs in attributes_by_driver.items():
                for attribute in attrs:
                    raw_value = attribute_values.get(
                        attribute.id,
                        [] if _is_checkbox_attribute(attribute) else "",
                    )
                    resolved_submission = _resolve_attribute_submission(attribute, raw_value)
                    if resolved_submission["has_selection"]:
                        direct_attribute_sum += resolved_submission["weighted_score"]
            
            logger.info(f"Direct sum of all attribute weighted scores: {float(direct_attribute_sum)}")
            logger.info(f"Difference (section sum vs direct sum): {float(abs(total_weighted_percent - direct_attribute_sum))}")
            logger.info(f"=== END BACKEND CALCULATION DEBUG ===")

            # Determine final grade and status for completed submission
            # (Drafts are handled earlier and return early, so we only reach here for completed submissions)
            grade_bands = template.grade_bands.all().order_by("-min_percent")
            final_grade = ""
            for band in grade_bands:
                if total_weighted_percent >= band.min_percent:
                    final_grade = band.grade_code
                    break
            
            # Use maker-checker workflow for submissions
            # Ensure maker is set
            if not evaluation.maker:
                evaluation.maker = request.user
            
            # Determine status based on whether it's a draft or final submission
            # old_status was already captured above if we're editing approved/completed
            # Otherwise, get it here
            if 'old_status' not in locals():
                old_status = evaluation.status
                
            if is_draft:
                # Drafts are handled earlier and return early, so this shouldn't be reached
                pass
            else:
                # ALL final submissions (including approved) go to 'submitted' status for maker-checker workflow
                # This ensures everything goes through review, even when editing approved questionnaires
                
                # CRITICAL: When submitting after editing an approved questionnaire, 
                # DO NOT update total_weighted_percent and final_grade with unapproved scores
                # These fields should ONLY contain approved scores
                # The new unapproved scores will be stored in the version snapshot
                # Only when checker approves will these fields be updated
                
                # CRITICAL: Check if there are already approved scores before updating
                # If approved scores exist, preserve them - don't overwrite with unapproved scores
                if old_status == 'approved' or evaluation.approved_weighted_percent is not None:
                    # There are approved scores - preserve them, don't overwrite with unapproved scores
                    # The approved scores are already in total_weighted_percent and final_grade
                    # Keep them as-is - they represent the last approved state
                    # Also ensure they're preserved in approved_weighted_percent and approved_grade fields
                    if evaluation.total_weighted_percent is not None and evaluation.approved_weighted_percent is None:
                        # If approved scores exist in total_weighted_percent but not in approved_weighted_percent,
                        # copy them to approved fields
                        evaluation.approved_weighted_percent = evaluation.total_weighted_percent
                        evaluation.approved_grade = evaluation.final_grade
                    elif evaluation.approved_weighted_percent is not None:
                        # Restore approved scores to main fields if they were overwritten
                        evaluation.total_weighted_percent = evaluation.approved_weighted_percent
                        evaluation.final_grade = evaluation.approved_grade
                    # Don't update total_weighted_percent/final_grade with new unapproved scores
                else:
                    # No approved scores yet - this is a first submission or resubmission without prior approval
                    # Store scores temporarily (they're not approved yet, but we need to store them somewhere)
                    # They will be moved to approved fields when checker approves
                    evaluation.total_raw_score = total_raw_score
                    evaluation.total_weighted_percent = total_weighted_percent
                    evaluation.final_grade = final_grade
                
                # Store previous scores before updating (for display purposes)
                evaluation.previous_weighted_percent = evaluation.total_weighted_percent
                evaluation.previous_grade = evaluation.final_grade
                
                auto_approve = _can_auto_approve_basel_submission(request.user)

                evaluation.status = 'approved' if auto_approve else 'submitted'
                evaluation.submitted_by = request.user
                evaluation.submitted_at = timezone.now()
                
                # Increment version number for resubmissions
                if old_status in ['approved', 'submitted', 'returned']:
                    evaluation.version = (evaluation.version or 0) + 1
                    evaluation.resubmission_count = (evaluation.resubmission_count or 0) + 1
                elif not evaluation.version:
                    # First submission - set version to 1
                    evaluation.version = 1
                
            # Save the evaluation with all updates (this saves the new scores, status, etc.)
            evaluation.save()
            
            # Create a version snapshot AFTER updating with new values
            # This ensures the latest version reflects the CURRENT submitted state with new scores
            # Only create version snapshot if not a draft
            if not is_draft:
                from scorecard.models import EvaluationVersion
                
                # CRITICAL: Get the highest version number BEFORE deleting unapproved versions
                # This ensures sequential version numbering even after deleting old rejected versions
                last_version = evaluation.versions.order_by('-version_number').first()
                if last_version:
                    # Increment from the highest version number (whether approved or not)
                    final_version_number = last_version.version_number + 1
                else:
                    # No versions exist yet - this is the first submission
                    final_version_number = evaluation.version or 1
                
                # Keep old unapproved/returned versions too; every submission is part of the audit trail.
                # Create version snapshot with the NEW submitted data
                # This will capture all the newly created attribute_responses, driver_scores, section_scores
                # with the updated scores and values
                # IMPORTANT: Pass the calculated scores explicitly, not the evaluation fields
                # because evaluation fields may contain old approved scores that we're preserving
                _create_evaluation_version(
                    evaluation=evaluation,
                    version_number=final_version_number,
                    user=request.user,
                    change_description=f"Basel II score submitted for review - Version {final_version_number} (Score: {total_weighted_percent:.2f}%, Grade: {final_grade})",
                    total_weighted_percent=total_weighted_percent,
                    final_grade=final_grade,
                    total_raw_score=total_raw_score,
                    attribute_version_payload=attribute_version_payload,
                    driver_version_payload=driver_version_payload,
                    section_version_payload=section_version_payload,
                )
                if not auto_approve:
                    log_basel_score_audit(
                        request.user,
                        "submit",
                        evaluation,
                f"Basel II score submitted for review with score {total_weighted_percent:.2f}% and grade {final_grade or '-'} from status {old_status or 'in_progress'}.",
                    )

            if is_draft:
                messages.success(
                    request,
                    f"Draft saved successfully! You can continue working on it later.",
                )
                return redirect("scorecard:basel_scores_draft_list")
            else:
                if old_status == 'approved':
                    comments = 'Basel II score edited from approved state - resubmitted for review'
                    auto_comments = 'Basel II score edited from approved state - auto-approved on submission'
                elif old_status == 'returned':
                    comments = 'Basel II score resubmitted after being returned for changes'
                    auto_comments = 'Basel II score resubmitted after return and auto-approved on submission'
                else:
                    comments = 'Basel II score updated and submitted for review'
                    auto_comments = 'Basel II score updated and auto-approved on submission'

                if auto_approve:
                    _finalize_basel_auto_approval(
                        evaluation,
                        request.user,
                        old_status=old_status if old_status else 'in_progress',
                        comments=auto_comments,
                    )
                    evaluation.refresh_from_db()
                    log_basel_score_audit(
                        request.user,
                        "approve",
                        evaluation,
                    f"Basel II score auto-approved on submission with score {evaluation.total_weighted_percent:.2f}% and grade {evaluation.final_grade or '-'} from status {old_status or 'in_progress'}.",
                    )
                    messages.success(
                        request,
                    f"Basel II score approved automatically on submit. Total Score: {evaluation.total_weighted_percent}%, Grade: {evaluation.final_grade}.",
                    )
                    notify_basel_approved(evaluation, request.user)
                else:
                    EvaluationWorkflowHistory.objects.create(
                        evaluation=evaluation,
                        action='submitted',
                        from_status=old_status if old_status else 'in_progress',
                        to_status='submitted',
                        performed_by=request.user,
                        comments=comments
                    )
                    messages.success(
                        request,
                    f"Basel II score submitted for review! Total Score: {total_weighted_percent}%, Grade: {final_grade}. "
                        f"Waiting for checker approval.",
                    )
                    notify_basel_submitted(evaluation)
                return redirect("scorecard:maker_submitted_list")

    # GET request - show edit form with existing data
    grade_bands = template.grade_bands.all().order_by("display_order")

    # Keep live responses for document display, but restore approved edits from
    # the immutable evaluation snapshot that produced the displayed score.
    attribute_responses_list = list(evaluation.attribute_responses.select_related('option').prefetch_related('documents').all())
    approved_evaluation_version = None
    if evaluation.status in {"approved", "completed"}:
        approved_evaluation_version = (
            evaluation.versions.filter(is_approved=True)
            .order_by("-approved_at", "-version_number")
            .first()
        )

    if template_switched and original_template is not None:
        existing_responses, attribute_responses = _map_responses_to_replacement_template(
            attribute_responses_list,
            template,
        )
    else:
        current_values, attribute_responses = _build_checkbox_form_values_from_responses(
            attribute_responses_list
        )
        if approved_evaluation_version is not None:
            version_responses = list(
                approved_evaluation_version.attribute_responses.select_related(
                    "attribute",
                    "option",
                ).all()
            )
            existing_responses, _ = _build_checkbox_form_values_from_responses(
                version_responses
            )
        else:
            existing_responses = current_values

    current_autofill_payload = (
        _build_basel_autofill_payload(
            request,
            template,
            attributes_by_driver,
            customer_ref_code=evaluation.customer_id,
        )
        if evaluation.customer_id
        else _empty_autofill_payload()
    )
    saved_autofill_metadata = evaluation.autofill_metadata or {}
    autofill_profile_changes = _build_profile_change_rows(saved_autofill_metadata, current_autofill_payload)
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
        "grade_bands": grade_bands,
        "evaluation": evaluation,
        "attribute_responses": attribute_responses,
        "template_switch_notice": template_switch_notice,
        "errors": {},
        "autofill_applied_labels": display_autofill_metadata.get("applied_labels", []),
        "autofill_applied_attribute_ids": display_autofill_metadata.get("applied_attribute_ids", []),
        "autofill_missing_labels": display_autofill_metadata.get("missing_required_labels", []),
        "autofill_missing_attribute_ids": display_autofill_metadata.get("missing_required_attribute_ids", []),
        "autofill_profile_changes": autofill_profile_changes,
        # Pre-populate form with existing data
        "form_data": {
            "branch_name": evaluation.branch_name,
            "customer_name": evaluation.customer_name,
            "customer_id": evaluation.customer_id,
            "attribute_values": existing_responses,
        },
    }

    return render(
        request,
        "credit_scoreshifts/basel_scores_form/basel_scores_edit.html",
        context,
    )


@login_required
def questionnaire_view_detail(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    View to display a submitted questionnaire in read-only mode.
    Shows the same structure as the form but with submitted data.
    
    For approved questionnaires: Shows the approved version's data (currently approved options and scores)
    For submitted questionnaires: Shows current data (pending approval) with indication
    """
    evaluation = get_object_or_404(
        CreditEvaluation.objects.select_related("template").prefetch_related(
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
            "Choose a Basel template first to view this questionnaire in the full template layout.",
        )
        return redirect("scorecard:basel_scores_template_assignment", evaluation_id=evaluation.id)

    # Use approved template version structure to ensure consistency
    sections, drivers_by_section, attributes_by_driver = _build_configuration(template, use_approved_version=True)

    is_preview_mode = request.GET.get("preview") == "1"

    # For approved questionnaires, get the latest approved version's data
    # For submitted/other statuses, use current evaluation data
    approved_version = None
    if evaluation.status == 'approved':
        # Get the latest approved version
        approved_version = evaluation.versions.filter(is_approved=True).order_by('-approved_at', '-version_number').first()
    
    if approved_version:
        # Use approved version's data, but include documents from current evaluation
        # Get current evaluation's attribute responses for documents
        current_responses = {}
        for response in evaluation.attribute_responses.prefetch_related('documents__uploaded_by').all():
            current_responses[response.attribute_id] = response
        
        attribute_responses = {}
        for version_response in approved_version.attribute_responses.all():
            # Create a dict mapping attribute_id to response data
            # Include documents from current evaluation if available
            current_response = current_responses.get(version_response.attribute_id)
            attribute_responses[version_response.attribute_id] = {
                'option_id': version_response.option_id,
                'option': version_response.option,
                'allocated_score': version_response.allocated_score,
                'weighted_percent': getattr(version_response, 'weighted_percent', None),
                'raw_value': version_response.raw_value,
                'documents': current_response.documents.all() if current_response else [],
            }

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
        
        # Use approved version's total score and grade
        display_weighted_percent = approved_version.total_weighted_percent
        display_grade = approved_version.final_grade
    else:
        # Use current evaluation data
        attribute_responses = {}
        for response in evaluation.attribute_responses.prefetch_related('documents__uploaded_by').all():
            attribute_responses[response.attribute_id] = response

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
        display_grade = evaluation.final_grade

    grade_bands = template.grade_bands.all().order_by("display_order")
    
    # Get all history records (all previous values)
    history_records = evaluation.history_records.all().order_by("-recorded_at")
    
    # Get all versions for this evaluation
    all_versions = list(evaluation.versions.all().order_by('-version_number'))
    
    # Get the latest version to show who last modified the current version
    latest_version = all_versions[0] if all_versions else None
    
    # Check if there's a pending submission (status is 'submitted')
    has_pending_submission = evaluation.status == 'submitted'
    active_autofill_metadata = _get_active_autofill_metadata(
        evaluation,
        preferred_version=approved_version,
    )
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
        "grade_bands": grade_bands,
        "history_records": history_records,
        "all_versions": all_versions,
        "latest_version": latest_version,
        "approved_version": approved_version,
        "display_weighted_percent": display_weighted_percent,
        "display_grade": display_grade,
        "has_pending_submission": has_pending_submission,
        "active_autofill_metadata": active_autofill_metadata,
        "is_preview_mode": is_preview_mode,
        "workflow_history": workflow_history,
    }

    template_name = "credit_scoreshifts/basel_scores_form/basel_scores_view.html"
    if getattr(request.resolver_match, "url_name", "") == "checker_my_approvals_basel_score_view":
        template_name = "maker_checker/checker_basel_score_approval_view.html"

    return render(
        request,
        template_name,
        context,
    )


@login_required
def questionnaire_compare_versions(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    View to compare two versions of an evaluation.
    Shows side-by-side comparison of all attribute responses, driver scores, section scores, and summary.
    """
    evaluation = get_object_or_404(
        CreditEvaluation.objects.select_related("template").prefetch_related("versions"),
        id=evaluation_id,
    )
    
    template = evaluation.template
    if not evaluation.template_id or template is None:
        messages.info(
            request,
            "Choose a Basel template first before comparing versions for this questionnaire.",
        )
        return redirect("scorecard:basel_scores_template_assignment", evaluation_id=evaluation.id)

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
    # Prioritize showing latest approved version in comparisons
    # Get the most recent approved version (there can be multiple approved versions)
    approved_versions = sorted(
        [version for version in all_versions if version.is_approved],
        key=lambda version: (version.approved_at or version.created_at, version.version_number),
        reverse=True,
    )
    latest_approved_version = approved_versions[0] if approved_versions else None
    
    if not version1 and not version2:
        if latest_approved_version and total_versions >= 2:
            # Show latest approved version vs latest version (if different)
            version2 = latest_version  # Latest version
            if latest_approved_version != version2:
                version1 = latest_approved_version  # Latest approved version
            else:
                # Latest version is also approved, compare with previous version
                if total_versions >= 2:
                    version1 = all_versions[1]  # Previous version
        elif total_versions >= 2:
            # No approved version yet, show latest two versions
            version2 = latest_version  # Newer (latest version)
            version1 = all_versions[1]  # Older (previous version)
        elif total_versions >= 1:
            version2 = latest_version  # Latest version
            # Version 1 can be current evaluation or None (will be handled below)
    elif not version1 and total_versions >= 1:
        # If version2 is selected but version1 is not, prefer latest approved version
        if latest_approved_version and latest_approved_version != version2:
            version1 = latest_approved_version
        else:
            version2 = latest_version  # Newer (latest version)
            # Version 1 can be current evaluation or None (will be handled below)
    elif not version2 and total_versions >= 1:
        # If version1 is selected but version2 is not, version2 should be newer
        version2 = latest_version  # Newer (latest version)
    
    # Ensure version1 is always older than version2 based on creation date/version number
    # If both versions are selected, swap them if version1 is newer than version2
    if version1 and version2:
        # Compare by version number (higher = newer) or creation date
        v1_is_newer = False
        
        # Check if both are version objects
        if hasattr(version1, 'version_number') and hasattr(version2, 'version_number'):
            # Compare by version number (higher number = newer)
            if version1.version_number > version2.version_number:
                v1_is_newer = True
            elif version1.version_number == version2.version_number:
                # If same version number, compare by date
                if hasattr(version1, 'created_at') and hasattr(version2, 'created_at'):
                    if version1.created_at > version2.created_at:
                        v1_is_newer = True
        elif hasattr(version1, 'created_at') and hasattr(version2, 'created_at'):
            # Compare by date only (if no version numbers)
            if version1.created_at > version2.created_at:
                v1_is_newer = True
        
        # If version1 is newer, swap them so version1 is always older
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
        # Get version 1 data
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
                'final_grade': version1.final_grade,
            }
        }
    
    # Get current evaluation data (for version 2 if not specified)
    # Always use the latest version if it exists and matches current evaluation, otherwise use current evaluation
    if not version2:
        # Always use current evaluation data directly when version2 is not specified
        # This ensures we're comparing against the actual current state, not a potentially outdated version
        v2_attrs = {r.attribute_id: r for r in evaluation.attribute_responses.all()}
        v2_drivers = {s.risk_driver_id: s for s in evaluation.driver_scores.all()}
        v2_sections = {s.section_id: s for s in evaluation.section_scores.all()}
        
        comparison_data['version2'] = {
            'version': None,  # Current evaluation (always use live data)
            'attributes': v2_attrs,
            'drivers': v2_drivers,
            'sections': v2_sections,
            'summary': {
                'total_raw_score': evaluation.total_raw_score,
                'total_weighted_percent': evaluation.total_weighted_percent,
                'final_grade': evaluation.final_grade,
            }
        }
    else:
        # Get version 2 data
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
                'final_grade': version2.final_grade,
            }
        }
    
    # Compare attributes
    if comparison_data['version1'] and comparison_data['version2']:
        v1_attrs = comparison_data['version1']['attributes']
        v2_attrs = comparison_data['version2']['attributes']
        
        all_attr_ids = set(v1_attrs.keys()) | set(v2_attrs.keys())
        
        for attr_id in all_attr_ids:
            v1_attr = v1_attrs.get(attr_id)
            v2_attr = v2_attrs.get(attr_id)
            
            # Get the attribute object to calculate weighted scores
            attribute = None
            for driver_id, attrs in attributes_by_driver.items():
                for attr in attrs:
                    if attr.id == attr_id:
                        attribute = attr
                        break
                if attribute:
                    break
            
            # Calculate attribute-level weighted scores
            v1_weighted_score = None
            v2_weighted_score = None
            weighted_diff = None
            
            if attribute:
                # Get highest possible score for this attribute
                max_option = attribute.options.aggregate(Max('allocated_score'))
                highest_possible_score = Decimal(str(max_option['allocated_score__max'] or 0))
                weight_percent = Decimal(str(attribute.weight_percent or 0))
                
                # Calculate weighted score for version 1
                if v1_attr and highest_possible_score > 0:
                    actual_score_v1 = Decimal(str(v1_attr.allocated_score))
                    v1_weighted_score = float((actual_score_v1 / highest_possible_score) * weight_percent)
                
                # Calculate weighted score for version 2
                if v2_attr and highest_possible_score > 0:
                    actual_score_v2 = Decimal(str(v2_attr.allocated_score))
                    v2_weighted_score = float((actual_score_v2 / highest_possible_score) * weight_percent)
                
                # Calculate weighted difference
                if v1_weighted_score is not None and v2_weighted_score is not None:
                    weighted_diff = v2_weighted_score - v1_weighted_score
                elif v1_weighted_score is not None:
                    weighted_diff = -v1_weighted_score  # Removed
                elif v2_weighted_score is not None:
                    weighted_diff = v2_weighted_score  # Added
            
            # Calculate raw score difference
            score_diff = None
            if v1_attr and v2_attr:
                score_diff = float(v2_attr.allocated_score) - float(v1_attr.allocated_score)
            elif v1_attr:
                score_diff = -float(v1_attr.allocated_score)  # Removed
            elif v2_attr:
                score_diff = float(v2_attr.allocated_score)  # Added
            
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
            
            # Calculate differences
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
            
            # Calculate differences
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
        
        # Calculate differences
        total_weighted_diff = None
        if v1_summary['total_weighted_percent'] is not None and v2_summary['total_weighted_percent'] is not None:
            total_weighted_diff = float(v2_summary['total_weighted_percent']) - float(v1_summary['total_weighted_percent'])
        
        total_raw_diff = None
        if v1_summary['total_raw_score'] is not None and v2_summary['total_raw_score'] is not None:
            total_raw_diff = float(v2_summary['total_raw_score']) - float(v1_summary['total_raw_score'])
        
        if (v1_summary['total_raw_score'] != v2_summary['total_raw_score'] or
            v1_summary['total_weighted_percent'] != v2_summary['total_weighted_percent'] or
            v1_summary['final_grade'] != v2_summary['final_grade']):
            comparison_data['summary_differences'] = {
                'v1': v1_summary,
                'v2': v2_summary,
                'total_weighted_diff': total_weighted_diff,
                'total_raw_diff': total_raw_diff,
            }
    
    # Create lookup dictionaries for attributes and drivers
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
        'credit_scoreshifts/basel_scores_form/basel_scores_compare.html',
        context,
    )


@login_required
def questionnaire_delete_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    View to delete a submitted questionnaire/evaluation.
    """
    evaluation = get_object_or_404(
        CreditEvaluation.objects.select_related("template"),
        id=evaluation_id,
    )

    # Lock deletion if status is 'submitted' (must withdraw first)
    # Approved questionnaires CAN be deleted
    # Returned questionnaires CAN be deleted ONLY from the maker draft list
    if evaluation.status == 'submitted':
        messages.error(
            request,
            f"This questionnaire is submitted for review. "
            f"It cannot be deleted. Please withdraw the submission first."
        )
        return redirect("scorecard:basel_scores_view_detail", evaluation_id=evaluation.id)
    
    if evaluation.status not in ['draft', 'in_progress', 'returned', 'approved']:
        messages.error(request, "Only draft, in-progress, returned, or approved questionnaires can be deleted.")
        return redirect("scorecard:basel_scores_view_detail", evaluation_id=evaluation.id)
    if not evaluation.can_be_edited_by(request.user):
        messages.error(request, "You don't have permission to delete this questionnaire.")
        return redirect("scorecard:basel_scores_view_detail", evaluation_id=evaluation.id)

    if request.method == "POST":
        customer_name = evaluation.customer_name
        template_code, _ = _get_basel_template_label(evaluation)
        
        # Store status before deletion (for redirect)
        deleted_status = evaluation.status
        log_basel_score_audit(
            request.user,
            "delete",
            evaluation,
            f"Basel questionnaire deleted from status {deleted_status}.",
        )
        
        # Delete the evaluation (this will cascade delete all related objects:
        # - AttributeResponse and AttributeResponseDocument
        # - DriverScore
        # - SectionScore
        # - EvaluationWorkflowHistory
        # - EvaluationHistory
        # - EvaluationVersion and all version-related data (CASCADE))
        evaluation.delete()
        
        messages.success(
            request,
            f"Returned questionnaire for '{customer_name}' (Template: {template_code}) has been permanently deleted.",
        )
        
        # Redirect based on status
        if deleted_status == 'returned':
            return redirect("scorecard:maker_draft_list")
        else:
            return redirect("scorecard:basel_scores_submitted_list")

    context = {
        "evaluation": evaluation,
    }

    return render(
        request,
        "credit_scoreshifts/basel_scores_form/basel_scores_delete.html",
        context,
    )


@login_required
def questionnaire_check_existing(request: HttpRequest) -> JsonResponse:
    """
    API endpoint to check if a customer already has a submitted questionnaire.
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
        locked_payload = _get_locked_existing_basel_score_payload(customer_code, current_branch_name=branch.branch_name)
        if locked_payload is not None:
            return JsonResponse(locked_payload)

        existing_evaluation = CreditEvaluation.objects.filter(
            customer_id=customer_code,
            branch_name=branch.branch_name,
        ).first()

        if existing_evaluation:
            return JsonResponse(
                _build_existing_basel_score_payload(
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
def customer_versions_list_view(request: HttpRequest) -> HttpResponse:
    """
    View to list all customers that have questionnaires with versions.
    Shows customers grouped by their evaluations.
    """
    search_query = (request.GET.get("q") or "").strip()
    customer_summaries = CreditEvaluation.objects.filter(versions__isnull=False)

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

    evaluations_by_customer: dict[tuple[str, str, str], list[CreditEvaluation]] = {}
    if page_customers:
        evaluation_filters = Q(pk__in=[])
        for customer in page_customers:
            evaluation_filters |= Q(
                customer_id=customer["customer_id"],
                customer_name=customer["customer_name"],
                branch_name=customer["branch_name"],
            )
        page_evaluations = (
            CreditEvaluation.objects.filter(evaluation_filters, versions__isnull=False)
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
        "credit_scoreshifts/basel_scores_form/basel_scores_customer_versions_list.html",
        context,
    )


@login_required
def customer_evaluation_versions_view(request: HttpRequest, evaluation_id: int) -> HttpResponse:
    """
    View to show all versions for a specific customer evaluation.
    Lists all versions with their details.
    """
    evaluation = get_object_or_404(
        CreditEvaluation.objects.select_related("template", "maker", "submitted_by", "approved_by"),
        id=evaluation_id,
    )
    
    # Get all versions for this evaluation, ordered by version number
    # Show approved versions and the latest unapproved version (if any)
    # Old rejected unapproved versions are deleted on resubmission, so we should only see:
    # - All approved versions (for history)
    # - One unapproved version (the latest submission, if pending)
    all_versions = evaluation.versions.select_related("created_by", "approved_by").order_by('-version_number')
    
    context = {
        "evaluation": evaluation,
        "all_versions": all_versions,
    }
    
    return render(
        request,
        "credit_scoreshifts/basel_scores_form/basel_scores_customer_evaluation_versions.html",
        context,
    )


@login_required
def customer_version_detail_view(request: HttpRequest, version_id: int) -> HttpResponse:
    """
    View to display a specific version in full detail with all selected options.
    Shows complete version snapshot with all attribute responses, scores, etc.
    """
    version = get_object_or_404(
        EvaluationVersion.objects.select_related(
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
    
    grade_bands = template.grade_bands.all().order_by("display_order")
    
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
        "grade_bands": grade_bands,
        "display_weighted_percent": version.total_weighted_percent,
        "display_grade": version.final_grade,
    }
    
    return render(
        request,
        "credit_scoreshifts/basel_scores_form/basel_scores_customer_version_detail.html",
        context,
    )


# Aliases for URL names (basel_scores_*)
basel_scores_template_select_view = questionnaire_template_select_view
basel_scores_form_view = questionnaire_form_view
basel_scores_submitted_list_view = questionnaire_submitted_list_view
basel_scores_draft_list_view = questionnaire_draft_list_view
basel_scores_edit_view = questionnaire_edit_view
basel_scores_view_detail = questionnaire_view_detail
basel_scores_compare_versions = questionnaire_compare_versions
basel_scores_delete_view = questionnaire_delete_view
basel_scores_check_existing = questionnaire_check_existing
basel_scores_export_excel = questionnaire_export_excel
basel_scores_export_csv = questionnaire_export_csv
