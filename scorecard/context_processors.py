from __future__ import annotations

from hashlib import md5

from django.apps import apps
from django.core.cache import cache
from django.db import OperationalError, ProgrammingError
from django.db.models import Q

from scorecard.functions_view.customers import _get_customer_list_summary_counts
from scorecard.functions_view.notifications import notification_context
from scorecard.functions_view.main_customer_lookup import (
    get_accessible_branches_for_request,
    is_all_branches_selected,
)
from scorecard.permission_catalog import ROUTE_PERMISSION_MAP
from scorecard.default_role_seeder import ensure_default_role_seeder_synced
from scorecard.workflow_approval import (
    can_user_self_review_score_submission,
    can_user_self_review_template_submission,
)
from scorecard.models import (
    BankBranch,
    BaselScoreSheetTemplate,
    CreditEvaluation,
    IFRS9Evaluation,
    IFRS9ScoreSheetTemplate,
)


SCORECARD_TEMPLATE_ROUTE_NAMES = (
    "api_dashboard",
    "add_customer",
    "basel_template_builder",
    "basel_template_create",
    "basel_template_delete",
    "basel_template_edit",
    "basel_scores_customer_versions_list",
    "basel_scores_compare_versions",
    "basel_scores_edit",
    "basel_scores_submitted_list",
    "basel_scores_template_select",
    "basel_scores_template_assignment",
    "basel_scores_delete",
    "basel_scores_override_grade",
    "basel_scores_view_detail",
    "basel_template_list",
    "branch_master",
    "checker_ifrs9_scores_pending_list",
    "checker_my_approvals",
    "checker_my_approvals_basel_score_view",
    "checker_my_approvals_ifrs9_score_view",
    "checker_my_approvals_basel_template_view",
    "checker_my_approvals_ifrs9_template_view",
    "checker_pending_list",
    "customer_list",
    "customer_without_ifrs9_list",
    "customer_without_questionnaire_list",
    "document_list",
    "email_configuration",
    "ifrs9_section_list",
    "ifrs9_customer_versions_list",
    "ifrs9_scores_compare_versions",
    "ifrs9_scores_edit",
    "ifrs9_scores_submitted_list",
    "ifrs9_scores_template_select",
    "ifrs9_scores_template_assignment",
    "ifrs9_scores_delete",
    "ifrs9_scores_view_detail",
    "historical_scores_list",
    "historical_scores_download",
    "historical_scores_refresh",
    "historical_scores_bulk_delete",
    "ifrs9_results_home",
    "ifrs9_results_extract",
    "ifrs9_results_extract_download",
    "ifrs9_results_ecl_summary",
    "ifrs9_supporting_data",
    "ifrs9_template_builder",
    "ifrs9_template_checker_pending_list",
    "ifrs9_template_create",
    "ifrs9_template_delete",
    "ifrs9_template_edit",
    "ifrs9_template_list",
    "ifrs9_template_maker_draft_list",
    "ifrs9_template_maker_submitted_list",
    "maker_draft_list",
    "maker_basel_scores_view",
    "maker_ifrs9_scores_draft_list",
    "maker_ifrs9_scores_submitted_list",
    "maker_ifrs9_scores_view",
    "maker_submitted_list",
    "notifications",
    "scorecard_dashboard",
    "scorecard_document_list",
    "section_list",
    "switch_branch",
    "settings_audit_trail",
    "settings_permission_matrix",
    "settings_permissions",
    "settings_user_roles",
    "settings_workflow_approvals",
    "template_checker_pending_list",
    "template_maker_draft_list",
    "template_maker_view",
    "template_maker_submitted_list",
    "template_withdraw",
    "ifrs9_template_maker_view",
    "ifrs9_template_withdraw",
    "withdraw_submission",
    "withdraw_ifrs9_scores_submission",
    "upload_home",
)


