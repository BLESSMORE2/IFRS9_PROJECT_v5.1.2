from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from scorecard.functions_view.audit import log_basel_score_audit, log_ifrs9_score_audit
from scorecard.functions_view.basel_scores_form import (
    BULK_WRITE_BATCH_SIZE as BASEL_BULK_WRITE_BATCH_SIZE,
    _build_checkbox_form_values_from_responses,
    _build_configuration as _build_basel_configuration,
    _build_saved_autofill_metadata as _build_saved_basel_autofill_metadata,
    _create_evaluation_version,
    _is_checkbox_attribute,
    _resolve_attribute_submission,
)
from scorecard.functions_view.ifrs9_score_config import (
    _build_configuration as _build_ifrs9_configuration,
)
from scorecard.functions_view.ifrs9_scores_form import (
    BULK_WRITE_BATCH_SIZE as IFRS9_BULK_WRITE_BATCH_SIZE,
    _build_ifrs9_form_values_from_responses,
    _build_saved_autofill_metadata as _build_saved_ifrs9_autofill_metadata,
    _create_ifrs9_evaluation_version,
    _is_ifrs9_checkbox_attribute,
    _resolve_ifrs9_attribute_submission,
)
from scorecard.functions_view.main_customer_lookup import get_main_customer_any_branch
from scorecard.functions_view.scorecard_autofill import (
    build_basel_autofill,
    build_ifrs9_autofill,
)
from scorecard.models import (
    Attribute,
    AttributeResponse,
    CreditEvaluation,
    EvaluationWorkflowHistory,
    IFRS9Attribute,
    IFRS9AttributeResponse,
    IFRS9Evaluation,
    IFRS9EvaluationWorkflowHistory,
    IFRS9RiskDriverScore,
    IFRS9SectionScore,
    RiskDriverScore,
    SectionScore,
)


APPROVED_SCORE_STATUSES = ("approved", "completed")
AUTO_REFRESH_BATCH_DEFAULT = 1000
AUTO_REFRESH_BATCH_MIN = 1
AUTO_REFRESH_BATCH_MAX = 10000


def _as_int_set(value: Any) -> set[int]:
    if value in (None, ""):
        return set()
    if not isinstance(value, (list, tuple, set)):
        value = [value]

    ids: set[int] = set()
    for item in value:
        try:
            ids.add(int(item))
        except (TypeError, ValueError):
            continue
    return ids


def _is_blank_response_value(value: Any) -> bool:
    return value in (None, "", [], ())


def _normalize_response_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(item) for item in value if str(item))
    return str(value)


def _option_display(attribute: Attribute | IFRS9Attribute, value: Any) -> str:
    if _is_blank_response_value(value):
        return "-"
    if isinstance(value, (list, tuple, set)):
        labels = [_option_display(attribute, item) for item in value]
        return ", ".join(label for label in labels if label and label != "-") or "-"
    try:
        option_id = int(value)
    except (TypeError, ValueError):
        return str(value)

    prefetched_options = getattr(attribute, "_prefetched_objects_cache", {}).get("options")
    if prefetched_options is not None:
        option = next((item for item in prefetched_options if item.id == option_id), None)
    else:
        options = getattr(attribute, "options", None)
        if hasattr(options, "filter"):
            option = options.filter(id=option_id).first()
        elif isinstance(options, (list, tuple)):
            option = next((item for item in options if item.id == option_id), None)
        else:
            option = None
    if option is None:
        return str(value)
    return option.label or option.value or str(option.id)


def _build_autofill_payload(result: Any, customer: Any, branch_name: str) -> dict[str, Any]:
    return {
        "attribute_values": dict(getattr(result, "attribute_values", {}) or {}),
        "applied_labels": list(getattr(result, "applied_labels", []) or []),
        "applied_attribute_ids": list(getattr(result, "applied_attribute_ids", []) or []),
        "missing_required_labels": list(getattr(result, "missing_required_labels", []) or []),
        "missing_required_attribute_ids": list(
            getattr(result, "missing_required_attribute_ids", []) or []
        ),
        "profile_snapshot": dict(getattr(result, "profile_snapshot", {}) or {}),
        "customer_name": getattr(customer, "customer_name", "") or "",
        "branch_name": branch_name or "",
    }


def _pick_system_actor(*candidates: Any) -> Any:
    for candidate in candidates:
        if getattr(candidate, "pk", None) and getattr(candidate, "is_active", True):
            return candidate

    user_model = get_user_model()
    return (
        user_model.objects.filter(is_active=True, is_superuser=True).order_by("id").first()
        or user_model.objects.filter(is_active=True, is_staff=True).order_by("id").first()
        or user_model.objects.filter(is_active=True).order_by("id").first()
    )


