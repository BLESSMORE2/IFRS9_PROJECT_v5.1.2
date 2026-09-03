from collections import defaultdict
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.db.models import Count, Avg, Q, F
from django.db.models.functions import TruncMonth
from datetime import datetime, timedelta
from django.utils import timezone
from dateutil.relativedelta import relativedelta
import json
from ..check_package_expiry import check_expiry as check_scorecard_expiry
from scorecard.functions_view.customers import (
    _build_stage_backed_customer_records,
    _count_collection,
    _get_filtered_customers_without_ifrs9,
    _get_filtered_customers_without_questionnaires,
    _get_stage_customer_population_snapshot,
)
from scorecard.functions_view.main_customer_lookup import main_customer_branch_filter, resolve_branch_context
from scorecard.functions_view.main_customer_lookup import (
    build_request_main_customer_branch_filter,
    get_request_branch_names,
    get_request_branch_scope,
    is_all_branches_selected,
)
from scorecard.context_processors import _build_scorecard_route_access

from scorecard.models import (
    ApiImportRun,
    ApiMainSyncRun,
    AttributeResponseDocument,
    BankBranch,
    BaselScoreSheetTemplate,
    CreditEvaluation,
    GradeBand,
    HistoricalScore,
    IFRS9AttributeResponseDocument,
    IFRS9Evaluation,
    IFRS9ScoreSheetTemplate,
    MainCustomer,
    ScorecardDocument,
)

DASHBOARD_SECTION_CACHE_TTL_SECONDS = 120


def _dashboard_can_access_any(request: HttpRequest, permission_codes: tuple[str, ...]) -> bool:
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return any(user.has_perm(permission_code) for permission_code in permission_codes)


def _build_dashboard_access(request: HttpRequest) -> dict[str, bool]:
    route_access, _nav_visibility = _build_scorecard_route_access(request)
    full_dashboard = _dashboard_can_access_any(
        request,
        ("scorecard.view_full_scorecard_dashboard",),
    )

    customers = full_dashboard or _dashboard_can_access_any(
        request,
        ("scorecard.view_scorecard_customers", "scorecard.manage_scorecard_customers"),
    )
    basel_scores = full_dashboard or _dashboard_can_access_any(
        request,
        (
            "scorecard.view_basel_scores",
            "scorecard.manage_basel_scores",
            "scorecard.review_basel_scores",
            "scorecard.reopen_basel_scores",
        ),
    )
    ifrs9_scores = full_dashboard or _dashboard_can_access_any(
        request,
        (
            "scorecard.view_ifrs9_scores",
            "scorecard.manage_ifrs9_scores",
            "scorecard.review_ifrs9_scores",
            "scorecard.reopen_ifrs9_scores",
        ),
    )
    basel_templates = full_dashboard or _dashboard_can_access_any(
        request,
        (
            "scorecard.view_basel_templates",
            "scorecard.manage_basel_templates",
            "scorecard.review_basel_templates",
        ),
    )
    ifrs9_templates = full_dashboard or _dashboard_can_access_any(
        request,
        (
            "scorecard.view_ifrs9_templates",
            "scorecard.manage_ifrs9_templates",
            "scorecard.review_ifrs9_templates",
        ),
    )
    api = full_dashboard or _dashboard_can_access_any(
        request,
        (
            "scorecard.view_scorecard_api",
            "scorecard.manage_scorecard_api_settings",
            "scorecard.manage_scorecard_api_endpoints",
            "scorecard.manage_scorecard_api_schedules",
            "scorecard.run_scorecard_api_operations",
        ),
    )
    documents = full_dashboard or _dashboard_can_access_any(
        request,
        ("scorecard.view_scorecard_documents", "scorecard.manage_scorecard_documents"),
    )

    quick_actions = any(
        route_access.get(route_name, False)
        for route_name in (
            "add_customer",
            "basel_scores_template_select",
            "ifrs9_scores_template_select",
            "customer_list",
            "api_dashboard",
            "document_list",
        )
    )

    operations = documents or api or basel_scores or ifrs9_scores
    charts = basel_scores or api
    recent_activity = basel_scores or ifrs9_scores or api
    operational_snapshot = basel_scores or ifrs9_scores or api
    quick_links = any(
        route_access.get(route_name, False)
        for route_name in (
            "checker_pending_list",
            "checker_ifrs9_scores_pending_list",
            "customer_without_questionnaire_list",
            "customer_without_ifrs9_list",
            "document_list",
            "api_health",
        )
    )
    templates = basel_templates or ifrs9_templates
    content_grid = recent_activity or basel_scores or operational_snapshot or quick_links or templates
    any_section = customers or basel_scores or ifrs9_scores or templates or operations or charts or content_grid

    return {
        "full_dashboard": full_dashboard,
        "quick_actions": quick_actions,
        "quick_add_customer": route_access.get("add_customer", False),
        "quick_new_basel": route_access.get("basel_scores_template_select", False),
        "quick_new_ifrs9": route_access.get("ifrs9_scores_template_select", False),
        "quick_all_customers": route_access.get("customer_list", False),
        "quick_api_workspace": route_access.get("api_dashboard", False),
        "quick_documents": route_access.get("document_list", False),
        "customers": customers,
        "basel_scores": basel_scores,
        "ifrs9_scores": ifrs9_scores,
        "basel_templates": basel_templates,
        "ifrs9_templates": ifrs9_templates,
        "templates": templates,
        "operations": operations,
        "operations_documents": documents,
        "operations_library_documents": documents,
        "operations_historical": basel_scores or ifrs9_scores,
        "operations_api": api,
        "charts": charts,
        "charts_basel": basel_scores,
        "charts_api": api,
        "recent_activity": recent_activity,
        "recent_basel": basel_scores,
        "recent_ifrs9": ifrs9_scores,
        "recent_api": api,
        "grade_distribution": basel_scores,
        "top_templates": basel_scores,
        "operational_snapshot": operational_snapshot,
        "snapshot_historical": basel_scores or ifrs9_scores,
        "snapshot_api": api,
        "snapshot_main_sync": api,
        "quick_links": quick_links,
        "quick_link_basel_pending": route_access.get("checker_pending_list", False),
        "quick_link_ifrs9_pending": route_access.get("checker_ifrs9_scores_pending_list", False),
        "quick_link_customers_without_basel": route_access.get("customer_without_questionnaire_list", False),
        "quick_link_customers_without_ifrs9": route_access.get("customer_without_ifrs9_list", False),
        "quick_link_documents": route_access.get("document_list", False),
        "quick_link_api_health": route_access.get("api_health", False),
        "content_grid": content_grid,
        "any_section": any_section,
    }