def _resolve_scorecard_branch_context(request) -> dict[str, object]:
    cached_context = getattr(request, "_scorecard_branch_context", None)
    if cached_context is not None:
        return cached_context

    user = getattr(request, "user", None)
    accessible_branches: list[BankBranch] = []
    current_branch = None
    has_multiple_branches = False
    all_branches_selected = False
    current_branch_label = ""

    if getattr(user, "is_authenticated", False):
        try:
            accessible_branches = get_accessible_branches_for_request(request)
            all_branches_selected = is_all_branches_selected(request)

            current_branch_id = str(request.session.get("current_branch_id") or "").strip()
            current_branch_code = (request.session.get("current_branch_code") or "").strip()
            accessible_branch_map = {
                str(branch.id): branch
                for branch in accessible_branches
                if getattr(branch, "id", None)
            }
            if current_branch_id:
                current_branch = accessible_branch_map.get(current_branch_id)
                if current_branch is None and getattr(user, "is_superuser", False):
                    current_branch = (
                        BankBranch.objects.only("id", "branch_code", "branch_name", "bank_name")
                        .filter(pk=current_branch_id)
                        .first()
                    )
            if current_branch is None and current_branch_code:
                current_branch = (
                    next(
                        (
                            branch
                            for branch in accessible_branches
                            if (branch.branch_code or "").strip() == current_branch_code
                        ),
                        None,
                    )
                )
                if current_branch is None and getattr(user, "is_superuser", False):
                    current_branch = (
                        BankBranch.objects.only("id", "branch_code", "branch_name", "bank_name")
                        .filter(branch_code=current_branch_code)
                        .order_by("branch_name", "id")
                        .first()
                    )

            if current_branch is None:
                current_branch = accessible_branches[0] if accessible_branches else None
                if current_branch is not None:
                    request.session["current_branch_id"] = current_branch.id
                    request.session["current_branch_code"] = current_branch.branch_code

            has_multiple_branches = len(accessible_branches) > 1
            current_branch_label = "ALL ASSIGNED BRANCHES" if all_branches_selected else (
                current_branch.branch_name if current_branch is not None else ""
            )
        except (OperationalError, ProgrammingError):
            accessible_branches = []
            current_branch = None
            has_multiple_branches = False
            all_branches_selected = False
            current_branch_label = ""

    branch_context = {
        "accessible_branches": accessible_branches,
        "current_branch": current_branch,
        "has_multiple_branches": has_multiple_branches,
        "all_branches_selected": all_branches_selected,
        "current_branch_label": current_branch_label,
    }
    request._scorecard_branch_context = branch_context
    request._scorecard_accessible_branches = accessible_branches
    request._scorecard_current_branch = current_branch
    request._scorecard_all_branches_selected = all_branches_selected
    return branch_context


def _user_can_access_scorecard_route(user, route_name: str) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True

    permission_code = ROUTE_PERMISSION_MAP.get(route_name)
    if not permission_code:
        return True
    if isinstance(permission_code, (list, tuple, set)):
        return any(user.has_perm(code) for code in permission_code)
    return user.has_perm(permission_code)