def _changed_autofill_values(
    *,
    current_values: dict[int, Any],
    suggested_values: dict[int, Any],
    applied_attribute_ids: set[int],
    overridden_attribute_ids: set[int],
    tracked_attribute_ids: set[int],
    attributes_by_id: dict[int, Attribute | IFRS9Attribute],
) -> tuple[dict[int, Any], list[str]]:
    updated_values = dict(current_values)
    changed_fields: list[str] = []

    for attribute_id, suggested_value in suggested_values.items():
        try:
            normalized_attribute_id = int(attribute_id)
        except (TypeError, ValueError):
            continue

        attribute = attributes_by_id.get(normalized_attribute_id)
        if attribute is None:
            continue

        current_value = current_values.get(normalized_attribute_id)
        old_normalized = _normalize_response_value(current_value)
        new_normalized = _normalize_response_value(suggested_value)
        updated_values[normalized_attribute_id] = str(suggested_value)

        if old_normalized != new_normalized:
            changed_fields.append(
                f"{attribute.label}: {_option_display(attribute, current_value)} -> "
                f"{_option_display(attribute, suggested_value)}"
            )

    return updated_values, changed_fields


def _normalized_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    metadata = metadata if isinstance(metadata, dict) else {}
    return {
        "applied_attribute_ids": sorted(_as_int_set(metadata.get("applied_attribute_ids"))),
        "overridden_attribute_ids": sorted(_as_int_set(metadata.get("overridden_attribute_ids"))),
        "suggested_attribute_ids": sorted(_as_int_set(metadata.get("suggested_attribute_ids"))),
        "missing_required_attribute_ids": sorted(
            _as_int_set(metadata.get("missing_required_attribute_ids"))
        ),
        "current_missing_required_ids": sorted(
            _as_int_set(metadata.get("current_missing_required_ids"))
        ),
        "profile_snapshot": dict(metadata.get("profile_snapshot", {}) or {}),
    }


def _metadata_needs_adoption(
    current_metadata: dict[str, Any] | None,
    proposed_metadata: dict[str, Any],
) -> bool:
    return _normalized_metadata(current_metadata) != _normalized_metadata(proposed_metadata)


def _build_metadata_adoption_item(
    *,
    score_type: str,
    evaluation: CreditEvaluation | IFRS9Evaluation,
    actor: Any,
) -> dict[str, Any]:
    return {
        "metadata_only": True,
        "score_type": score_type,
        "customer_code": evaluation.customer_id,
        "customer_name": evaluation.customer_name,
        "branch_name": evaluation.branch_name,
        "template": evaluation.template,
        "updated_by": actor,
    }


def _basel_final_grade(evaluation: CreditEvaluation, total_weighted_percent: Decimal) -> str:
    if not evaluation.template_id:
        return ""
    for band in evaluation.template.grade_bands.all().order_by("-min_percent"):
        if total_weighted_percent >= band.min_percent:
            return band.grade_code
    return ""


def _calculate_basel_submission(
    *,
    evaluation: CreditEvaluation,
    values: dict[int, Any],
    sections: list[Any],
    drivers_by_section: dict[int, list[Any]],
    attributes_by_driver: dict[int, list[Attribute]],
) -> dict[str, Any]:
    driver_raw_scores: dict[int, Decimal] = {}
    driver_weighted_scores: dict[int, Decimal] = {}
    submitted_attribute_specs: list[tuple[Attribute, dict[str, Any]]] = []

    for driver_id, attrs in attributes_by_driver.items():
        driver_total_raw = Decimal("0")
        driver_total_weighted = Decimal("0")
        for attribute in attrs:
            raw_value = values.get(attribute.id, [] if _is_checkbox_attribute(attribute) else "")
            resolved = _resolve_attribute_submission(attribute, raw_value)
            submitted_attribute_specs.append((attribute, resolved))
            if resolved["has_selection"]:
                driver_total_raw += resolved["allocated_score"]
                driver_total_weighted += resolved["weighted_score"]

        driver_raw_scores[driver_id] = driver_total_raw
        driver_weighted_scores[driver_id] = driver_total_weighted

    total_raw_score = Decimal("0")
    section_weighted_totals: dict[int, Decimal] = {}
    driver_score_rows: list[RiskDriverScore] = []
    section_score_rows: list[SectionScore] = []

    for section in sections:
        section_total = Decimal("0")
        section_raw_total = Decimal("0")
        for driver in drivers_by_section.get(section.id, []):
            actual_score = driver_raw_scores.get(driver.id, Decimal("0"))
            weighted_percent = driver_weighted_scores.get(driver.id, Decimal("0"))
            max_score = driver.get_max_score()

            if actual_score == Decimal("0") or actual_score is None:
                proof = str(max_score) if max_score else ""
            elif actual_score != max_score:
                proof = f"ERROR{max_score}" if max_score else "ERROR"
            else:
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

        section_weighted_totals[section.id] = section_total
        section_score_rows.append(
            SectionScore(
                evaluation=evaluation,
                section=section,
                raw_score=section_raw_total,
                weighted_percent=section_total,
            )
        )

    total_weighted_percent = sum(section_weighted_totals.values(), Decimal("0"))
    return {
        "attribute_specs": submitted_attribute_specs,
        "attribute_version_payload": [
            {
                "attribute_id": attribute.id,
                "option_id": (
                    resolved["selected_option"].id
                    if resolved["selected_option"] is not None
                    else None
                ),
                "raw_value": resolved["stored_raw_value"],
                "allocated_score": resolved["allocated_score"],
            }
            for attribute, resolved in submitted_attribute_specs
        ],
        "driver_score_rows": driver_score_rows,
        "driver_version_payload": [
            {
                "risk_driver_id": row.risk_driver_id,
                "raw_score": row.raw_score,
                "weighted_percent": row.weighted_percent,
                "proof": row.proof,
            }
            for row in driver_score_rows
        ],
        "section_score_rows": section_score_rows,
        "section_version_payload": [
            {
                "section_id": row.section_id,
                "raw_score": row.raw_score,
                "weighted_percent": row.weighted_percent,
            }
            for row in section_score_rows
        ],
        "total_raw_score": total_raw_score,
        "total_weighted_percent": total_weighted_percent,
        "final_grade": _basel_final_grade(evaluation, total_weighted_percent),
    }