def _dashboard_scope_key(request: HttpRequest, current_branch) -> str:
    if is_all_branches_selected(request):
        branch_names = sorted(name.lower() for name in get_request_branch_names(request))
        return f"branches:{'|'.join(branch_names) if branch_names else 'none'}"
    if current_branch is not None:
        return f"branch:{(current_branch.branch_name or '').strip().lower()}"
    if request.user.is_superuser:
        return "all"
    return "none"


def _dashboard_base_querysets(request: HttpRequest, current_branch):
    customers_qs = MainCustomer.objects.filter(is_active_for_scoring=True)
    evaluations_qs = CreditEvaluation.objects.all()
    ifrs9_evaluations_qs = IFRS9Evaluation.objects.all()
    evaluation_documents_qs = AttributeResponseDocument.objects.all()
    ifrs9_documents_qs = IFRS9AttributeResponseDocument.objects.all()
    historical_scores_qs = HistoricalScore.objects.all()

    branch_scope = get_request_branch_scope(request)
    branch_names = [
        (branch.branch_name or "").strip()
        for branch in branch_scope
        if (branch.branch_name or "").strip()
    ]

    if is_all_branches_selected(request) and branch_scope:
        customers_qs = customers_qs.filter(build_request_main_customer_branch_filter(request))
        evaluations_qs = evaluations_qs.filter(branch_name__in=branch_names)
        ifrs9_evaluations_qs = ifrs9_evaluations_qs.filter(branch_name__in=branch_names)
        evaluation_documents_qs = evaluation_documents_qs.filter(
            attribute_response__evaluation__branch_name__in=branch_names
        )
        ifrs9_documents_qs = ifrs9_documents_qs.filter(
            attribute_response__evaluation__branch_name__in=branch_names
        )
        historical_scores_qs = historical_scores_qs.filter(branch_name__in=branch_names)
    elif current_branch is not None:
        branch_name = current_branch.branch_name
        customers_qs = customers_qs.filter(main_customer_branch_filter(branch_name, current_branch.branch_code))
        evaluations_qs = evaluations_qs.filter(branch_name=branch_name)
        ifrs9_evaluations_qs = ifrs9_evaluations_qs.filter(branch_name=branch_name)
        evaluation_documents_qs = evaluation_documents_qs.filter(
            attribute_response__evaluation__branch_name=branch_name
        )
        ifrs9_documents_qs = ifrs9_documents_qs.filter(
            attribute_response__evaluation__branch_name=branch_name
        )
        historical_scores_qs = historical_scores_qs.filter(branch_name=branch_name)
    elif not request.user.is_superuser:
        customers_qs = customers_qs.none()
        evaluations_qs = evaluations_qs.none()
        ifrs9_evaluations_qs = ifrs9_evaluations_qs.none()
        evaluation_documents_qs = evaluation_documents_qs.none()
        ifrs9_documents_qs = ifrs9_documents_qs.none()
        historical_scores_qs = historical_scores_qs.none()

    return (
        customers_qs,
        evaluations_qs,
        ifrs9_evaluations_qs,
        evaluation_documents_qs,
        ifrs9_documents_qs,
        historical_scores_qs,
    )