def _build_scorecard_route_access(request) -> tuple[dict[str, bool], dict[str, bool]]:
    cached_route_access = getattr(request, "_scorecard_route_access", None)
    cached_nav_visibility = getattr(request, "_scorecard_nav_visibility", None)
    if cached_route_access is not None and cached_nav_visibility is not None:
        return cached_route_access, cached_nav_visibility

    user = getattr(request, "user", None)
    route_access = {
        route_name: _user_can_access_scorecard_route(user, route_name)
        for route_name in SCORECARD_TEMPLATE_ROUTE_NAMES
    }
    can_view_basel_templates_module = bool(
        getattr(user, "is_authenticated", False)
        and (
            getattr(user, "is_superuser", False)
            or user.has_perm("scorecard.view_basel_templates")
            or user.has_perm("scorecard.manage_basel_templates")
        )
    )
    can_view_ifrs9_templates_module = bool(
        getattr(user, "is_authenticated", False)
        and (
            getattr(user, "is_superuser", False)
            or user.has_perm("scorecard.view_ifrs9_templates")
            or user.has_perm("scorecard.manage_ifrs9_templates")
        )
    )
    can_view_basel_scores_module = bool(
        getattr(user, "is_authenticated", False)
        and (
            getattr(user, "is_superuser", False)
            or user.has_perm("scorecard.view_basel_scores")
            or user.has_perm("scorecard.manage_basel_scores")
            or user.has_perm("scorecard.reopen_basel_scores")
        )
    )
    can_view_ifrs9_scores_module = bool(
        getattr(user, "is_authenticated", False)
        and (
            getattr(user, "is_superuser", False)
            or user.has_perm("scorecard.view_ifrs9_scores")
            or user.has_perm("scorecard.manage_ifrs9_scores")
            or user.has_perm("scorecard.reopen_ifrs9_scores")
        )
    )
    ifrs9_results_available = apps.is_installed("IFRS9")
    nav_visibility = {
        "dashboard": route_access["scorecard_dashboard"],
        "api": route_access["api_dashboard"],
        "notifications": route_access["notifications"],
        "settings_permissions_workspace": any(
            route_access[route_name]
            for route_name in (
                "settings_permissions",
                "settings_user_roles",
                "settings_permission_matrix",
                "settings_audit_trail",
            )
        ),
        "settings_branch_master": route_access["branch_master"],
        "settings_email": route_access["email_configuration"],
        "settings_workflow": route_access["settings_workflow_approvals"],
        "settings_audit": route_access["settings_audit_trail"],
        "basel_templates": can_view_basel_templates_module and route_access["basel_template_list"],
        "ifrs9_templates": can_view_ifrs9_templates_module and route_access["ifrs9_template_list"],
        "basel_scores": can_view_basel_scores_module and any(
            route_access[route_name]
            for route_name in (
                "basel_scores_template_select",
                "basel_scores_submitted_list",
                "basel_scores_customer_versions_list",
            )
        ),
        "ifrs9_scores": can_view_ifrs9_scores_module and any(
            route_access[route_name]
            for route_name in (
                "ifrs9_scores_template_select",
                "ifrs9_scores_submitted_list",
                "ifrs9_customer_versions_list",
            )
        ),
        "customers": any(
            route_access[route_name]
            for route_name in (
                "customer_list",
                "add_customer",
                "customer_without_questionnaire_list",
                "customer_without_ifrs9_list",
            )
        ),
        "maker": any(
            route_access[route_name]
            for route_name in (
                "maker_draft_list",
                "maker_submitted_list",
                "template_maker_draft_list",
                "template_maker_submitted_list",
                "ifrs9_template_maker_draft_list",
                "ifrs9_template_maker_submitted_list",
                "maker_ifrs9_scores_draft_list",
                "maker_ifrs9_scores_submitted_list",
            )
        ),
        "checker": any(
            route_access[route_name]
            for route_name in (
                "checker_pending_list",
                "template_checker_pending_list",
                "ifrs9_template_checker_pending_list",
                "checker_ifrs9_scores_pending_list",
            )
        ),
        "ifrs9_supporting_data": route_access["ifrs9_supporting_data"],
        "ifrs9_results": ifrs9_results_available and route_access["ifrs9_results_home"],
        "historical_scores": route_access["historical_scores_list"],
        "documents": any(
            route_access[route_name]
            for route_name in (
                "document_list",
                "scorecard_document_list",
            )
        ),
        "imports": route_access["upload_home"],
    }

    request._scorecard_route_access = route_access
    request._scorecard_nav_visibility = nav_visibility
    return route_access, nav_visibility