def _calculate_ifrs9_submission(
    *,
    evaluation: IFRS9Evaluation,
    values: dict[int, Any],
    sections: list[Any],
    drivers_by_section: dict[int, list[Any]],
    attributes_by_driver: dict[int, list[IFRS9Attribute]],
) -> dict[str, Any]:
    driver_raw_scores: dict[int, Decimal] = {}
    driver_weighted_scores: dict[int, Decimal] = {}
    submitted_attribute_specs: list[dict[str, Any]] = []

    for driver_id, attrs in attributes_by_driver.items():
        driver_total_raw = Decimal("0")
        driver_total_weighted = Decimal("0")
        for attribute in attrs:
            raw_value = values.get(attribute.id, [] if _is_ifrs9_checkbox_attribute(attribute) else "")
            resolved = _resolve_ifrs9_attribute_submission(attribute, raw_value)
            submitted_attribute_specs.append(resolved)
            if resolved["has_selection"]:
                driver_total_raw += resolved["allocated_score"]
                driver_total_weighted += resolved["weighted_score"]

        driver_raw_scores[driver_id] = driver_total_raw
        driver_weighted_scores[driver_id] = driver_total_weighted

    total_raw_score = Decimal("0")
    section_weighted_totals: dict[int, Decimal] = {}
    driver_score_rows: list[IFRS9RiskDriverScore] = []
    section_score_rows: list[IFRS9SectionScore] = []

    for section in sections:
        section_total = Decimal("0")
        section_raw_total = Decimal("0")
        for driver in drivers_by_section.get(section.id, []):
            actual_score = driver_raw_scores.get(driver.id, Decimal("0"))
            weighted_percent = driver_weighted_scores.get(driver.id, Decimal("0"))
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

    total_weighted_percent = sum(section_weighted_totals.values(), Decimal("0"))
    return {
        "attribute_specs": submitted_attribute_specs,
        "attribute_version_payload": [
            {
                "attribute_id": spec["attribute"].id,
                "option_id": (
                    spec["selected_option"].id
                    if spec["selected_option"] is not None
                    else None
                ),
                "raw_value": spec["stored_raw_value"],
                "allocated_score": spec["allocated_score"],
            }
            for spec in submitted_attribute_specs
        ],
        "driver_score_rows": driver_score_rows,
        "driver_version_payload": [
            {
                "risk_driver_id": row.risk_driver_id,
                "raw_score": row.raw_score,
                "weighted_percent": row.weighted_percent,
                "proof": row.proof,
            }
            for row in driver_score_rows
        ],
        "section_score_rows": section_score_rows,
        "section_version_payload": [
            {
                "section_id": row.section_id,
                "raw_score": row.raw_score,
                "weighted_percent": row.weighted_percent,
            }
            for row in section_score_rows
        ],
        "total_raw_score": total_raw_score,
        "total_weighted_percent": total_weighted_percent,
        "final_grade": "",
    }


def _save_basel_attribute_responses(
    evaluation: CreditEvaluation,
    attribute_specs: list[tuple[Attribute, dict[str, Any]]],
) -> None:
    attribute_ids = [attribute.id for attribute, _resolved in attribute_specs]
    existing_rows = {
        response.attribute_id: response
        for response in AttributeResponse.objects.filter(
            evaluation=evaluation,
            attribute_id__in=attribute_ids,
        )
    }
    create_rows: list[AttributeResponse] = []
    update_rows: list[AttributeResponse] = []

    for attribute, resolved in attribute_specs:
        response = existing_rows.get(attribute.id)
        if response is None:
            response = AttributeResponse(evaluation=evaluation, attribute=attribute)
            create_rows.append(response)
        else:
            update_rows.append(response)

        selected_option = resolved["selected_option"]
        response.option_id = selected_option.id if selected_option is not None else None
        response.raw_value = resolved["stored_raw_value"]
        response.allocated_score = resolved["allocated_score"]

    if create_rows:
        AttributeResponse.objects.bulk_create(
            create_rows,
            batch_size=BASEL_BULK_WRITE_BATCH_SIZE,
        )
    if update_rows:
        AttributeResponse.objects.bulk_update(
            update_rows,
            ["option", "raw_value", "allocated_score"],
            batch_size=BASEL_BULK_WRITE_BATCH_SIZE,
        )


