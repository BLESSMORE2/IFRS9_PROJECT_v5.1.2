from __future__ import annotations

from django.db.models import Q, Case, When, Value, IntegerField
from django.http import HttpRequest

from scorecard.models import BankBranch, MainCustomer


UNASSIGNED_BRANCH_NAME = "UNASSIGNED"
UNASSIGNED_BRANCH_CODE = "UNASSIGNED"
ALL_BRANCHES_SESSION_FLAG = "scorecard_all_branches"


def get_accessible_branches_for_request(request: HttpRequest) -> list[BankBranch]:
    cached_accessible_branches = getattr(request, "_scorecard_accessible_branches", None)
    if cached_accessible_branches is not None:
        return list(cached_accessible_branches)

    accessible_branches: list[BankBranch] = []
    if getattr(request.user, "is_superuser", False):
        accessible_branches = list(
            BankBranch.objects.only("id", "branch_code", "branch_name", "bank_name").order_by("branch_name", "id")
        )
    elif hasattr(request.user, "get_accessible_branches"):
        accessible_source = request.user.get_accessible_branches()
        if hasattr(accessible_source, "only"):
            accessible_branches = list(
                accessible_source.only("id", "branch_code", "branch_name", "bank_name").order_by("branch_name", "id")
            )
        else:
            accessible_branches = list(accessible_source)

    request._scorecard_accessible_branches = accessible_branches
    return list(accessible_branches)


def is_all_branches_selected(request: HttpRequest) -> bool:
    if not getattr(request.user, "is_authenticated", False):
        return False
    if not bool(request.session.get(ALL_BRANCHES_SESSION_FLAG)):
        return False
    return len(get_accessible_branches_for_request(request)) > 1


def get_request_branch_scope(request: HttpRequest) -> list[BankBranch]:
    accessible_branches = get_accessible_branches_for_request(request)
    if not accessible_branches:
        return []
    if is_all_branches_selected(request):
        return accessible_branches
    current_branch = resolve_branch_context(request)
    return [current_branch] if current_branch is not None else []


def get_request_branch_names(request: HttpRequest) -> list[str]:
    branch_names: list[str] = []
    seen_names: set[str] = set()
    for branch in get_request_branch_scope(request):
        branch_name = (branch.branch_name or "").strip()
        if branch_name and branch_name not in seen_names:
            seen_names.add(branch_name)
            branch_names.append(branch_name)
    return branch_names


def build_request_main_customer_branch_filter(request: HttpRequest) -> Q:
    branch_scope = get_request_branch_scope(request)
    if not branch_scope:
        return Q(pk__in=[])

    if len(branch_scope) > 1:
        branch_codes = [
            (branch.branch_code or "").strip()
            for branch in branch_scope
            if (branch.branch_code or "").strip()
        ]
        branch_names = [
            (branch.branch_name or "").strip()
            for branch in branch_scope
            if (branch.branch_name or "").strip()
        ]
        branch_filter = Q(pk__in=[])
        if branch_codes:
            branch_filter |= Q(branch_code__in=branch_codes)
        if branch_names:
            branch_filter |= Q(branch_name__in=branch_names)
        return branch_filter | Q(branch_code__iexact=UNASSIGNED_BRANCH_CODE) | Q(branch_name__iexact=UNASSIGNED_BRANCH_NAME)

    branch_filter = Q(pk__in=[])
    for branch in branch_scope:
        branch_filter |= main_customer_branch_filter(branch.branch_name, branch.branch_code)
    return branch_filter


def current_branch_display_name(request: HttpRequest) -> str:
    if is_all_branches_selected(request):
        return "ALL ASSIGNED BRANCHES"
    current_branch = resolve_branch_context(request)
    return current_branch.branch_name if current_branch is not None else ""