CHECKER_PENDING_COUNTS_CACHE_TTL_SECONDS = 60
MAKER_QUEUE_COUNTS_CACHE_TTL_SECONDS = 60
CUSTOMER_QUEUE_COUNTS_CACHE_TTL_SECONDS = 120
CHECKER_PENDING_COUNTS_VERSION_CACHE_KEY = "scorecard:checker-pending:version"
MAKER_QUEUE_COUNTS_VERSION_CACHE_KEY = "scorecard:maker-queue:version"
CUSTOMER_QUEUE_COUNTS_VERSION_CACHE_KEY = "scorecard:customer-queue:version"


def _branch_names_from_context(branch_context: dict[str, object]) -> list[str]:
    accessible_branches = branch_context.get("accessible_branches") or []
    all_branches_selected = bool(branch_context.get("all_branches_selected"))
    current_branch = branch_context.get("current_branch")
    if all_branches_selected:
        return [
            (getattr(branch, "branch_name", "") or "").strip()
            for branch in accessible_branches
            if getattr(branch, "branch_name", None)
        ]
    if current_branch is not None and getattr(current_branch, "branch_name", None):
        return [(getattr(current_branch, "branch_name", "") or "").strip()]
    return []


def _checker_pending_counts_cache_key(
    user,
    branch_names: list[str],
    *,
    basel_self_review_allowed: bool,
    basel_template_self_review_allowed: bool,
    ifrs9_self_review_allowed: bool,
    ifrs9_template_self_review_allowed: bool,
) -> str:
    digest_source = "|".join(sorted(name.strip() for name in branch_names if name.strip()))
    digest = md5(digest_source.encode("utf-8")).hexdigest()[:12] if digest_source else "none"
    return (
        f"scorecard:checker-pending:{getattr(user, 'pk', 0)}:"
        f"{_get_checker_pending_counts_version()}:"
        f"{int(bool(getattr(user, 'is_superuser', False)))}:"
        f"{int(basel_self_review_allowed)}:{int(basel_template_self_review_allowed)}:"
        f"{int(ifrs9_self_review_allowed)}:{int(ifrs9_template_self_review_allowed)}:{digest}"
    )


def _get_checker_pending_counts_version() -> int:
    try:
        return int(cache.get(CHECKER_PENDING_COUNTS_VERSION_CACHE_KEY, 1))
    except (TypeError, ValueError):
        return 1


def bump_checker_pending_counts_version() -> None:
    cache.set(
        CHECKER_PENDING_COUNTS_VERSION_CACHE_KEY,
        _get_checker_pending_counts_version() + 1,
        None,
    )


def _get_maker_queue_counts_version() -> int:
    try:
        return int(cache.get(MAKER_QUEUE_COUNTS_VERSION_CACHE_KEY, 1))
    except (TypeError, ValueError):
        return 1


def bump_maker_queue_counts_version() -> None:
    cache.set(
        MAKER_QUEUE_COUNTS_VERSION_CACHE_KEY,
        _get_maker_queue_counts_version() + 1,
        None,
    )


def _get_customer_queue_counts_version() -> int:
    try:
        return int(cache.get(CUSTOMER_QUEUE_COUNTS_VERSION_CACHE_KEY, 1))
    except (TypeError, ValueError):
        return 1


def bump_customer_queue_counts_version() -> None:
    cache.set(
        CUSTOMER_QUEUE_COUNTS_VERSION_CACHE_KEY,
        _get_customer_queue_counts_version() + 1,
        None,
    )