def _save_ifrs9_attribute_responses(
    evaluation: IFRS9Evaluation,
    attribute_specs: list[dict[str, Any]],
) -> None:
    attribute_ids = [spec["attribute"].id for spec in attribute_specs]
    existing_rows = {
        response.attribute_id: response
        for response in IFRS9AttributeResponse.objects.filter(
            evaluation=evaluation,
            attribute_id__in=attribute_ids,
        )
    }
    create_rows: list[IFRS9AttributeResponse] = []
    update_rows: list[IFRS9AttributeResponse] = []

    for spec in attribute_specs:
        attribute = spec["attribute"]
        response = existing_rows.get(attribute.id)
        if response is None:
            response = IFRS9AttributeResponse(evaluation=evaluation, attribute=attribute)
            create_rows.append(response)
        else:
            update_rows.append(response)

        selected_option = spec["selected_option"]
        response.option_id = selected_option.id if selected_option is not None else None
        response.raw_value = spec["stored_raw_value"]
        response.allocated_score = spec["allocated_score"]

    if create_rows:
        IFRS9AttributeResponse.objects.bulk_create(
            create_rows,
            batch_size=IFRS9_BULK_WRITE_BATCH_SIZE,
        )
    if update_rows:
        IFRS9AttributeResponse.objects.bulk_update(
            update_rows,
            ["option", "raw_value", "allocated_score"],
            batch_size=IFRS9_BULK_WRITE_BATCH_SIZE,
        )


def _next_basel_version_number(evaluation: CreditEvaluation) -> int:
    latest = evaluation.versions.aggregate(latest=Max("version_number")).get("latest")
    return int(latest or evaluation.version or 0) + 1


def _next_ifrs9_version_number(evaluation: IFRS9Evaluation) -> int:
    latest = evaluation.versions.aggregate(latest=Max("version_number")).get("latest")
    return int(latest or evaluation.version or 0) + 1


def _build_updated_item(
    *,
    score_type: str,
    evaluation: CreditEvaluation | IFRS9Evaluation,
    previous_weighted_score: Decimal | None,
    previous_grade: str,
    new_weighted_score: Decimal | None,
    new_grade: str,
    changed_fields: list[str],
    version_number: int,
    actor: Any,
) -> dict[str, Any]:
    return {
        "score_type": score_type,
        "customer_code": evaluation.customer_id,
        "customer_name": evaluation.customer_name,
        "branch_name": evaluation.branch_name,
        "template": evaluation.template,
        "previous_weighted_score": previous_weighted_score,
        "new_weighted_score": new_weighted_score,
        "previous_grade": previous_grade,
        "new_grade": new_grade,
        "changed_fields": changed_fields,
        "version": version_number,
        "updated_by": actor,
        "maker": evaluation.maker,
        "submitted_by": evaluation.submitted_by,
        "checker": evaluation.checker,
        "approved_by": evaluation.approved_by,
    }