def _dashboard_cached_section(scope_key: str, section_name: str, builder, ttl: int = DASHBOARD_SECTION_CACHE_TTL_SECONDS) -> dict:
    cache_key = f"scorecard:dashboard:section:{section_name}:{scope_key}"
    context = cache.get(cache_key)
    if context is None:
        context = builder()
        cache.set(cache_key, context, ttl)
    return context


def _normalize_dashboard_grade(value) -> str:
    return str(value or "").strip().upper()


def _effective_basel_grade(evaluation: CreditEvaluation) -> str:
    if getattr(evaluation, "override_grade", ""):
        return _normalize_dashboard_grade(evaluation.override_grade)
    if getattr(evaluation, "status", "") in {"submitted", "returned"}:
        return _normalize_dashboard_grade(getattr(evaluation, "approved_grade", "") or "")
    return _normalize_dashboard_grade(getattr(evaluation, "final_grade", "") or "")


def _build_dashboard_npl_summary(request: HttpRequest, current_branch) -> dict:
    branch_scope = get_request_branch_scope(request)

    # NPL must be calculated from CustomerLoan only.
    # Do not include CustomerOverdraft customers in the NPL denominator or numerator.
    loan_only_source_settings = {
        "include_loans": True,
        "include_overdrafts": False,
    }

    snapshot = (
        _get_stage_customer_population_snapshot(branch_scope, loan_only_source_settings)
        if branch_scope
        else {"rows": []}
    )

    active_customer_codes = {
        str(row.get("customer_ref_code") or "").strip()
        for row in (snapshot.get("rows") or [])
        if str(row.get("customer_ref_code") or "").strip()
    }

    denominator = len(active_customer_codes)

    if not denominator:
        return {
            "npl_customer_count": 0,
            "npl_denominator": 0,
            "npl_ratio": 0,
        }

    npl_codes: set[str] = set()
    npl_grade_set = {"C", "D", "E"}

    grade_candidates = (
        CreditEvaluation.objects.filter(customer_id__in=active_customer_codes)
        .exclude(status__in=["draft", "in_progress", "cancelled"])
        .order_by("customer_id", "-approved_at", "-submitted_at", "-updated_at", "-created_at")
    )

    seen_codes: set[str] = set()

    for evaluation in grade_candidates:
        customer_code = str(evaluation.customer_id or "").strip()

        if not customer_code or customer_code in seen_codes:
            continue

        seen_codes.add(customer_code)

        if _effective_basel_grade(evaluation) in npl_grade_set:
            npl_codes.add(customer_code)

    return {
        "npl_customer_count": len(npl_codes),
        "npl_denominator": denominator,
        "npl_ratio": round((len(npl_codes) / denominator * 100), 2) if denominator else 0,
    }