def main_customer_branch_filter(branch_name: str, branch_code: str = "") -> Q:
    cleaned_branch_name = (branch_name or "").strip()
    cleaned_branch_code = (branch_code or "").strip()
    if not cleaned_branch_name and not cleaned_branch_code:
        return Q(pk__in=[])
    branch_match = Q(pk__in=[])
    if cleaned_branch_code and cleaned_branch_name:
        branch_match |= Q(branch_code__iexact=cleaned_branch_code, branch_name__iexact=cleaned_branch_name)
    elif cleaned_branch_code:
        branch_match |= Q(branch_code__iexact=cleaned_branch_code)
    elif cleaned_branch_name:
        branch_match |= Q(branch_name__iexact=cleaned_branch_name)
    return branch_match | Q(branch_code__iexact=UNASSIGNED_BRANCH_CODE) | Q(branch_name__iexact=UNASSIGNED_BRANCH_NAME)


def _branch_priority(branch_name: str, branch_code: str = "") -> Case:
    cleaned_branch_name = (branch_name or "").strip()
    cleaned_branch_code = (branch_code or "").strip()
    clauses = []
    if cleaned_branch_code and cleaned_branch_name:
        clauses.append(
            When(
                branch_code__iexact=cleaned_branch_code,
                branch_name__iexact=cleaned_branch_name,
                then=Value(0),
            )
        )
    elif cleaned_branch_code:
        clauses.append(When(branch_code__iexact=cleaned_branch_code, then=Value(0)))
    elif cleaned_branch_name:
        clauses.append(When(branch_name__iexact=cleaned_branch_name, then=Value(0)))
    clauses.extend(
        [
            When(branch_code__iexact=UNASSIGNED_BRANCH_CODE, then=Value(1)),
            When(branch_name__iexact=UNASSIGNED_BRANCH_NAME, then=Value(1)),
        ]
    )
    return Case(*clauses, default=Value(2), output_field=IntegerField())


def search_main_customers(
    query: str,
    branch_name: str = "",
    branch_code: str = "",
    limit: int = 20,
) -> list[dict[str, str | int]]:
    cleaned_query = (query or "").strip()
    cleaned_branch_name = (branch_name or "").strip()
    cleaned_branch_code = (branch_code or "").strip()
    if not cleaned_query:
        return []

    results: list[dict[str, str | int]] = []
    seen_customer_codes: set[str] = set()

    base_rows = MainCustomer.objects.filter(is_active_for_scoring=True)
    if cleaned_branch_name or cleaned_branch_code:
        base_rows = (
            base_rows.filter(main_customer_branch_filter(cleaned_branch_name, cleaned_branch_code))
            .annotate(branch_priority=_branch_priority(cleaned_branch_name, cleaned_branch_code))
            .order_by("branch_priority", "-reporting_date", "customer_ref_code", "customer_name", "id")
        )
    else:
        base_rows = base_rows.order_by("-reporting_date", "customer_ref_code", "customer_name", "id")

    candidate_filters = [
        Q(customer_ref_code__iexact=cleaned_query) | Q(customer_name__iexact=cleaned_query),
        Q(customer_ref_code__istartswith=cleaned_query) | Q(customer_name__istartswith=cleaned_query),
    ]
    if len(cleaned_query) >= 4:
        candidate_filters.append(
            Q(customer_ref_code__icontains=cleaned_query) | Q(customer_name__icontains=cleaned_query)
        )

    for filter_q in candidate_filters:
        candidate_limit = max(limit * 2, limit)
        rows = base_rows.filter(filter_q).values(
            "id",
            "customer_ref_code",
            "customer_name",
            "branch_code",
            "branch_name",
        )[:candidate_limit]
        for customer in rows:
            customer_key = "|".join(
                [
                    customer["customer_ref_code"],
                    customer.get("branch_code") or "",
                    customer.get("branch_name") or "",
                ]
            )
            if customer_key in seen_customer_codes:
                continue
            seen_customer_codes.add(customer_key)
            results.append(
                {
                    "id": customer["id"],
                    "customer_ref_code": customer["customer_ref_code"],
                    "customer_name": customer["customer_name"],
                    "branch_code": customer.get("branch_code") or "",
                    "branch_name": customer.get("branch_name") or "",
                    "display": f"{customer['customer_ref_code']} - {customer['customer_name']}",
                }
            )
            if len(results) >= limit:
                return results

    return results