def _refresh_one_basel_score(
    evaluation: CreditEvaluation,
    *,
    now,
    dry_run: bool,
) -> dict[str, Any] | None:
    metadata = evaluation.autofill_metadata if isinstance(evaluation.autofill_metadata, dict) else {}
    applied_attribute_ids = _as_int_set(metadata.get("applied_attribute_ids"))
    overridden_attribute_ids = _as_int_set(metadata.get("overridden_attribute_ids"))
    tracked_attribute_ids = (
        applied_attribute_ids
        | overridden_attribute_ids
        | _as_int_set(metadata.get("suggested_attribute_ids"))
    )

    sections, drivers_by_section, attributes_by_driver = _build_basel_configuration(
        evaluation.template,
        use_approved_version=True,
    )
    attributes = [attribute for attrs in attributes_by_driver.values() for attribute in attrs]
    attributes_by_id = {attribute.id: attribute for attribute in attributes}

    customer = get_main_customer_any_branch(evaluation.customer_id)
    if customer is None:
        return None

    autofill_result = build_basel_autofill(
        evaluation.template.code,
        customer,
        attributes_by_driver,
    )
    payload = _build_autofill_payload(autofill_result, customer, evaluation.branch_name)
    suggested_values = payload["attribute_values"]
    if not suggested_values:
        return None

    current_values, _response_map = _build_checkbox_form_values_from_responses(
        list(evaluation.attribute_responses.select_related("attribute", "option"))
    )
    updated_values, changed_fields = _changed_autofill_values(
        current_values=current_values,
        suggested_values=suggested_values,
        applied_attribute_ids=applied_attribute_ids,
        overridden_attribute_ids=overridden_attribute_ids,
        tracked_attribute_ids=tracked_attribute_ids,
        attributes_by_id=attributes_by_id,
    )
    proposed_metadata = _build_saved_basel_autofill_metadata(
        payload,
        attributes_by_driver,
        updated_values,
    )
    actor = _pick_system_actor(evaluation.approved_by, evaluation.checker, evaluation.submitted_by, evaluation.maker)
    if not changed_fields:
        if _metadata_needs_adoption(metadata, proposed_metadata):
            if not dry_run:
                CreditEvaluation.objects.filter(pk=evaluation.pk).update(
                    autofill_metadata=proposed_metadata
                )
            return _build_metadata_adoption_item(
                score_type="Basel II",
                evaluation=evaluation,
                actor=actor,
            )
        return None

    calculated = _calculate_basel_submission(
        evaluation=evaluation,
        values=updated_values,
        sections=sections,
        drivers_by_section=drivers_by_section,
        attributes_by_driver=attributes_by_driver,
    )
    version_number = _next_basel_version_number(evaluation)
    updated_item = _build_updated_item(
        score_type="Basel II",
        evaluation=evaluation,
        previous_weighted_score=evaluation.total_weighted_percent,
        previous_grade=evaluation.final_grade,
        new_weighted_score=calculated["total_weighted_percent"],
        new_grade=calculated["final_grade"],
        changed_fields=changed_fields,
        version_number=version_number,
        actor=actor,
    )

    if dry_run:
        return updated_item

    with transaction.atomic():
        locked_evaluation = CreditEvaluation.objects.select_for_update().get(pk=evaluation.pk)
        _save_basel_attribute_responses(locked_evaluation, calculated["attribute_specs"])
        locked_evaluation.driver_scores.all().delete()
        locked_evaluation.section_scores.all().delete()
        RiskDriverScore.objects.bulk_create(
            [row for row in calculated["driver_score_rows"]],
            batch_size=BASEL_BULK_WRITE_BATCH_SIZE,
        )
        SectionScore.objects.bulk_create(
            [row for row in calculated["section_score_rows"]],
            batch_size=BASEL_BULK_WRITE_BATCH_SIZE,
        )

        locked_evaluation.previous_weighted_percent = evaluation.total_weighted_percent
        locked_evaluation.previous_grade = evaluation.final_grade or ""
        locked_evaluation.total_raw_score = calculated["total_raw_score"]
        locked_evaluation.total_weighted_percent = calculated["total_weighted_percent"]
        locked_evaluation.final_grade = calculated["final_grade"]
        locked_evaluation.approved_weighted_percent = calculated["total_weighted_percent"]
        locked_evaluation.approved_grade = calculated["final_grade"]
        locked_evaluation.status = "approved"
        locked_evaluation.approved_by = actor
        locked_evaluation.approved_at = now
        locked_evaluation.version = version_number
        locked_evaluation.autofill_metadata = proposed_metadata
        locked_evaluation.save()

        version = _create_evaluation_version(
            evaluation=locked_evaluation,
            version_number=version_number,
            user=actor,
            change_description=(
                "Auto update from latest API-backed auto-fill data - "
                f"Version {version_number} (Score: {calculated['total_weighted_percent']:.2f}%, "
                f"Grade: {calculated['final_grade'] or '-'})"
            ),
            total_weighted_percent=calculated["total_weighted_percent"],
            final_grade=calculated["final_grade"],
            total_raw_score=calculated["total_raw_score"],
            attribute_version_payload=calculated["attribute_version_payload"],
            driver_version_payload=calculated["driver_version_payload"],
            section_version_payload=calculated["section_version_payload"],
        )
        version.is_approved = True
        version.approved_at = now
        version.approved_by = actor
        version.save(update_fields=["is_approved", "approved_at", "approved_by"])

        if actor is not None:
            EvaluationWorkflowHistory.objects.create(
                evaluation=locked_evaluation,
                action="approved",
                from_status=evaluation.status,
                to_status="approved",
                performed_by=actor,
                comments="Auto update from latest API-backed auto-fill data.",
            )
        log_basel_score_audit(
            actor,
            "auto_update",
            locked_evaluation,
            (
                f"Auto updated score from {evaluation.total_weighted_percent or 0:.2f}% "
                f"to {calculated['total_weighted_percent']:.2f}%; "
                f"Fields: {'; '.join(changed_fields)}."
            ),
        )

    return updated_item