def _get_checker_pending_counts(request, branch_context: dict[str, object]) -> dict[str, int]:
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {
            "basel_scores": 0,
            "basel_templates": 0,
            "ifrs9_templates": 0,
            "ifrs9_scores": 0,
        }

    branch_names = _branch_names_from_context(branch_context)
    basel_self_review_allowed = can_user_self_review_score_submission(user, "basel_scores", "scorecard.review_basel_scores")
    basel_template_self_review_allowed = can_user_self_review_template_submission(
        user,
        "basel_templates",
        "scorecard.review_basel_templates",
    )
    ifrs9_self_review_allowed = can_user_self_review_score_submission(user, "ifrs9_scores", "scorecard.review_ifrs9_scores")
    ifrs9_template_self_review_allowed = can_user_self_review_template_submission(
        user,
        "ifrs9_templates",
        "scorecard.review_ifrs9_templates",
    )
    cache_key = _checker_pending_counts_cache_key(
        user,
        branch_names,
        basel_self_review_allowed=basel_self_review_allowed,
        basel_template_self_review_allowed=basel_template_self_review_allowed,
        ifrs9_self_review_allowed=ifrs9_self_review_allowed,
        ifrs9_template_self_review_allowed=ifrs9_template_self_review_allowed,
    )
    cached_counts = cache.get(cache_key)
    if cached_counts is not None:
        return cached_counts

    try:
        basel_scores = CreditEvaluation.objects.filter(
            Q(status="submitted") | Q(status="completed", checker__isnull=True, approved_by__isnull=True)
        )
        if not getattr(user, "is_superuser", False):
            basel_scores = basel_scores.filter(Q(checker=user) | Q(checker__isnull=True))
        if branch_names:
            basel_scores = basel_scores.filter(branch_name__in=branch_names)
        if not basel_self_review_allowed:
            basel_scores = basel_scores.exclude(maker=user)

        ifrs9_scores = IFRS9Evaluation.objects.filter(
            Q(status="submitted") | Q(status="completed", checker__isnull=True, approved_by__isnull=True)
        )
        if not getattr(user, "is_superuser", False):
            ifrs9_scores = ifrs9_scores.filter(Q(checker=user) | Q(checker__isnull=True))
        if branch_names:
            ifrs9_scores = ifrs9_scores.filter(branch_name__in=branch_names)
        if not ifrs9_self_review_allowed:
            ifrs9_scores = ifrs9_scores.exclude(maker=user)

        basel_templates = BaselScoreSheetTemplate.objects.filter(status="submitted")
        ifrs9_templates = IFRS9ScoreSheetTemplate.objects.filter(status="submitted")
        if not getattr(user, "is_superuser", False):
            basel_templates = basel_templates.filter(Q(checker=user) | Q(checker__isnull=True))
            ifrs9_templates = ifrs9_templates.filter(Q(checker=user) | Q(checker__isnull=True))
        if not basel_template_self_review_allowed:
            basel_templates = basel_templates.exclude(Q(maker=user) | Q(submitted_by=user))
        if not ifrs9_template_self_review_allowed:
            ifrs9_templates = ifrs9_templates.exclude(Q(maker=user) | Q(submitted_by=user))

        counts = {
            "basel_scores": basel_scores.count(),
            "basel_templates": basel_templates.count(),
            "ifrs9_templates": ifrs9_templates.count(),
            "ifrs9_scores": ifrs9_scores.count(),
        }
        counts["total"] = (
            counts["basel_scores"]
            + counts["basel_templates"]
            + counts["ifrs9_templates"]
            + counts["ifrs9_scores"]
        )
        counts["total"] = (
            counts["basel_scores"]
            + counts["basel_templates"]
            + counts["ifrs9_templates"]
            + counts["ifrs9_scores"]
        )
    except (OperationalError, ProgrammingError):
        counts = {
            "basel_scores": 0,
            "basel_templates": 0,
            "ifrs9_templates": 0,
            "ifrs9_scores": 0,
            "total": 0,
        }

    cache.set(cache_key, counts, CHECKER_PENDING_COUNTS_CACHE_TTL_SECONDS)
    return counts


def _maker_queue_counts_cache_key(user, branch_names: list[str]) -> str:
    digest_source = "|".join(sorted(name.strip() for name in branch_names if name.strip()))
    digest = md5(digest_source.encode("utf-8")).hexdigest()[:12] if digest_source else "none"
    return (
        f"scorecard:maker-queue:{getattr(user, 'pk', 0)}:"
        f"{_get_maker_queue_counts_version()}:"
        f"{int(bool(getattr(user, 'is_superuser', False)))}:{digest}"
    )