def get_main_customer(branch_name: str, customer_ref_code: str, branch_code: str = "") -> MainCustomer | None:
    cleaned_branch_name = (branch_name or "").strip()
    cleaned_branch_code = (branch_code or "").strip()
    cleaned_customer_ref_code = (customer_ref_code or "").strip()
    if (not cleaned_branch_name and not cleaned_branch_code) or not cleaned_customer_ref_code:
        return None

    return (
        MainCustomer.objects.filter(
            customer_ref_code=cleaned_customer_ref_code,
            is_active_for_scoring=True,
        )
        .filter(main_customer_branch_filter(cleaned_branch_name, cleaned_branch_code))
        .annotate(branch_priority=_branch_priority(cleaned_branch_name, cleaned_branch_code))
        .order_by("branch_priority", "-reporting_date", "-last_synced_at", "-id")
        .first()
    )


def get_main_customer_any_branch(customer_ref_code: str) -> MainCustomer | None:
    cleaned_customer_ref_code = (customer_ref_code or "").strip()
    if not cleaned_customer_ref_code:
        return None

    return (
        MainCustomer.objects.filter(
            customer_ref_code=cleaned_customer_ref_code,
            is_active_for_scoring=True,
        )
        .order_by("-reporting_date", "-last_synced_at", "-id")
        .first()
    )


def _cache_request_branch(request: HttpRequest, branch: BankBranch | None) -> BankBranch | None:
    request._scorecard_current_branch = branch
    if branch is not None:
        request.session["current_branch_id"] = branch.id
        request.session["current_branch_code"] = branch.branch_code
        branch_context = getattr(request, "_scorecard_branch_context", None)
        if isinstance(branch_context, dict):
            branch_context["current_branch"] = branch
    return branch