def _build_dashboard_customer_section(request: HttpRequest, current_branch) -> dict:
    customers_qs, evaluations_qs, ifrs9_evaluations_qs, _evaluation_documents_qs, _ifrs9_documents_qs, _historical_scores_qs = _dashboard_base_querysets(request, current_branch)
    customers_without_questionnaires = _count_collection(_get_filtered_customers_without_questionnaires(request))
    customers_without_ifrs9 = _count_collection(_get_filtered_customers_without_ifrs9(request))
    total_customers = customers_qs.count()

    customers_with_questionnaires = evaluations_qs.count()
    total_contracts = customers_with_questionnaires + customers_without_questionnaires
    questionnaire_coverage = (
        round((customers_with_questionnaires / total_contracts * 100), 1)
        if total_contracts > 0
        else 0
    )
    pending_coverage = (
        round((customers_without_questionnaires / total_contracts * 100), 1)
        if total_contracts > 0
        else 0
    )

    customers_with_ifrs9 = ifrs9_evaluations_qs.count()
    total_ifrs9_contracts = customers_with_ifrs9 + customers_without_ifrs9
    ifrs9_coverage = (
        round((customers_with_ifrs9 / total_ifrs9_contracts * 100), 1)
        if total_ifrs9_contracts > 0
        else 0
    )
    ifrs9_pending_coverage = (
        round((customers_without_ifrs9 / total_ifrs9_contracts * 100), 1)
        if total_ifrs9_contracts > 0
        else 0
    )

    npl_summary = _build_dashboard_npl_summary(request, current_branch)

    return {
        "total_customers": total_customers,
        "total_contracts": total_contracts,
        "customers_with_questionnaires": customers_with_questionnaires,
        "customers_without_questionnaires": customers_without_questionnaires,
        "customers_with_ifrs9": customers_with_ifrs9,
        "customers_without_ifrs9": customers_without_ifrs9,
        "questionnaire_coverage": questionnaire_coverage,
        "pending_coverage": pending_coverage,
        "ifrs9_coverage": ifrs9_coverage,
        "ifrs9_pending_coverage": ifrs9_pending_coverage,
        **npl_summary,
    }


def _build_dashboard_basel_section(request: HttpRequest, current_branch) -> dict:
    _customers_qs, evaluations_qs, _ifrs9_evaluations_qs, _evaluation_documents_qs, _ifrs9_documents_qs, _historical_scores_qs = _dashboard_base_querysets(request, current_branch)
    evaluation_summary = evaluations_qs.aggregate(total=Count("id"), avg_score=Avg("total_weighted_percent"), avg_raw=Avg("total_raw_score"))
    total_questionnaires = evaluation_summary["total"] or 0
    avg_weighted_score = evaluation_summary["avg_score"] or 0
    avg_raw_score = evaluation_summary["avg_raw"] or 0
    questionnaires_by_template = (
        list(
            evaluations_qs.exclude(template_id__isnull=True)
            .values("template_id", "template__code", "template__name")
            .annotate(
                count=Count("id"),
                customer_count=Count("customer_id", distinct=True),
            )
            .order_by("-customer_count", "template__code")
        )
        if total_questionnaires
        else []
    )
    grade_distribution = list(evaluations_qs.values("final_grade").annotate(count=Count("id")).order_by("-count")) if total_questionnaires else []
    thirty_days_ago = timezone.now() - timedelta(days=30)
    questionnaires_last_30_days = evaluations_qs.filter(created_at__gte=thirty_days_ago).count()
    now = timezone.now()
    current_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    questionnaires_this_month = evaluations_qs.filter(created_at__gte=current_month_start).count()
    questionnaires_this_year = evaluations_qs.filter(created_at__year=now.year).count()
    basel_by_status = dict(evaluations_qs.values("status").annotate(count=Count("id")).values_list("status", "count"))
    basel_pending_review = basel_by_status.get("submitted", 0)
    basel_drafts = basel_by_status.get("draft", 0) + basel_by_status.get("in_progress", 0) + basel_by_status.get("returned", 0)
    basel_approved = basel_by_status.get("approved", 0) + basel_by_status.get("completed", 0)

    used_template_ids = evaluations_qs.values_list("template_id", flat=True).distinct()
    score_distribution_by_grade = []
    if used_template_ids:
        grade_band_lookup = {}
        for grade_band in GradeBand.objects.filter(template_id__in=used_template_ids).order_by("grade_code", "template_id", "id"):
            grade_band_lookup.setdefault(grade_band.grade_code, grade_band)
        for grade_code in grade_distribution:
            grade_code_val = grade_code.get("final_grade", "")
            grade_band = grade_band_lookup.get(grade_code_val)
            if grade_code_val and grade_band:
                score_distribution_by_grade.append(
                    {
                        "grade_code": grade_code_val,
                        "count": grade_code.get("count", 0),
                        "min_percent": float(grade_band.min_percent),
                        "max_percent": float(grade_band.max_percent),
                        "description": grade_band.description or "",
                    }
                )
        score_distribution_by_grade.sort(key=lambda item: item["max_percent"], reverse=True)

    return {
        "total_questionnaires": total_questionnaires,
        "avg_weighted_score": round(float(avg_weighted_score), 2) if avg_weighted_score else 0,
        "avg_raw_score": round(float(avg_raw_score), 2) if avg_raw_score else 0,
        "questionnaires_by_template": questionnaires_by_template,
        "basel_customers_by_template": questionnaires_by_template,
        "grade_distribution": grade_distribution,
        "score_distribution_by_grade": score_distribution_by_grade,
        "questionnaires_last_30_days": questionnaires_last_30_days,
        "questionnaires_this_month": questionnaires_this_month,
        "questionnaires_this_year": questionnaires_this_year,
        "basel_pending_review": basel_pending_review,
        "basel_drafts": basel_drafts,
        "basel_approved": basel_approved,
    }