def _refresh_one_ifrs9_score(
    evaluation: IFRS9Evaluation,
    *,
    now,
    dry_run: bool,
) -> dict[str, Any] | None:
    metadata = evaluation.autofill_metadata if isinstance(evaluation.autofill_metadata, dict) else {}
    applied_attribute_ids = _as_int_set(metadata.get("applied_attribute_ids"))
    overridden_attribute_ids = _as_int_set(metadata.get("overridden_attribute_ids"))
    tracked_attribute_ids = (
        applied_attribute_ids
        | overridden_attribute_ids
        | _as_int_set(metadata.get("suggested_attribute_ids"))
    )

    sections, drivers_by_section, attributes_by_driver = _build_ifrs9_configuration(
        evaluation.template,
        use_approved_version=True,
    )
    attributes = [attribute for attrs in attributes_by_driver.values() for attribute in attrs]
    attributes_by_id = {attribute.id: attribute for attribute in attributes}

    customer = get_main_customer_any_branch(evaluation.customer_id)
    if customer is None:
        return None

    autofill_result = build_ifrs9_autofill(
        evaluation.template.code,
        customer,
        attributes_by_driver,
    )
    payload = _build_autofill_payload(autofill_result, customer, evaluation.branch_name)
    suggested_values = payload["attribute_values"]
    if not suggested_values:
        return None

    current_values = _build_ifrs9_form_values_from_responses(
        list(evaluation.attribute_responses.select_related("attribute", "option"))
    )
    updated_values, changed_fields = _changed_autofill_values(
        current_values=current_values,
        suggested_values=suggested_values,
        applied_attribute_ids=applied_attribute_ids,
        overridden_attribute_ids=overridden_attribute_ids,
        tracked_attribute_ids=tracked_attribute_ids,
        attributes_by_id=attributes_by_id,
    )
    proposed_metadata = _build_saved_ifrs9_autofill_metadata(
        payload,
        attributes_by_driver,
        updated_values,
    )
    actor = _pick_system_actor(evaluation.approved_by, evaluation.checker, evaluation.submitted_by, evaluation.maker)
    if not changed_fields:
        if _metadata_needs_adoption(metadata, proposed_metadata):
            if not dry_run:
                IFRS9Evaluation.objects.filter(pk=evaluation.pk).update(
                    autofill_metadata=proposed_metadata
                )
            return _build_metadata_adoption_item(
                score_type="IFRS9",
                evaluation=evaluation,
                actor=actor,
            )
        return None

    calculated = _calculate_ifrs9_submission(
        evaluation=evaluation,
        values=updated_values,
        sections=sections,
        drivers_by_section=drivers_by_section,
        attributes_by_driver=attributes_by_driver,
    )
    version_number = _next_ifrs9_version_number(evaluation)
    updated_item = _build_updated_item(
        score_type="IFRS9",
        evaluation=evaluation,
        previous_weighted_score=evaluation.total_weighted_percent,
        previous_grade=evaluation.final_grade,
        new_weighted_score=calculated["total_weighted_percent"],
        new_grade="",
        changed_fields=changed_fields,
        version_number=version_number,
        actor=actor,
    )

    if dry_run:
        return updated_item

    with transaction.atomic():
        locked_evaluation = IFRS9Evaluation.objects.select_for_update().get(pk=evaluation.pk)
        _save_ifrs9_attribute_responses(locked_evaluation, calculated["attribute_specs"])
        locked_evaluation.driver_scores.all().delete()
        locked_evaluation.section_scores.all().delete()
        IFRS9RiskDriverScore.objects.bulk_create(
            [row for row in calculated["driver_score_rows"]],
            batch_size=IFRS9_BULK_WRITE_BATCH_SIZE,
        )
        IFRS9SectionScore.objects.bulk_create(
            [row for row in calculated["section_score_rows"]],
            batch_size=IFRS9_BULK_WRITE_BATCH_SIZE,
        )

        locked_evaluation.previous_weighted_percent = evaluation.total_weighted_percent
        locked_evaluation.previous_grade = evaluation.final_grade or ""
        locked_evaluation.total_raw_score = calculated["total_raw_score"]
        locked_evaluation.total_weighted_percent = calculated["total_weighted_percent"]
        locked_evaluation.final_grade = ""
        locked_evaluation.approved_weighted_percent = calculated["total_weighted_percent"]
        locked_evaluation.approved_grade = ""
        locked_evaluation.status = "approved"
        locked_evaluation.approved_by = actor
        locked_evaluation.approved_at = now
        locked_evaluation.version = version_number
        locked_evaluation.autofill_metadata = proposed_metadata
        locked_evaluation.save()

        version = _create_ifrs9_evaluation_version(
            evaluation=locked_evaluation,
            version_number=version_number,
            user=actor,
            change_description=(
                "Auto update from latest API-backed auto-fill data - "
                f"Version {version_number} (Score: {calculated['total_weighted_percent']:.2f}%)"
            ),
            total_weighted_percent=calculated["total_weighted_percent"],
            final_grade="",
            total_raw_score=calculated["total_raw_score"],
            attribute_version_payload=calculated["attribute_version_payload"],
            driver_version_payload=calculated["driver_version_payload"],
            section_version_payload=calculated["section_version_payload"],
        )
        version.is_approved = True
        version.approved_at = now
        version.approved_by = actor
        version.save(update_fields=["is_approved", "approved_at", "approved_by"])

        if actor is not None:
            IFRS9EvaluationWorkflowHistory.objects.create(
                evaluation=locked_evaluation,
                action="approved",
                from_status=evaluation.status,
                to_status="approved",
                performed_by=actor,
                comments="Auto update from latest API-backed auto-fill data.",
            )
        log_ifrs9_score_audit(
            actor,
            "auto_update",
            locked_evaluation,
            (
                f"Auto updated score from {evaluation.total_weighted_percent or 0:.2f}% "
                f"to {calculated['total_weighted_percent']:.2f}%; "
                f"Fields: {'; '.join(changed_fields)}."
            ),
        )

    return updated_item