def _get_maker_queue_counts(request, branch_context: dict[str, object]) -> dict[str, int]:
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {
            "basel_score_drafts": 0,
            "basel_score_submissions": 0,
            "basel_template_drafts": 0,
            "basel_template_submissions": 0,
            "ifrs9_template_drafts": 0,
            "ifrs9_template_submissions": 0,
            "ifrs9_score_drafts": 0,
            "ifrs9_score_submissions": 0,
            "total": 0,
        }

    branch_names = _branch_names_from_context(branch_context)
    cache_key = _maker_queue_counts_cache_key(user, branch_names)
    cached_counts = cache.get(cache_key)
    if cached_counts is not None:
        return cached_counts

    try:
        basel_score_drafts = CreditEvaluation.objects.filter(
            maker=user,
            status__in=["draft", "in_progress", "returned"],
        )
        basel_score_submissions = CreditEvaluation.objects.filter(
            Q(maker=user) | Q(maker__isnull=True),
            status__in=["submitted", "approved", "returned", "completed"],
        )
        ifrs9_score_drafts = IFRS9Evaluation.objects.filter(
            maker=user,
            status__in=["draft", "in_progress", "returned"],
        )
        ifrs9_score_submissions = IFRS9Evaluation.objects.filter(
            Q(maker=user) | Q(maker__isnull=True),
            status__in=["submitted", "approved", "returned", "completed"],
        )
        if branch_names:
            basel_score_drafts = basel_score_drafts.filter(branch_name__in=branch_names)
            basel_score_submissions = basel_score_submissions.filter(branch_name__in=branch_names)
            ifrs9_score_drafts = ifrs9_score_drafts.filter(branch_name__in=branch_names)
            ifrs9_score_submissions = ifrs9_score_submissions.filter(branch_name__in=branch_names)

        basel_template_drafts = BaselScoreSheetTemplate.objects.filter(
            maker=user,
            status__in=["draft", "in_progress", "returned"],
        )
        basel_template_submissions = BaselScoreSheetTemplate.objects.filter(
            maker=user,
            status__in=["submitted", "approved", "returned"],
        )
        ifrs9_template_drafts = IFRS9ScoreSheetTemplate.objects.filter(
            maker=user,
            status__in=["draft", "in_progress", "returned"],
        )
        ifrs9_template_submissions = IFRS9ScoreSheetTemplate.objects.filter(
            maker=user,
            status__in=["submitted", "approved", "returned"],
        )

        counts = {
            "basel_score_drafts": basel_score_drafts.count(),
            "basel_score_submissions": basel_score_submissions.count(),
            "basel_template_drafts": basel_template_drafts.count(),
            "basel_template_submissions": basel_template_submissions.count(),
            "ifrs9_template_drafts": ifrs9_template_drafts.count(),
            "ifrs9_template_submissions": ifrs9_template_submissions.count(),
            "ifrs9_score_drafts": ifrs9_score_drafts.count(),
            "ifrs9_score_submissions": ifrs9_score_submissions.count(),
        }
        counts["total"] = (
            counts["basel_score_drafts"]
            + counts["basel_template_drafts"]
            + counts["ifrs9_template_drafts"]
            + counts["ifrs9_score_drafts"]
        )
    except (OperationalError, ProgrammingError):
        counts = {
            "basel_score_drafts": 0,
            "basel_score_submissions": 0,
            "basel_template_drafts": 0,
            "basel_template_submissions": 0,
            "ifrs9_template_drafts": 0,
            "ifrs9_template_submissions": 0,
            "ifrs9_score_drafts": 0,
            "ifrs9_score_submissions": 0,
            "total": 0,
        }

    cache.set(cache_key, counts, MAKER_QUEUE_COUNTS_CACHE_TTL_SECONDS)
    return counts