def _build_dashboard_ifrs9_section(request: HttpRequest, current_branch) -> dict:
    _customers_qs, _evaluations_qs, ifrs9_evaluations_qs, _evaluation_documents_qs, _ifrs9_documents_qs, _historical_scores_qs = _dashboard_base_querysets(request, current_branch)
    now = timezone.now()
    current_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total_ifrs9 = ifrs9_evaluations_qs.count()
    ifrs9_this_month = ifrs9_evaluations_qs.filter(created_at__gte=current_month_start).count()
    ifrs9_this_year = ifrs9_evaluations_qs.filter(created_at__year=now.year).count()
    ifrs9_by_status = dict(ifrs9_evaluations_qs.values("status").annotate(count=Count("id")).values_list("status", "count"))
    ifrs9_customers_by_template = (
        list(
            ifrs9_evaluations_qs.exclude(template_id__isnull=True)
            .values("template_id", "template__code", "template__name")
            .annotate(
                count=Count("id"),
                customer_count=Count("customer_id", distinct=True),
            )
            .order_by("-customer_count", "template__code")
        )
        if total_ifrs9
        else []
    )
    return {
        "total_ifrs9": total_ifrs9,
        "ifrs9_customers_by_template": ifrs9_customers_by_template,
        "ifrs9_this_month": ifrs9_this_month,
        "ifrs9_this_year": ifrs9_this_year,
        "ifrs9_pending_review": ifrs9_by_status.get("submitted", 0),
        "ifrs9_drafts": ifrs9_by_status.get("draft", 0) + ifrs9_by_status.get("in_progress", 0) + ifrs9_by_status.get("returned", 0),
        "ifrs9_approved": ifrs9_by_status.get("approved", 0) + ifrs9_by_status.get("completed", 0),
    }


def _build_dashboard_templates_ops_section(request: HttpRequest, current_branch) -> dict:
    _customers_qs, _evaluations_qs, _ifrs9_evaluations_qs, evaluation_documents_qs, ifrs9_documents_qs, historical_scores_qs = _dashboard_base_querysets(request, current_branch)
    active_basel_templates = BaselScoreSheetTemplate.objects.filter(is_active=True).count()
    active_ifrs9_templates = IFRS9ScoreSheetTemplate.objects.filter(is_active=True).count()
    evaluation_documents_count = evaluation_documents_qs.count() + ifrs9_documents_qs.count()
    scorecard_documents_count = ScorecardDocument.objects.filter(is_active=True).count()
    latest_historical_reporting_date = historical_scores_qs.values_list("reporting_date", flat=True).first()
    latest_historical_rows = 0
    latest_historical_complete = 0
    if latest_historical_reporting_date:
        latest_historical_qs = historical_scores_qs.filter(reporting_date=latest_historical_reporting_date)
        latest_historical_rows = latest_historical_qs.count()
        latest_historical_complete = latest_historical_qs.filter(
            basel_ii_score__isnull=False,
            ifrs_9_score__isnull=False,
        ).count()

    seven_days_ago = timezone.now() - timedelta(days=7)
    api_last_7_day_summary = ApiImportRun.objects.filter(started_at__gte=seven_days_ago).aggregate(
        success_count=Count("id", filter=Q(status=ApiImportRun.STATUS_SUCCESS)),
        failed_count=Count("id", filter=Q(status=ApiImportRun.STATUS_FAILED)),
    )
    main_sync_last_7_day_summary = ApiMainSyncRun.objects.filter(started_at__gte=seven_days_ago).aggregate(
        success_count=Count("id", filter=Q(status=ApiMainSyncRun.STATUS_SUCCESS)),
        failed_count=Count("id", filter=Q(status=ApiMainSyncRun.STATUS_FAILED)),
    )
    latest_main_sync_run = ApiMainSyncRun.objects.order_by("-started_at").first()

    return {
        "active_basel_templates": active_basel_templates,
        "active_ifrs9_templates": active_ifrs9_templates,
        "evaluation_documents_count": evaluation_documents_count,
        "scorecard_documents_count": scorecard_documents_count,
        "latest_historical_reporting_date": latest_historical_reporting_date,
        "latest_historical_rows": latest_historical_rows,
        "latest_historical_complete": latest_historical_complete,
        "api_success_last_7_days": api_last_7_day_summary["success_count"] or 0,
        "api_failed_last_7_days": api_last_7_day_summary["failed_count"] or 0,
        "main_sync_success_last_7_days": main_sync_last_7_day_summary["success_count"] or 0,
        "main_sync_failed_last_7_days": main_sync_last_7_day_summary["failed_count"] or 0,
        "latest_main_sync_run": latest_main_sync_run,
    }