def _matches_score_types(score_type: str, allowed_score_types: set[str]) -> bool:
    if not allowed_score_types:
        return True
    normalized = score_type.lower()
    return normalized in allowed_score_types


def _normalize_score_types(score_types: Any) -> set[str]:
    if not score_types:
        return set()
    if isinstance(score_types, str):
        score_types = [score_types]
    normalized: set[str] = set()
    for score_type in score_types:
        text = str(score_type or "").strip().lower()
        if text in {"basel", "basel_ii", "basel ii"}:
            normalized.add("basel")
        elif text in {"ifrs9", "ifrs 9"}:
            normalized.add("ifrs9")
    return normalized


def _base_result(*, dry_run: bool) -> dict[str, Any]:
    return {
        "performed": not dry_run,
        "dry_run": dry_run,
        "batch_size": None,
        "checked_count": 0,
        "basel_checked_count": 0,
        "ifrs9_checked_count": 0,
        "updated_count": 0,
        "updated_items": [],
        "skipped_count": 0,
        "adopted_count": 0,
        "error_count": 0,
        "errors": [],
        "basel_cursor_id": 0,
        "ifrs9_cursor_id": 0,
        "basel_cycle_complete": False,
        "ifrs9_cycle_complete": False,
    }


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


def _remaining_checks(result: dict[str, Any], max_checked: int | None) -> int | None:
    if max_checked is None:
        return None
    return max(max_checked - int(result.get("checked_count", 0) or 0), 0)


def _window_limit(
    result: dict[str, Any],
    max_checked: int | None,
    batch_size: int | None,
) -> int | None:
    remaining = _remaining_checks(result, max_checked)
    if batch_size is None:
        return remaining
    if remaining is None:
        return batch_size
    return min(batch_size, remaining)


def _batched_ids(ids: list[int], batch_size: int = 100) -> list[list[int]]:
    return [ids[index : index + batch_size] for index in range(0, len(ids), batch_size)]


def _materialized_candidate_ids(queryset, max_checked: int | None) -> list[int]:
    if max_checked is not None and max_checked <= 0:
        return []
    ids = list(queryset.values_list("pk", flat=True))
    if max_checked is not None:
        return ids[:max_checked]
    return ids


def _candidate_window(
    queryset,
    *,
    after_id: Any = 0,
    max_checked: int | None = None,
) -> tuple[list[int], int, bool]:
    cursor_id = _clean_positive_int(after_id, 0, minimum=0, maximum=2147483647)
    window_qs = queryset.filter(pk__gt=cursor_id) if cursor_id else queryset
    ids = _materialized_candidate_ids(window_qs, max_checked)
    if not ids and cursor_id:
        ids = _materialized_candidate_ids(queryset, max_checked)
    if not ids:
        return [], 0, True

    last_id = int(ids[-1])
    cycle_complete = not queryset.filter(pk__gt=last_id).exists()
    return ids, 0 if cycle_complete else last_id, cycle_complete


def _load_basel_candidate_batch(candidate_ids: list[int]) -> list[CreditEvaluation]:
    return list(
        CreditEvaluation.objects.filter(pk__in=candidate_ids)
        .select_related("template", "maker", "checker", "submitted_by", "approved_by")
        .order_by("id")
    )


def _load_ifrs9_candidate_batch(candidate_ids: list[int]) -> list[IFRS9Evaluation]:
    return list(
        IFRS9Evaluation.objects.filter(pk__in=candidate_ids)
        .select_related("template", "maker", "checker", "submitted_by", "approved_by")
        .order_by("id")
    )