def _customer_queue_counts_cache_key(user, branch_context: dict[str, object]) -> str:
    branch_names = _branch_names_from_context(branch_context)
    digest_source = "|".join(sorted(name.strip() for name in branch_names if name.strip()))
    digest = md5(digest_source.encode("utf-8")).hexdigest()[:12] if digest_source else "none"
    return (
        f"scorecard:customer-queue:{getattr(user, 'pk', 0)}:"
        f"{_get_customer_queue_counts_version()}:"
        f"{int(bool(getattr(user, 'is_superuser', False)))}:{digest}"
    )


def _get_customer_queue_counts(request, *, refresh_if_stale: bool = False) -> dict[str, int]:
    user = getattr(request, "user", None)
    branch_context = _resolve_scorecard_branch_context(request)
    cache_key = _customer_queue_counts_cache_key(user, branch_context)
    cached_counts = cache.get(cache_key)
    from scorecard.functions_view.customers import _get_customer_list_summary_version

    summary_version = _get_customer_list_summary_version()
    if cached_counts is not None and (
        not refresh_if_stale or cached_counts.get("_summary_version") == summary_version
    ):
        return cached_counts

    try:
        summary_counts = _get_customer_list_summary_counts(request)
        without_basel_total = summary_counts.get("without_basel_total", 0)
        without_ifrs9_total = summary_counts.get("without_ifrs9_total", 0)
        counts = {
            "without_basel_total": without_basel_total,
            "without_ifrs9_total": without_ifrs9_total,
            "total": without_basel_total + without_ifrs9_total,
            "_summary_version": summary_version,
        }
    except (OperationalError, ProgrammingError):
        counts = {
            "without_basel_total": 0,
            "without_ifrs9_total": 0,
            "total": 0,
            "_summary_version": summary_version,
        }
    cache.set(cache_key, counts, CUSTOMER_QUEUE_COUNTS_CACHE_TTL_SECONDS)
    return counts


def _empty_checker_pending_counts() -> dict[str, int]:
    return {
        "basel_scores": 0,
        "basel_templates": 0,
        "ifrs9_templates": 0,
        "ifrs9_scores": 0,
        "total": 0,
    }


def _empty_maker_queue_counts() -> dict[str, int]:
    return {
        "basel_score_drafts": 0,
        "basel_score_submissions": 0,
        "basel_template_drafts": 0,
        "basel_template_submissions": 0,
        "ifrs9_template_drafts": 0,
        "ifrs9_template_submissions": 0,
        "ifrs9_score_drafts": 0,
        "ifrs9_score_submissions": 0,
        "total": 0,
    }


def _empty_customer_queue_counts() -> dict[str, int]:
    return {
        "without_basel_total": 0,
        "without_ifrs9_total": 0,
        "total": 0,
    }


def _empty_notification_context() -> dict[str, int | bool]:
    return {
        "scorecard_notification_unread_count": 0,
        "scorecard_notification_has_unread": False,
        "scorecard_notification_total_count": 0,
        "scorecard_notification_has_any": False,
    }


def scorecard_base_context(request):
    ensure_default_role_seeder_synced()
    branch_context = _resolve_scorecard_branch_context(request)
    route_access, nav_visibility = _build_scorecard_route_access(request)
    context = notification_context(request) if nav_visibility.get("notifications") else _empty_notification_context()
    context.update(branch_context)
    context["scorecard_checker_pending_counts"] = (
        _get_checker_pending_counts(request, branch_context)
        if nav_visibility.get("checker")
        else _empty_checker_pending_counts()
    )
    context["scorecard_maker_queue_counts"] = (
        _get_maker_queue_counts(request, branch_context)
        if nav_visibility.get("maker")
        else _empty_maker_queue_counts()
    )
    context["scorecard_customer_counts"] = (
        _get_customer_queue_counts(request)
        if nav_visibility.get("customers")
        else _empty_customer_queue_counts()
    )
    context["can_switch_scorecard_branch"] = bool(branch_context.get("has_multiple_branches")) and route_access.get("switch_branch", False)
    context["scorecard_route_access"] = route_access
    context["scorecard_nav_visibility"] = nav_visibility
    return context