def resolve_branch_context(
    request: HttpRequest,
    *,
    branch_id: int | str | None = None,
    branch_code: str = "",
    branch_name: str = "",
) -> BankBranch | None:
    """
    Resolve the user's active branch without forcing branch_code to be unique.

    Resolution order:
    1. Explicit branch_id
    2. Session current_branch_id
    3. Explicit branch_code + branch_name
    4. Explicit branch_code
    5. Session current_branch_code
    6. Explicit branch_name
    7. First accessible branch for the user
    """
    cleaned_branch_id = str(branch_id or "").strip()
    cleaned_branch_code = (branch_code or "").strip()
    cleaned_branch_name = (branch_name or "").strip()
    cached_branch = getattr(request, "_scorecard_current_branch", None)
    cached_accessible_branches = getattr(request, "_scorecard_accessible_branches", None)

    if cached_branch is not None:
        cached_branch_id = str(getattr(cached_branch, "id", "") or "").strip()
        cached_branch_code = (cached_branch.branch_code or "").strip()
        cached_branch_name = (cached_branch.branch_name or "").strip()
        if cleaned_branch_id and cleaned_branch_id == cached_branch_id:
            return _cache_request_branch(request, cached_branch)
        if cleaned_branch_code and cleaned_branch_name:
            if cleaned_branch_code == cached_branch_code and cleaned_branch_name.lower() == cached_branch_name.lower():
                return _cache_request_branch(request, cached_branch)
        if cleaned_branch_code and cleaned_branch_code == cached_branch_code:
            return _cache_request_branch(request, cached_branch)
        if cleaned_branch_name and cleaned_branch_name.lower() == cached_branch_name.lower():
            return _cache_request_branch(request, cached_branch)
        if not cleaned_branch_id and not cleaned_branch_code and not cleaned_branch_name:
            return _cache_request_branch(request, cached_branch)

    accessible_branches: list[BankBranch] = []
    if cached_accessible_branches is not None:
        accessible_branches = list(cached_accessible_branches)
    elif not request.user.is_superuser and hasattr(request.user, "get_accessible_branches"):
        accessible_source = request.user.get_accessible_branches()
        if hasattr(accessible_source, "only"):
            accessible_branches = list(
                accessible_source.only("id", "branch_code", "branch_name", "bank_name").order_by("branch_name", "id")
            )
        else:
            accessible_branches = list(accessible_source)
        request._scorecard_accessible_branches = accessible_branches

    accessible_by_id = {
        str(branch.id): branch
        for branch in accessible_branches
        if getattr(branch, "id", None)
    }
    accessible_by_code = {
        (branch.branch_code or "").strip(): branch
        for branch in accessible_branches
        if (branch.branch_code or "").strip()
    }

    candidate_ids: list[str] = []
    if cleaned_branch_id:
        candidate_ids.append(cleaned_branch_id)

    session_branch_id = str(request.session.get("current_branch_id") or "").strip()
    if session_branch_id and session_branch_id not in candidate_ids:
        candidate_ids.append(session_branch_id)

    for candidate_id in candidate_ids:
        branch = accessible_by_id.get(candidate_id)
        if branch is not None:
            return _cache_request_branch(request, branch)
        if request.user.is_superuser:
            branch = (
                BankBranch.objects.only("id", "branch_code", "branch_name", "bank_name")
                .filter(pk=candidate_id)
                .first()
            )
            if branch is not None:
                return _cache_request_branch(request, branch)

    if cleaned_branch_code and cleaned_branch_name:
        lowered_branch_name = cleaned_branch_name.lower()
        for branch in accessible_branches:
            if (branch.branch_code or "").strip() == cleaned_branch_code and (branch.branch_name or "").strip().lower() == lowered_branch_name:
                return _cache_request_branch(request, branch)
        if request.user.is_superuser:
            branch = (
                BankBranch.objects.only("id", "branch_code", "branch_name", "bank_name")
                .filter(branch_code=cleaned_branch_code, branch_name__iexact=cleaned_branch_name)
                .order_by("branch_name", "id")
                .first()
            )
            if branch is not None:
                return _cache_request_branch(request, branch)

    candidate_codes: list[str] = []
    if cleaned_branch_code:
        candidate_codes.append(cleaned_branch_code)

    session_branch_code = (request.session.get("current_branch_code") or "").strip()
    if session_branch_code and session_branch_code not in candidate_codes:
        candidate_codes.append(session_branch_code)

    for candidate_code in candidate_codes:
        branch = accessible_by_code.get(candidate_code)
        if branch is not None:
            return _cache_request_branch(request, branch)
        if request.user.is_superuser:
            branch = (
                BankBranch.objects.only("id", "branch_code", "branch_name", "bank_name")
                .filter(branch_code=candidate_code)
                .order_by("branch_name", "id")
                .first()
            )
            if branch is not None:
                return _cache_request_branch(request, branch)

    if cleaned_branch_name:
        lowered_branch_name = cleaned_branch_name.lower()
        for branch in accessible_branches:
            if (branch.branch_name or "").strip().lower() == lowered_branch_name:
                return _cache_request_branch(request, branch)
        if request.user.is_superuser:
            branch = (
                BankBranch.objects.only("id", "branch_code", "branch_name", "bank_name")
                .filter(branch_name__iexact=cleaned_branch_name)
                .order_by("branch_name", "id")
                .first()
            )
            if branch:
                return _cache_request_branch(request, branch)

    if request.user.is_superuser:
        return None

    fallback_branch = accessible_branches[0] if accessible_branches else None
    return _cache_request_branch(request, fallback_branch)