def run_autofilled_score_auto_update(
    *,
    now=None,
    limit: int | None = None,
    max_checked: int | None = None,
    batch_size: int | None = None,
    basel_after_id: int | None = None,
    ifrs9_after_id: int | None = None,
    customer_code: str | None = None,
    score_types: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Refresh approved score forms when saved auto-filled fields now resolve to
    different values from the latest customer and loan source records.
    """
    now = now or timezone.now()
    result = _base_result(dry_run=dry_run)
    allowed_score_types = _normalize_score_types(score_types)
    customer_code = (customer_code or "").strip()
    normalized_batch_size = (
        _clean_positive_int(batch_size, AUTO_REFRESH_BATCH_DEFAULT)
        if batch_size is not None
        else None
    )
    result["batch_size"] = normalized_batch_size
    result["basel_cursor_id"] = _clean_positive_int(
        basel_after_id,
        0,
        minimum=0,
        maximum=2147483647,
    )
    result["ifrs9_cursor_id"] = _clean_positive_int(
        ifrs9_after_id,
        0,
        minimum=0,
        maximum=2147483647,
    )

    if _matches_score_types("basel", allowed_score_types):
        basel_qs = (
            CreditEvaluation.objects.filter(
                status__in=APPROVED_SCORE_STATUSES,
                template__isnull=False,
            )
            .order_by("id")
        )
        if customer_code:
            basel_qs = basel_qs.filter(customer_id=customer_code)

        # SQL Server/ODBC can raise HY010 if a streaming cursor is still being
        # fetched while the loop performs writes. Materialize IDs first, then
        # reload each small batch before doing any update work.
        basel_ids, basel_next_cursor_id, basel_cycle_complete = _candidate_window(
            basel_qs,
            after_id=0 if customer_code else basel_after_id,
            max_checked=_window_limit(result, max_checked, normalized_batch_size),
        )
        basel_last_processed_id: int | None = None
        basel_stopped_early = False
        for candidate_ids in _batched_ids(basel_ids):
            for evaluation in _load_basel_candidate_batch(candidate_ids):
                if max_checked is not None and result["checked_count"] >= max_checked:
                    basel_stopped_early = True
                    break
                result["checked_count"] += 1
                result["basel_checked_count"] += 1
                basel_last_processed_id = int(evaluation.pk)
                try:
                    updated_item = _refresh_one_basel_score(
                        evaluation,
                        now=now,
                        dry_run=dry_run,
                    )
                except Exception as exc:
                    result["error_count"] += 1
                    result["errors"].append(
                        f"Basel evaluation {evaluation.pk} ({evaluation.customer_id}): {exc}"
                    )
                    continue

                if updated_item is None:
                    result["skipped_count"] += 1
                    continue
                if updated_item.get("metadata_only"):
                    result["adopted_count"] += 1
                    continue
                result["updated_items"].append(updated_item)
                result["updated_count"] = len(result["updated_items"])
                if limit is not None and result["updated_count"] >= limit:
                    basel_stopped_early = True
                    break
            if (
                (max_checked is not None and result["checked_count"] >= max_checked)
                or (limit is not None and result["updated_count"] >= limit)
            ):
                break
        if basel_stopped_early and basel_last_processed_id is not None:
            result["basel_cursor_id"] = basel_last_processed_id
            result["basel_cycle_complete"] = False
        else:
            result["basel_cursor_id"] = basel_next_cursor_id
            result["basel_cycle_complete"] = basel_cycle_complete

    if (
        (limit is None or result["updated_count"] < limit)
        and (max_checked is None or result["checked_count"] < max_checked)
        and _matches_score_types("ifrs9", allowed_score_types)
    ):
        ifrs9_qs = (
            IFRS9Evaluation.objects.filter(
                status__in=APPROVED_SCORE_STATUSES,
                template__isnull=False,
            )
            .order_by("id")
        )
        if customer_code:
            ifrs9_qs = ifrs9_qs.filter(customer_id=customer_code)

        ifrs9_ids, ifrs9_next_cursor_id, ifrs9_cycle_complete = _candidate_window(
            ifrs9_qs,
            after_id=0 if customer_code else ifrs9_after_id,
            max_checked=_window_limit(result, max_checked, normalized_batch_size),
        )
        ifrs9_last_processed_id: int | None = None
        ifrs9_stopped_early = False
        for candidate_ids in _batched_ids(ifrs9_ids):
            for evaluation in _load_ifrs9_candidate_batch(candidate_ids):
                if max_checked is not None and result["checked_count"] >= max_checked:
                    ifrs9_stopped_early = True
                    break
                result["checked_count"] += 1
                result["ifrs9_checked_count"] += 1
                ifrs9_last_processed_id = int(evaluation.pk)
                try:
                    updated_item = _refresh_one_ifrs9_score(
                        evaluation,
                        now=now,
                        dry_run=dry_run,
                    )
                except Exception as exc:
                    result["error_count"] += 1
                    result["errors"].append(
                        f"IFRS9 evaluation {evaluation.pk} ({evaluation.customer_id}): {exc}"
                    )
                    continue

                if updated_item is None:
                    result["skipped_count"] += 1
                    continue
                if updated_item.get("metadata_only"):
                    result["adopted_count"] += 1
                    continue
                result["updated_items"].append(updated_item)
                result["updated_count"] = len(result["updated_items"])
                if limit is not None and result["updated_count"] >= limit:
                    ifrs9_stopped_early = True
                    break
            if (
                (max_checked is not None and result["checked_count"] >= max_checked)
                or (limit is not None and result["updated_count"] >= limit)
            ):
                break
        if ifrs9_stopped_early and ifrs9_last_processed_id is not None:
            result["ifrs9_cursor_id"] = ifrs9_last_processed_id
            result["ifrs9_cycle_complete"] = False
        else:
            result["ifrs9_cursor_id"] = ifrs9_next_cursor_id
            result["ifrs9_cycle_complete"] = ifrs9_cycle_complete

    return result


def refresh_autofilled_scores(**kwargs) -> dict[str, Any]:
    return run_autofilled_score_auto_update(**kwargs)