def _build_dashboard_charts_section(request: HttpRequest, current_branch) -> dict:
    _customers_qs, evaluations_qs, _ifrs9_evaluations_qs, _evaluation_documents_qs, _ifrs9_documents_qs, _historical_scores_qs = _dashboard_base_querysets(request, current_branch)
    monthly_questionnaires = []
    monthly_customers = []
    month_labels = []
    now = timezone.now()
    current_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_ranges = []
    for i in range(12, -1, -1):
        month_start = current_month - relativedelta(months=i)
        month_key = (month_start.year, month_start.month)
        month_label = month_start.strftime("%b") if month_start.year == now.year else month_start.strftime("%b %Y")
        month_ranges.append((month_key, month_label))

    monthly_evaluation_map = {}
    for row in (
        evaluations_qs.annotate(month=TruncMonth("created_at"))
        .values("month")
        .annotate(questionnaire_count=Count("id"), customer_count=Count("customer_id", distinct=True))
    ):
        month_value = row.get("month")
        if month_value:
            monthly_evaluation_map[(month_value.year, month_value.month)] = row

    for month_key, month_label in month_ranges:
        month_row = monthly_evaluation_map.get(month_key, {})
        month_labels.append(month_label)
        monthly_questionnaires.append(month_row.get("questionnaire_count", 0))
        monthly_customers.append(month_row.get("customer_count", 0))

    api_import_success_series = []
    api_import_failed_series = []
    api_import_month_labels = []
    api_month_summary = defaultdict(lambda: {"success": 0, "failed": 0})
    for row in (
        ApiImportRun.objects.annotate(month=TruncMonth("started_at"))
        .values("month", "status")
        .annotate(count=Count("id"))
    ):
        month_value = row.get("month")
        if not month_value:
            continue
        bucket = api_month_summary[(month_value.year, month_value.month)]
        if row.get("status") == ApiImportRun.STATUS_SUCCESS:
            bucket["success"] = row.get("count", 0)
        elif row.get("status") == ApiImportRun.STATUS_FAILED:
            bucket["failed"] = row.get("count", 0)

    for i in range(6, -1, -1):
        month_start = current_month - relativedelta(months=i)
        month_key = (month_start.year, month_start.month)
        month_label = month_start.strftime("%b") if month_start.year == now.year else month_start.strftime("%b %Y")
        api_import_month_labels.append(month_label)
        api_import_success_series.append(api_month_summary[month_key]["success"])
        api_import_failed_series.append(api_month_summary[month_key]["failed"])

    return {
        "monthly_questionnaires": json.dumps(monthly_questionnaires),
        "monthly_customers": json.dumps(monthly_customers),
        "month_labels": json.dumps(month_labels),
        "api_import_success_series": json.dumps(api_import_success_series),
        "api_import_failed_series": json.dumps(api_import_failed_series),
        "api_import_month_labels": json.dumps(api_import_month_labels),
    }


def _build_dashboard_recent_activity_section(request: HttpRequest, current_branch) -> dict:
    _customers_qs, evaluations_qs, ifrs9_evaluations_qs, _evaluation_documents_qs, _ifrs9_documents_qs, _historical_scores_qs = _dashboard_base_querysets(request, current_branch)
    recent_questionnaires = list(
        evaluations_qs.select_related("template")
        .only("customer_name", "created_at", "final_grade", "total_weighted_percent", "template__code")
        .order_by("-created_at")[:10]
    )
    recent_ifrs9 = list(
        ifrs9_evaluations_qs.select_related("template")
        .only("customer_name", "created_at", "total_weighted_percent", "template__code")
        .order_by("-created_at")[:5]
    )
    recent_import_runs = list(
        ApiImportRun.objects.select_related("endpoint")
        .only("status", "fetched", "started_at", "endpoint__name")
        .order_by("-started_at")[:5]
    )
    latest_import_run = recent_import_runs[0] if recent_import_runs else None
    return {
        "recent_questionnaires": recent_questionnaires,
        "top_templates": [],
        "recent_activity": recent_questionnaires[:5],
        "recent_ifrs9": recent_ifrs9,
        "recent_import_runs": recent_import_runs,
        "latest_import_run": latest_import_run,
    }


def _build_dashboard_page_context(request: HttpRequest) -> dict:
    current_branch = resolve_branch_context(request)
    scope_key = _dashboard_scope_key(request, current_branch)
    dashboard_access = _build_dashboard_access(request)

    needs_customers = any(
        (
            dashboard_access["customers"],
            dashboard_access["quick_link_customers_without_basel"],
            dashboard_access["quick_link_customers_without_ifrs9"],
        )
    )
    needs_basel = any(
        (
            dashboard_access["basel_scores"],
            dashboard_access["grade_distribution"],
            dashboard_access["top_templates"],
            dashboard_access["quick_link_basel_pending"],
        )
    )
    needs_ifrs9 = any(
        (
            dashboard_access["ifrs9_scores"],
            dashboard_access["quick_link_ifrs9_pending"],
        )
    )
    needs_template_ops = any(
        (
            dashboard_access["templates"],
            dashboard_access["operations"],
            dashboard_access["operational_snapshot"],
            dashboard_access["quick_link_documents"],
            dashboard_access["quick_link_api_health"],
        )
    )
    needs_charts = dashboard_access["charts"]
    needs_recent_activity = dashboard_access["recent_activity"]

    customer_context = (
        _dashboard_cached_section(
            scope_key,
            "customers",
            lambda: _build_dashboard_customer_section(request, current_branch),
        )
        if needs_customers
        else {}
    )
    basel_context = (
        _dashboard_cached_section(
            scope_key,
            "basel",
            lambda: _build_dashboard_basel_section(request, current_branch),
        )
        if needs_basel
        else {}
    )
    ifrs9_context = (
        _dashboard_cached_section(
            scope_key,
            "ifrs9",
            lambda: _build_dashboard_ifrs9_section(request, current_branch),
        )
        if needs_ifrs9
        else {}
    )
    template_ops_context = (
        _dashboard_cached_section(
            scope_key,
            "template_ops",
            lambda: _build_dashboard_templates_ops_section(request, current_branch),
        )
        if needs_template_ops
        else {}
    )
    charts_context = (
        _dashboard_cached_section(
            scope_key,
            "charts",
            lambda: _build_dashboard_charts_section(request, current_branch),
        )
        if needs_charts
        else {}
    )
    recent_activity_context = (
        _dashboard_cached_section(
            scope_key,
            "recent_activity",
            lambda: _build_dashboard_recent_activity_section(request, current_branch),
        )
        if needs_recent_activity
        else {}
    )
    recent_activity_context["top_templates"] = (
        list(basel_context.get("questionnaires_by_template", [])[:3])
        if dashboard_access["top_templates"]
        else []
    )

    return {
        **customer_context,
        **basel_context,
        **ifrs9_context,
        **template_ops_context,
        **charts_context,
        **recent_activity_context,
        "dashboard_access": dashboard_access,
        "current_branch": current_branch,
        "expiry_message": check_scorecard_expiry(),
    }


@login_required
def dashboard_view(request: HttpRequest) -> HttpResponse:
    return render(request, 'credit_scoreshifts/dashboard/dashboard.html', {})


@login_required
def dashboard_content_view(request: HttpRequest) -> HttpResponse:
    context = _build_dashboard_page_context(request)
    return render(request, 'credit_scoreshifts/dashboard/_dashboard_content.html', context)
