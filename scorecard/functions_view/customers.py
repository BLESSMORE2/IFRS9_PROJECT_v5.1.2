import csv
from types import SimpleNamespace
from urllib.parse import urlencode
from django import forms
from django.apps import apps
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.db.models import Count, Max, OuterRef, Q, Subquery
from django.conf import settings
from django.utils import timezone
try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False

try:
    from scorecard.models import CustomerLoan, CustomerOverdraft
except ModuleNotFoundError:
    CustomerLoan = None
    CustomerOverdraft = None
from scorecard.models import BankBranch, CreditEvaluation, IFRS9Evaluation, MainCustomer, ScorecardUserBranchAccess
from scorecard.functions_view.main_customer_lookup import (
    _branch_priority,
    ALL_BRANCHES_SESSION_FLAG,
    build_request_main_customer_branch_filter,
    get_accessible_branches_for_request,
    get_request_branch_names,
    get_request_branch_scope,
    is_all_branches_selected,
    main_customer_branch_filter,
    resolve_branch_context,
)
from scorecard.workflow_approval import get_without_score_list_customer_sources


LIST_PAGE_SIZE_OPTIONS = (20, 50, 100)
STAGE_CUSTOMER_CACHE_TTL_SECONDS = 120
CUSTOMER_LIST_SUMMARY_CACHE_TTL_SECONDS = 120
BRANCH_MASTER_DIAGNOSTICS_CACHE_TTL_SECONDS = 300
CUSTOMER_LIST_SUMMARY_VERSION_CACHE_KEY = "scorecard:customer-summary:version"


def _get_customer_list_summary_version() -> int:
    try:
        return int(cache.get(CUSTOMER_LIST_SUMMARY_VERSION_CACHE_KEY, 1))
    except (TypeError, ValueError):
        return 1


def bump_customer_list_summary_version() -> None:
    try:
        cache.incr(CUSTOMER_LIST_SUMMARY_VERSION_CACHE_KEY)
    except (ValueError, TypeError):
        cache.set(CUSTOMER_LIST_SUMMARY_VERSION_CACHE_KEY, 2, None)


def _ifrs9_stage_models_available() -> bool:
    return CustomerLoan is not None and CustomerOverdraft is not None


def _get_without_score_stage_sources() -> dict[str, bool]:
    sources = get_without_score_list_customer_sources()
    return {
        "include_loans": bool(sources.get("include_loans", True)),
        "include_overdrafts": bool(sources.get("include_overdrafts", True)),
    }


def _stage_customer_source_cache_token(source_settings: dict[str, bool]) -> str:
    return (
        f"loans{int(bool(source_settings.get('include_loans', True)))}_"
        f"overdrafts{int(bool(source_settings.get('include_overdrafts', True)))}"
    )


class BankBranchForm(forms.ModelForm):
    class Meta:
        model = BankBranch
        fields = [
            "branch_code",
            "branch_name",
            "bank_name",
            "address",
            "city",
            "province",
            "zipcode",
            "landline",
            "mobile",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            field.required = field_name in {"branch_code", "branch_name"}

    def _normalized_value(self, field_name: str) -> str:
        return " ".join((self.cleaned_data.get(field_name) or "").strip().split())

    def clean_branch_code(self):
        value = self._normalized_value("branch_code").upper().replace(" ", "")
        if not value:
            raise forms.ValidationError("Branch code is required.")
        return value

    def clean_branch_name(self):
        value = self._normalized_value("branch_name").upper()
        if not value:
            raise forms.ValidationError("Branch name is required.")
        return value

    def clean(self):
        cleaned_data = super().clean()
        branch_code = (cleaned_data.get("branch_code") or "").strip().upper().replace(" ", "")
        branch_name = " ".join((cleaned_data.get("branch_name") or "").strip().upper().split())
        if not branch_code or not branch_name:
            return cleaned_data

        queryset = BankBranch.objects.filter(branch_name__iexact=branch_name)
        if self.instance.pk:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            self.add_error(
                "branch_name",
                f"Branch name '{branch_name}' already exists.",
            )
        return cleaned_data

    def clean_bank_name(self):
        return self._normalized_value("bank_name")

    def clean_address(self):
        return self._normalized_value("address")

    def clean_city(self):
        return self._normalized_value("city")

    def clean_province(self):
        return self._normalized_value("province")

    def clean_zipcode(self):
        return self._normalized_value("zipcode")

    def clean_landline(self):
        return self._normalized_value("landline")

    def clean_mobile(self):
        return self._normalized_value("mobile")


def _normalize_branch_name(value: str | None) -> str:
    return " ".join((value or "").strip().upper().split())


def _normalize_branch_code(value: str | None) -> str:
    return "".join((value or "").strip().upper().split())


def _load_branch_master_from_main_customer() -> tuple[int, int, int]:
    normalized_rows: dict[str, dict[str, str]] = {}
    for item in MainCustomer.objects.values("branch_code", "branch_name", "branch_description").iterator(chunk_size=1000):
        normalized_code = _normalize_branch_code(item.get("branch_code"))
        normalized_name = _normalize_branch_name(item.get("branch_name") or item.get("branch_description"))
        if not normalized_code or not normalized_name or normalized_name == "UNASSIGNED":
            continue
        existing = normalized_rows.get(normalized_name)
        if existing is None or normalized_code < existing["branch_code"]:
            normalized_rows[normalized_name] = {
                "branch_code": normalized_code,
                "branch_name": normalized_name,
            }
    branch_rows = list(normalized_rows.values())
    if not branch_rows:
        return 0, 0, 0

    existing_branches = list(BankBranch.objects.all())
    existing_by_name = {
        _normalize_branch_name(branch.branch_name): branch
        for branch in existing_branches
        if _normalize_branch_name(branch.branch_name)
    }
    branches_to_create: list[BankBranch] = []
    branches_to_update: dict[int, BankBranch] = {}
    skipped = 0
    for branch_row in branch_rows:
        normalized_code = branch_row["branch_code"]
        normalized_name = branch_row["branch_name"]

        existing_branch = existing_by_name.get(normalized_name)
        if existing_branch is not None:
            if _normalize_branch_code(existing_branch.branch_code) != normalized_code:
                existing_branch.branch_code = normalized_code
                if existing_branch.pk:
                    branches_to_update[existing_branch.pk] = existing_branch
            else:
                skipped += 1
            continue

        branch = BankBranch(
            branch_name=normalized_name,
            branch_code=normalized_code,
            bank_name="AFC",
        )
        branches_to_create.append(branch)
        existing_by_name[normalized_name] = branch

    if branches_to_create:
        BankBranch.objects.bulk_create(branches_to_create, batch_size=200)
    if branches_to_update:
        BankBranch.objects.bulk_update(
            [branch for branch in branches_to_update.values() if branch.pk],
            ["branch_code"],
            batch_size=200,
        )
    return len(branches_to_create), len(branches_to_update), skipped


def sync_branch_master_from_rows(branch_rows: list[dict[str, str]]) -> tuple[int, int]:
    if not branch_rows:
        return 0, 0

    normalized_input_rows: dict[str, dict[str, str]] = {}
    for branch_row in branch_rows:
        normalized_code = _normalize_branch_code(branch_row.get("branch_code"))
        normalized_name = _normalize_branch_name(branch_row.get("branch_name"))
        if not normalized_code or not normalized_name or normalized_name == "UNASSIGNED":
            continue
        existing = normalized_input_rows.get(normalized_name)
        if existing is None or normalized_code < existing["branch_code"]:
            normalized_input_rows[normalized_name] = {
                "branch_code": normalized_code,
                "branch_name": normalized_name,
            }

    existing_by_name = {
        _normalize_branch_name(branch.branch_name): branch
        for branch in BankBranch.objects.all()
        if _normalize_branch_name(branch.branch_name)
    }
    branches_to_create: list[BankBranch] = []
    branches_to_update: dict[int, BankBranch] = {}

    for branch_row in normalized_input_rows.values():
        normalized_code = branch_row["branch_code"]
        normalized_name = branch_row["branch_name"]

        existing_branch = existing_by_name.get(normalized_name)
        if existing_branch is not None:
            if _normalize_branch_code(existing_branch.branch_code) != normalized_code:
                existing_branch.branch_code = normalized_code
                if existing_branch.pk:
                    branches_to_update[existing_branch.pk] = existing_branch
            continue

        branch = BankBranch(
            branch_name=normalized_name,
            branch_code=normalized_code,
            bank_name="AFC",
        )
        branches_to_create.append(branch)
        existing_by_name[normalized_name] = branch

    if branches_to_create:
        BankBranch.objects.bulk_create(branches_to_create, batch_size=200)
    if branches_to_update:
        BankBranch.objects.bulk_update(
            [branch for branch in branches_to_update.values() if branch.pk],
            ["branch_code"],
            batch_size=200,
        )
    return len(branches_to_create), len(branches_to_update)


def _get_branch_user_relation_name() -> str | None:
    for relation in BankBranch._meta.related_objects:
        if relation.related_model is ScorecardUserBranchAccess:
            return relation.get_accessor_name()
    return "scorecard_user_access_entries" if hasattr(BankBranch, "scorecard_user_access_entries") else None


def _branch_master_diagnostics_cache_key(sample_limit: int = 20) -> str:
    return f"scorecard:branch_master:diagnostics:{sample_limit}"


def _clear_branch_master_caches() -> None:
    cache.delete(_branch_master_diagnostics_cache_key())


def _get_branch_master_diagnostics(sample_limit: int = 20) -> dict[str, object]:
    cache_key = _branch_master_diagnostics_cache_key(sample_limit)
    cached_value = cache.get(cache_key)
    if cached_value is not None:
        return cached_value

    main_branch_rows: dict[str, dict[str, object]] = {}
    for item in MainCustomer.objects.values("branch_code", "branch_name", "branch_description").iterator(chunk_size=1000):
        normalized_code = _normalize_branch_code(item.get("branch_code"))
        normalized_name = _normalize_branch_name(item.get("branch_name") or item.get("branch_description"))
        if not normalized_code or not normalized_name or normalized_name == "UNASSIGNED":
            continue
        bucket = main_branch_rows.setdefault(
            normalized_name,
            {
                "branch_name": normalized_name,
                "branch_codes": set(),
            },
        )
        bucket["branch_codes"].add(normalized_code)

    branch_master_rows = {
        _normalize_branch_name(branch.branch_name): _normalize_branch_code(branch.branch_code)
        for branch in BankBranch.objects.only("branch_code", "branch_name")
        if _normalize_branch_name(branch.branch_name)
    }

    missing_rows = []
    mismatched_rows = []
    conflicted_rows = []
    for branch_name, row in sorted(main_branch_rows.items()):
        branch_codes = sorted(code for code in row["branch_codes"] if code)
        primary_code = branch_codes[0] if branch_codes else ""
        if len(branch_codes) > 1:
            conflicted_rows.append(
                {
                    "branch_name": branch_name,
                    "branch_codes": branch_codes,
                }
            )
        if branch_name not in branch_master_rows:
            missing_rows.append(
                {
                    "branch_code": primary_code,
                    "branch_name": branch_name,
                }
            )
            continue
        if primary_code and branch_master_rows[branch_name] != primary_code:
            mismatched_rows.append(
                {
                    "branch_code": primary_code,
                    "branch_name": branch_name,
                    "branch_master_branch_code": branch_master_rows[branch_name],
                }
            )

    diagnostics = {
        "main_customer_total": len(main_branch_rows),
        "branch_master_total": len(branch_master_rows),
        "missing_total": len(missing_rows),
        "mismatched_total": len(mismatched_rows),
        "conflicted_total": len(conflicted_rows),
        "missing_sample": missing_rows[:sample_limit],
        "mismatched_sample": mismatched_rows[:sample_limit],
        "conflicted_sample": conflicted_rows[:sample_limit],
    }
    cache.set(cache_key, diagnostics, BRANCH_MASTER_DIAGNOSTICS_CACHE_TTL_SECONDS)
    return diagnostics


def _get_current_branch(request):
    current_branch = resolve_branch_context(request)
    if current_branch is None:
        return None, None
    return current_branch.branch_code, current_branch


@login_required
def switch_branch_view(request: HttpRequest) -> JsonResponse:
    if not request.user.has_perm("scorecard.access_scorecard_dashboard"):
        if request.method == "POST":
            return JsonResponse({"success": False, "error": "You do not have permission to switch branches."}, status=403)
        raise PermissionDenied("You do not have permission to switch branches.")

    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST request required."}, status=405)

    branch_id = (request.POST.get("branch_id") or "").strip()
    branch_code = (request.POST.get("branch_code") or "").strip()
    branch_name = " ".join((request.POST.get("branch_name") or "").strip().split())
    all_branches = (request.POST.get("all_branches") or "").strip().lower() in {"1", "true", "yes", "on"}

    if all_branches:
        accessible_branches = get_accessible_branches_for_request(request)
        accessible_count = len(accessible_branches)
        if accessible_count <= 1:
            return JsonResponse({"success": False, "error": "All branches mode needs more than one assigned branch."}, status=400)

        request.session[ALL_BRANCHES_SESSION_FLAG] = True
        request._scorecard_all_branches_selected = True
        branch_context = getattr(request, "_scorecard_branch_context", None)
        if isinstance(branch_context, dict):
            branch_context["all_branches_selected"] = True
            branch_context["current_branch_label"] = "ALL ASSIGNED BRANCHES"
        return JsonResponse(
            {
                "success": True,
                "all_branches": True,
                "branch_name": "ALL ASSIGNED BRANCHES",
                "bank_name": "",
            }
        )

    if not branch_id and not branch_code:
        return JsonResponse({"success": False, "error": "Branch selection is required."}, status=400)

    if hasattr(request.user, "get_accessible_branches"):
        accessible_branches = request.user.get_accessible_branches()
    else:
        accessible_branches = BankBranch.objects.all()

    branch = None
    if branch_id:
        branch = accessible_branches.filter(pk=branch_id).first()
        if branch is None and request.user.is_superuser:
            branch = BankBranch.objects.filter(pk=branch_id).first()
    if branch is None and branch_code and branch_name:
        branch = accessible_branches.filter(branch_code=branch_code, branch_name__iexact=branch_name).first()
        if branch is None and request.user.is_superuser:
            branch = BankBranch.objects.filter(branch_code=branch_code, branch_name__iexact=branch_name).first()
    if branch is None and branch_code:
        branch = accessible_branches.filter(branch_code=branch_code).order_by("branch_name", "id").first()
        if branch is None and request.user.is_superuser:
            branch = BankBranch.objects.filter(branch_code=branch_code).order_by("branch_name", "id").first()

    if branch is None or (not request.user.is_superuser and not request.user.has_branch_access(branch_id=branch.id)):
        return JsonResponse({"success": False, "error": "You do not have access to that branch."}, status=403)

    request.session[ALL_BRANCHES_SESSION_FLAG] = False
    request._scorecard_all_branches_selected = False
    request.session["current_branch_id"] = branch.id
    request.session["current_branch_code"] = branch.branch_code
    return JsonResponse(
        {
            "success": True,
            "branch_id": branch.id,
            "branch_code": branch.branch_code,
            "branch_name": branch.branch_name,
            "bank_name": branch.bank_name,
        }
    )


def _empty_customer_queryset():
    return MainCustomer.objects.none()


def _get_customer_profile_status(customer: MainCustomer) -> str:
    if not (customer.customer_name or "").strip():
        return "Needs Attention"
    if (customer.customer_name or "").strip() == (customer.customer_ref_code or "").strip():
        return "Needs Attention"
    if not (customer.customer_type or "").strip():
        return "Needs Attention"
    return "Complete"


def _get_customer_export_headers() -> list[str]:
    return [
        "Customer Code",
        "Customer Name",
        "Customer Type",
        "Branch",
        "Reporting Date",
        "Mobile",
        "Email",
        "National ID",
        "Resident Status",
        "Nationality",
        "Occupation",
        "Employer Name",
        "Has Loan",
        "Has Overdraft",
        "Loan Count",
        "Overdraft Count",
        "Primary Loan ID",
        "Primary Account Number",
        "Profile Status",
    ]


def _get_customer_export_row(customer: MainCustomer) -> list:
    return [
        customer.customer_ref_code,
        customer.customer_name,
        customer.customer_type or "",
        customer.branch_name or customer.branch_description or "",
        customer.reporting_date.isoformat() if customer.reporting_date else "",
        customer.mobile or "",
        customer.email or "",
        customer.national_id or "",
        customer.resident_status or "",
        customer.nationality_code or "",
        customer.occupation_code or "",
        customer.employer_name or "",
        "Yes" if customer.has_loan else "No",
        "Yes" if customer.has_overdraft else "No",
        customer.loan_count,
        customer.overdraft_count,
        customer.primary_loan_id or "",
        customer.primary_account_number or "",
        _get_customer_profile_status(customer),
    ]


def _get_filtered_main_customers(request):
    """Helper function to get filtered main customers based on branch access."""
    branch_scope = get_request_branch_scope(request)
    if not branch_scope:
        return _empty_customer_queryset()

    customers = (
        MainCustomer.objects.filter(is_active_for_scoring=True)
        .filter(build_request_main_customer_branch_filter(request))
        .only(
            "id",
            "customer_ref_code",
            "customer_name",
            "customer_type",
            "branch_name",
            "branch_code",
            "branch_description",
            "reporting_date",
            "email",
            "mobile",
            "national_id",
            "resident_status",
            "nationality_code",
            "occupation_code",
            "employer_name",
            "has_loan",
            "has_overdraft",
            "loan_count",
            "overdraft_count",
            "primary_loan_id",
            "primary_account_number",
        )
        .order_by("-reporting_date", "customer_name", "customer_ref_code")
    )

    return customers


def _normalize_list_page_size(raw_value, default: int = 20) -> int:
    try:
        page_size = int(raw_value)
    except (TypeError, ValueError):
        return default
    return page_size if page_size in LIST_PAGE_SIZE_OPTIONS else default


def _build_list_query_string(*, search_query: str, page_size: int) -> str:
    params = {}
    if search_query:
        params["q"] = search_query
    if page_size != LIST_PAGE_SIZE_OPTIONS[0]:
        params["page_size"] = page_size
    return urlencode(params)


def _apply_customer_search(queryset, search_query: str):
    cleaned_query = (search_query or "").strip()
    if not cleaned_query:
        return queryset
    return queryset.filter(
        Q(customer_ref_code__icontains=cleaned_query)
        | Q(customer_name__icontains=cleaned_query)
        | Q(customer_type__icontains=cleaned_query)
        | Q(branch_name__icontains=cleaned_query)
        | Q(branch_code__icontains=cleaned_query)
        | Q(email__icontains=cleaned_query)
        | Q(mobile__icontains=cleaned_query)
        | Q(national_id__icontains=cleaned_query)
    )


def _apply_customer_search_to_collection(customers, search_query: str):
    cleaned_query = (search_query or "").strip().lower()
    if not cleaned_query:
        return list(customers)

    def _matches(customer) -> bool:
        haystack = " ".join(
            str(value or "").lower()
            for value in (
                getattr(customer, "customer_ref_code", ""),
                getattr(customer, "customer_name", ""),
                getattr(customer, "customer_type", ""),
                getattr(customer, "branch_name", ""),
                getattr(customer, "branch_code", ""),
                getattr(customer, "email", ""),
                getattr(customer, "mobile", ""),
                getattr(customer, "national_id", ""),
            )
        )
        return cleaned_query in haystack

    return [customer for customer in customers if _matches(customer)]


def _paginate_customer_queryset(request: HttpRequest, queryset, *, default_page_size: int = 20):
    search_query = (request.GET.get("q") or "").strip()
    page_size = _normalize_list_page_size(request.GET.get("page_size"), default=default_page_size)
    paginator = Paginator(queryset, page_size)
    page_obj = paginator.get_page(request.GET.get("p") or "1")
    return page_obj, search_query, page_size


def _count_collection(customers) -> int:
    if hasattr(customers, "model") and hasattr(customers, "query"):
        return customers.count()

    count_method = getattr(customers, "count", None)
    if callable(count_method) and not isinstance(customers, list):
        return count_method()
    return len(customers)


def _customer_summary_cache_key(current_branch, suffix: str, reporting_date=None) -> str:
    branch_name = _normalize_branch_name(getattr(current_branch, "branch_name", "")) or "NONE"
    branch_code = _normalize_branch_code(getattr(current_branch, "branch_code", "")) or "NONE"
    reporting_part = getattr(reporting_date, "isoformat", lambda: str(reporting_date or "NONE"))()
    return f"scorecard:customer_summary:{branch_code}:{branch_name}:{reporting_part}:{suffix}"


def _customer_summary_scope_cache_key(branch_scope, suffix: str, reporting_date=None) -> str:
    if not branch_scope:
        scope_key = "NONE"
    else:
        scope_key = "|".join(
            sorted(
                f"{_normalize_branch_code(getattr(branch, 'branch_code', ''))}:{_normalize_branch_name(getattr(branch, 'branch_name', ''))}"
                for branch in branch_scope
            )
        )
    reporting_part = getattr(reporting_date, "isoformat", lambda: str(reporting_date or "NONE"))()
    return (
        f"scorecard:customer_summary_scope:{_get_customer_list_summary_version()}:"
        f"{scope_key}:{reporting_part}:{suffix}"
    )


def _get_branch_scored_customer_keys(branch_scope, reporting_date, evaluation_model, suffix: str) -> set[tuple[str, str]]:
    """
    Return scored customer keys by scoring branch.

    The customer gap lists are contract/loan-branch driven, so the exclusion must
    use both customer code and branch. A customer scored in Bindura should not
    remove that same customer from the 8TH AVENUE missing-score list.
    """
    cache_key = _customer_summary_scope_cache_key(branch_scope, suffix, reporting_date)
    cached_keys = cache.get(cache_key)
    if cached_keys is not None:
        return {tuple(item) for item in cached_keys}

    branch_names = [
        (branch.branch_name or "").strip()
        for branch in branch_scope
        if (branch.branch_name or "").strip()
    ]
    scored_keys = {
        (str(customer_id).strip(), _normalize_branch_name(branch_name))
        for customer_id, branch_name in evaluation_model.objects.filter(branch_name__in=branch_names)
        .exclude(customer_id__isnull=True)
        .exclude(customer_id__exact="")
        .values_list("customer_id", "branch_name")
        .distinct()
        if str(customer_id or "").strip() and _normalize_branch_name(branch_name)
    }
    cache.set(cache_key, sorted(scored_keys), CUSTOMER_LIST_SUMMARY_CACHE_TTL_SECONDS)
    return scored_keys


def _get_scored_customer_codes_any_branch(evaluation_model, suffix: str) -> set[str]:
    """
    Return customers already scored anywhere.

    The customer gap lists are still loan/contract-branch driven, but once a
    customer has any Basel/IFRS9 score in any branch they should no longer show
    as "without" that score type.
    """
    cache_key = (
        f"scorecard:customer_summary:any_branch:"
        f"{_get_customer_list_summary_version()}:{suffix}"
    )
    cached_codes = cache.get(cache_key)
    if cached_codes is not None:
        return set(cached_codes)

    scored_codes = {
        str(value).strip()
        for value in evaluation_model.objects.exclude(customer_id__isnull=True)
        .exclude(customer_id__exact="")
        .values_list("customer_id", flat=True)
        .distinct()
        if str(value or "").strip()
    }
    cache.set(cache_key, sorted(scored_codes), CUSTOMER_LIST_SUMMARY_CACHE_TTL_SECONDS)
    return scored_codes


def _get_branch_scored_customer_codes(branch_scope, reporting_date, evaluation_model, suffix: str) -> set[str]:
    return _get_scored_customer_codes_any_branch(evaluation_model, suffix)


def _get_customer_list_summary_counts(request: HttpRequest) -> dict[str, int]:
    branch_scope = get_request_branch_scope(request)
    if not branch_scope:
        return {
            "customer_total": 0,
            "without_basel_total": 0,
            "without_ifrs9_total": 0,
        }

    source_settings = _get_without_score_stage_sources()
    snapshot = _get_stage_customer_population_snapshot(branch_scope, source_settings)
    reporting_date = snapshot.get("reporting_date")
    source_cache_token = _stage_customer_source_cache_token(source_settings)
    cache_key = _customer_summary_scope_cache_key(branch_scope, f"all_customers:{source_cache_token}", reporting_date)
    cached_counts = cache.get(cache_key)
    if cached_counts is not None:
        return cached_counts

    stage_rows = snapshot.get("rows") or []
    basel_scored_codes = _get_scored_customer_codes_any_branch(
        CreditEvaluation,
        "basel_scored_codes_any_branch",
    )
    ifrs9_scored_codes = _get_scored_customer_codes_any_branch(
        IFRS9Evaluation,
        "ifrs9_scored_codes_any_branch",
    )

    counts = {
        "customer_total": _get_filtered_main_customers(request).count(),
        "without_basel_total": sum(
            1
            for row in stage_rows
            if str(row.get("customer_ref_code") or "").strip() not in basel_scored_codes
        ),
        "without_ifrs9_total": sum(
            1
            for row in stage_rows
            if str(row.get("customer_ref_code") or "").strip() not in ifrs9_scored_codes
        ),
    }
    cache.set(cache_key, counts, CUSTOMER_LIST_SUMMARY_CACHE_TTL_SECONDS)
    return counts


def build_without_score_customer_snapshot_for_branch_scope(
    branch_scope,
    source_settings: dict[str, bool] | None = None,
) -> dict[str, object]:
    """
    Return the same staged missing-score population used by the customer lists.

    The missing lists are contract-source driven, and scoring exclusion is based
    on whether the customer has already been scored anywhere for that score type.
    """
    if not branch_scope:
        return {
            "reporting_date": None,
            "source_settings": source_settings or _get_without_score_stage_sources(),
            "basel_rows": [],
            "ifrs9_rows": [],
            "total_stage_rows": 0,
        }

    source_settings = source_settings or _get_without_score_stage_sources()
    snapshot = _get_stage_customer_population_snapshot(branch_scope, source_settings)
    stage_rows = snapshot.get("rows") or []
    basel_scored_codes = _get_scored_customer_codes_any_branch(
        CreditEvaluation,
        "basel_scored_codes_any_branch",
    )
    ifrs9_scored_codes = _get_scored_customer_codes_any_branch(
        IFRS9Evaluation,
        "ifrs9_scored_codes_any_branch",
    )

    return {
        "reporting_date": snapshot.get("reporting_date"),
        "source_settings": source_settings,
        "basel_rows": [
            row
            for row in stage_rows
            if str(row.get("customer_ref_code") or "").strip() not in basel_scored_codes
        ],
        "ifrs9_rows": [
            row
            for row in stage_rows
            if str(row.get("customer_ref_code") or "").strip() not in ifrs9_scored_codes
        ],
        "total_stage_rows": len(stage_rows),
    }


def _get_manual_customer_reporting_date(branch_name: str | None = None, branch_code: str | None = None):
    branch_name = (branch_name or "").strip()
    branch_code = (branch_code or "").strip()
    queryset = MainCustomer.objects.all()
    if branch_name or branch_code:
        branch_date = (
            queryset.filter(main_customer_branch_filter(branch_name, branch_code))
            .order_by("-reporting_date")
            .values_list("reporting_date", flat=True)
            .first()
        )
        if branch_date:
            return branch_date
    latest_date = queryset.order_by("-reporting_date").values_list("reporting_date", flat=True).first()
    return latest_date or timezone.localdate()


def _get_branch_scoped_customer_or_404(request: HttpRequest, customer_id: int) -> MainCustomer:
    customer = _get_filtered_main_customers(request).filter(pk=customer_id).first()
    if customer is None:
        raise Http404("Customer not found for the active branch.")
    return customer


@login_required
def customer_list_view(request: HttpRequest) -> HttpResponse:
    """
    View to list all customers.
    Filters by current branch for both regular users and admins.
    """
    base_customers = _get_filtered_main_customers(request)
    search_query = (request.GET.get("q") or "").strip()
    filtered_customers = _apply_customer_search(base_customers, search_query)
    page_obj, search_query, page_size = _paginate_customer_queryset(request, filtered_customers)

    summary_counts = _get_customer_list_summary_counts(request)
    context = {
        "customers": page_obj.object_list,
        "page_obj": page_obj,
        "customer_total": summary_counts["customer_total"],
        "without_basel_total": summary_counts["without_basel_total"],
        "without_ifrs9_total": summary_counts["without_ifrs9_total"],
        "search_query": search_query,
        "page_size": page_size,
        "list_query_string": _build_list_query_string(
            search_query=search_query,
            page_size=page_size,
        ),
    }

    return render(
        request,
        "credit_scoreshifts/customers/customer_list.html",
        context,
    )


@login_required
def customer_detail_view(request: HttpRequest, customer_id: int) -> HttpResponse:
    customer = _get_branch_scoped_customer_or_404(request, customer_id)
    context = {
        "customer": customer,
        "profile_status": _get_customer_profile_status(customer),
    }
    return render(
        request,
        "credit_scoreshifts/customers/_customer_detail_modal.html",
        context,
    )


def _get_filtered_customers(request):
    """Backward-compatible helper now reading from the main synced customer table."""
    return _get_filtered_main_customers(request)


def _build_stage_branch_filter(branch_name: str | None, branch_code: str | None) -> Q:
    normalized_branch_name = _normalize_branch_name(branch_name)
    normalized_branch_code = _normalize_branch_code(branch_code)
    if normalized_branch_name and normalized_branch_code:
        return Q(branch_description__iexact=normalized_branch_name) | (
            (Q(branch_description__isnull=True) | Q(branch_description__exact=""))
        )
    if normalized_branch_name:
        return Q(branch_description__iexact=normalized_branch_name)

    return Q(pk__isnull=True)


def _build_stage_scope_filter(branch_scope) -> Q:
    if not branch_scope:
        return Q(pk__isnull=True)
    scope_filter = Q(pk__isnull=True)
    for branch in branch_scope:
        scope_filter |= _build_stage_branch_filter(branch.branch_name, branch.branch_code)
    return scope_filter


def _stage_branch_cache_key(branch_scope, suffix: str, source_settings: dict[str, bool] | None = None) -> str:
    if not branch_scope:
        scope_key = "NONE"
    else:
        scope_key = "|".join(
            sorted(
                f"{_normalize_branch_code(getattr(branch, 'branch_code', ''))}:{_normalize_branch_name(getattr(branch, 'branch_name', ''))}"
                for branch in branch_scope
            )
        )
    source_key = _stage_customer_source_cache_token(source_settings or _get_without_score_stage_sources())
    return f"scorecard:stage_customers:{scope_key}:{source_key}:{suffix}"


def _get_latest_stage_reporting_date(branch_scope, source_settings: dict[str, bool] | None = None) -> object | None:
    source_settings = source_settings or _get_without_score_stage_sources()
    cache_key = _stage_branch_cache_key(branch_scope, "latest_reporting_date", source_settings)
    cached_value = cache.get(cache_key)
    if cached_value is not None:
        return cached_value or None
    if not _ifrs9_stage_models_available():
        cache.set(cache_key, "", STAGE_CUSTOMER_CACHE_TTL_SECONDS)
        return None

    if not source_settings["include_loans"] and not source_settings["include_overdrafts"]:
        cache.set(cache_key, "", STAGE_CUSTOMER_CACHE_TTL_SECONDS)
        return None

    branch_filter = _build_stage_scope_filter(branch_scope)
    available_dates = []
    if source_settings["include_loans"]:
        loan_date = CustomerLoan.objects.filter(branch_filter).aggregate(max_date=Max("reporting_date"))["max_date"]
        if loan_date is not None:
            available_dates.append(loan_date)
    if source_settings["include_overdrafts"]:
        overdraft_date = CustomerOverdraft.objects.filter(branch_filter).aggregate(max_date=Max("reporting_date"))["max_date"]
        if overdraft_date is not None:
            available_dates.append(overdraft_date)
    latest_date = max(available_dates) if available_dates else None
    cache.set(cache_key, latest_date or "", STAGE_CUSTOMER_CACHE_TTL_SECONDS)
    return latest_date


def _get_stage_customer_population_snapshot(branch_scope, source_settings: dict[str, bool] | None = None) -> dict[str, object]:
    source_settings = source_settings or _get_without_score_stage_sources()
    cache_key = _stage_branch_cache_key(branch_scope, "population_snapshot", source_settings)
    cached_snapshot = cache.get(cache_key)
    if cached_snapshot is not None:
        return cached_snapshot

    reporting_date = _get_latest_stage_reporting_date(branch_scope, source_settings)
    if reporting_date is None:
        snapshot = {"reporting_date": None, "rows": []}
        cache.set(cache_key, snapshot, STAGE_CUSTOMER_CACHE_TTL_SECONDS)
        return snapshot

    branch_filter = _build_stage_scope_filter(branch_scope)
    staged_customers: dict[str, dict[str, object]] = {}
    primary_scope_branch = branch_scope[0] if branch_scope else None
    default_branch_code = getattr(primary_scope_branch, "branch_code", "") or ""
    default_branch_name = getattr(primary_scope_branch, "branch_name", "") or ""

    def _merge_group_rows(rows, *, has_loan: bool) -> None:
        for row in rows:
            customer_code = str(row.get("customer_code") or "").strip()
            if not customer_code:
                continue
            row_branch_code = _normalize_branch_code(row.get("branch_code")) or default_branch_code
            row_branch_name = _normalize_branch_name(
                row.get("branch_description") or row.get("branch_name")
            ) or default_branch_name
            customer_key = (customer_code, row_branch_code, row_branch_name)
            bucket = staged_customers.setdefault(
                customer_key,
                {
                    "customer_ref_code": customer_code,
                    "customer_name": (row.get("customer_name") or "").strip(),
                    "branch_code": row_branch_code,
                    "branch_name": row_branch_name,
                    "reporting_date": reporting_date,
                    "has_loan": False,
                    "has_overdraft": False,
                    "loan_count": 0,
                    "overdraft_count": 0,
                    "main_customer_id": None,
                    "customer_type": "",
                    "email": "",
                    "mobile": "",
                    "national_id": "",
                    "resident_status": "",
                    "nationality_code": "",
                    "occupation_code": "",
                    "employer_name": "",
                    "primary_loan_id": "",
                    "primary_account_number": "",
                    "branch_description": "",
                },
            )
            if not bucket["customer_name"]:
                bucket["customer_name"] = (row.get("customer_name") or "").strip()
            if has_loan:
                bucket["has_loan"] = True
                bucket["loan_count"] += int(row.get("loan_count") or 0)
            else:
                bucket["has_overdraft"] = True
                bucket["overdraft_count"] += int(row.get("overdraft_count") or 0)

    if source_settings["include_loans"]:
        loan_groups = (
            CustomerLoan.objects.filter(reporting_date=reporting_date)
            .filter(branch_filter)
            .exclude(customer_code__isnull=True)
            .exclude(customer_code__exact="")
            .values("customer_code", "customer_name", "branch_description")
            .annotate(loan_count=Count("id"))
        )
        _merge_group_rows(loan_groups, has_loan=True)

    if source_settings["include_overdrafts"]:
        overdraft_groups = (
            CustomerOverdraft.objects.filter(reporting_date=reporting_date)
            .filter(branch_filter)
            .exclude(customer_code__isnull=True)
            .exclude(customer_code__exact="")
            .values("customer_code", "customer_name", "branch_description")
            .annotate(overdraft_count=Count("id"))
        )
        _merge_group_rows(overdraft_groups, has_loan=False)

    if staged_customers:
        preferred_main_customers: dict[tuple[str, str, str], MainCustomer] = {}
        main_customer_scope_filter = Q(pk__in=[])
        for branch in branch_scope:
            main_customer_scope_filter |= main_customer_branch_filter(branch.branch_name, branch.branch_code)
        main_customer_rows = (
            MainCustomer.objects.filter(
                customer_ref_code__in=[key[0] for key in staged_customers.keys()],
                is_active_for_scoring=True,
            )
            .filter(main_customer_scope_filter)
            .only(
                "id",
                "customer_ref_code",
                "customer_name",
                "customer_type",
                "branch_name",
                "branch_code",
                "branch_description",
                "email",
                "mobile",
                "national_id",
                "resident_status",
                "nationality_code",
                "occupation_code",
                "employer_name",
                "primary_loan_id",
                "primary_account_number",
                "reporting_date",
            )
            .order_by("customer_ref_code", "-reporting_date", "-id")
        )
        for customer in main_customer_rows:
            customer_key = (
                customer.customer_ref_code,
                _normalize_branch_code(customer.branch_code),
                _normalize_branch_name(customer.branch_name or customer.branch_description),
            )
            preferred_main_customers.setdefault(customer_key, customer)

        for customer_key, bucket in staged_customers.items():
            main_customer = preferred_main_customers.get(customer_key)
            if main_customer is None:
                continue
            bucket.update(
                {
                    "main_customer_id": main_customer.id,
                    "customer_name": main_customer.customer_name or bucket["customer_name"] or bucket["customer_ref_code"],
                    "customer_type": main_customer.customer_type or "",
                    "branch_name": bucket["branch_name"] or main_customer.branch_name or main_customer.branch_description or default_branch_name,
                    "branch_code": bucket["branch_code"] or main_customer.branch_code or default_branch_code,
                    "branch_description": main_customer.branch_description or main_customer.branch_name or bucket["branch_name"] or "",
                    "email": main_customer.email or "",
                    "mobile": main_customer.mobile or "",
                    "national_id": main_customer.national_id or "",
                    "resident_status": main_customer.resident_status or "",
                    "nationality_code": getattr(main_customer, "nationality_code", "") or "",
                    "occupation_code": getattr(main_customer, "occupation_code", "") or "",
                    "employer_name": getattr(main_customer, "employer_name", "") or "",
                    "primary_loan_id": getattr(main_customer, "primary_loan_id", "") or "",
                    "primary_account_number": getattr(main_customer, "primary_account_number", "") or "",
                }
            )

    rows = sorted(
        staged_customers.values(),
        key=lambda item: (((item.get("customer_name") or "").upper()), item.get("customer_ref_code") or ""),
    )
    snapshot = {"reporting_date": reporting_date, "rows": rows}
    cache.set(cache_key, snapshot, STAGE_CUSTOMER_CACHE_TTL_SECONDS)
    return snapshot


def get_active_exposure_customer_codes_for_branch_scope(
    branch_scope,
    source_settings: dict[str, bool] | None = None,
    *,
    exposure_type: str = "active",
) -> set[str]:
    """
    Return customer codes with active loan/overdraft exposure in the selected branch scope.

    The staged snapshot already uses the latest available reporting date for the
    requested CustomerLoan/CustomerOverdraft source set.
    """
    exposure_type = (exposure_type or "active").strip().lower()
    snapshot = _get_stage_customer_population_snapshot(branch_scope, source_settings)
    rows = snapshot.get("rows", []) if isinstance(snapshot, dict) else []
    customer_codes: set[str] = set()
    for row in rows:
        customer_code = str(row.get("customer_ref_code") or "").strip()
        has_loan = bool(row.get("has_loan"))
        has_overdraft = bool(row.get("has_overdraft"))
        include_customer = (
            (exposure_type == "loan" and has_loan)
            or (exposure_type == "overdraft" and has_overdraft)
            or (exposure_type == "active" and (has_loan or has_overdraft))
        )
        if customer_code and include_customer:
            customer_codes.add(customer_code)
    return customer_codes


def _get_stage_backed_main_customers(request):
    """
    Return branch-scoped MainCustomer rows for customers that are present in the
    current branch's staging loans or overdrafts for the latest staging date.
    """
    branch_scope = get_request_branch_scope(request)
    customers = _get_filtered_main_customers(request)
    if not branch_scope:
        return customers

    source_settings = _get_without_score_stage_sources()
    reporting_date = _get_latest_stage_reporting_date(branch_scope, source_settings)
    if reporting_date is None:
        return _empty_customer_queryset()

    branch_filter = _build_stage_scope_filter(branch_scope)
    exposure_code_filter = Q(pk__in=[])
    if source_settings["include_loans"]:
        loan_customer_codes = (
            CustomerLoan.objects.filter(reporting_date=reporting_date)
            .filter(branch_filter)
            .exclude(customer_code__isnull=True)
            .exclude(customer_code__exact="")
            .values_list("customer_code", flat=True)
            .distinct()
        )
        exposure_code_filter |= Q(customer_ref_code__in=loan_customer_codes)
    if source_settings["include_overdrafts"]:
        overdraft_customer_codes = (
            CustomerOverdraft.objects.filter(reporting_date=reporting_date)
            .filter(branch_filter)
            .exclude(customer_code__isnull=True)
            .exclude(customer_code__exact="")
            .values_list("customer_code", flat=True)
            .distinct()
        )
        exposure_code_filter |= Q(customer_ref_code__in=overdraft_customer_codes)

    latest_customer_pk = (
        MainCustomer.objects.filter(
            is_active_for_scoring=True,
            customer_ref_code=OuterRef("customer_ref_code"),
        )
        .filter(build_request_main_customer_branch_filter(request))
        .order_by("-reporting_date", "-id")
        .values("pk")[:1]
    )

    return (
        customers.filter(exposure_code_filter)
        .filter(pk=Subquery(latest_customer_pk))
        .order_by("-reporting_date", "customer_name", "customer_ref_code")
    )


def _build_stage_backed_customer_records(request, *, scored_customer_ids=None, scored_customer_keys=None):
    branch_scope = get_request_branch_scope(request)
    if not branch_scope:
        return []

    snapshot = _get_stage_customer_population_snapshot(branch_scope)
    staged_rows = snapshot.get("rows") or []
    if not staged_rows:
        return []

    scored_customer_codes = {
        str(value).strip()
        for value in (scored_customer_ids or [])
        if str(value or "").strip()
    }
    scored_keys = {
        (str(customer_code).strip(), _normalize_branch_name(branch_name))
        for customer_code, branch_name in (scored_customer_keys or [])
        if str(customer_code or "").strip() and _normalize_branch_name(branch_name)
    }

    results = []
    for staged in staged_rows:
        customer_code = str(staged.get("customer_ref_code") or "").strip()
        stage_key = (customer_code, _normalize_branch_name(staged.get("branch_name")))
        if not customer_code or customer_code in scored_customer_codes or stage_key in scored_keys:
            continue
        results.append(
            SimpleNamespace(
                id=staged.get("main_customer_id"),
                customer_ref_code=customer_code,
                customer_name=staged.get("customer_name") or customer_code,
                customer_type=staged.get("customer_type") or "",
                branch_name=staged.get("branch_name") or "",
                branch_code=staged.get("branch_code") or "",
                branch_description=staged.get("branch_description") or staged.get("branch_name") or "",
                reporting_date=staged.get("reporting_date"),
                email=staged.get("email") or "",
                mobile=staged.get("mobile") or "",
                national_id=staged.get("national_id") or "",
                resident_status=staged.get("resident_status") or "",
                nationality_code=staged.get("nationality_code") or "",
                occupation_code=staged.get("occupation_code") or "",
                employer_name=staged.get("employer_name") or "",
                has_loan=bool(staged.get("has_loan")),
                has_overdraft=bool(staged.get("has_overdraft")),
                loan_count=int(staged.get("loan_count") or 0),
                overdraft_count=int(staged.get("overdraft_count") or 0),
                primary_loan_id=staged.get("primary_loan_id") or "",
                primary_account_number=staged.get("primary_account_number") or "",
            )
        )

    return results


@login_required
def customer_export_csv(request: HttpRequest) -> HttpResponse:
    """Export customers to CSV format."""
    page = request.GET.get('page', 'current')  # 'current' or 'all'
    customers = _get_filtered_customers(request)
    
    # Create CSV response
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="customers_export_{page}.csv"'
    
    writer = csv.writer(response)
    
    # Write header
    writer.writerow(_get_customer_export_headers())
    
    # Write data rows
    for customer in customers:
        writer.writerow(_get_customer_export_row(customer))
    
    return response


@login_required
def customer_export_excel(request: HttpRequest) -> HttpResponse:
    """Export customers to Excel format."""
    if not OPENPYXL_AVAILABLE:
        return HttpResponse("Excel export requires openpyxl library. Please install it.", status=500)
    
    page = request.GET.get('page', 'current')  # 'current' or 'all'
    customers = _get_filtered_customers(request)
    
    # Create workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Customers"
    
    # Define headers
    headers = _get_customer_export_headers()
    
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
    for row_num, customer in enumerate(customers, 2):
        for col_num, value in enumerate(_get_customer_export_row(customer), 1):
            ws.cell(row=row_num, column=col_num, value=value)
    
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
    response['Content-Disposition'] = f'attachment; filename="customers_export_{page}.xlsx"'
    
    wb.save(response)
    return response


def _get_filtered_customers_without_questionnaires(request):
    """Stage-backed customers in the current branch with no Basel evaluations in that same branch."""
    branch_scope = get_request_branch_scope(request)
    if not branch_scope:
        return []
    evaluated_customer_ids = _get_scored_customer_codes_any_branch(
        CreditEvaluation,
        "basel_scored_codes_any_branch",
    )
    return _build_stage_backed_customer_records(request, scored_customer_ids=evaluated_customer_ids)


@login_required
def customer_without_questionnaire_list_view(request: HttpRequest) -> HttpResponse:
    """
    View to list customers who don't have any questionnaires/evaluations.
    Filters by current branch for both regular users and admins.
    """
    base_customers = _get_filtered_customers_without_questionnaires(request)
    search_query = (request.GET.get("q") or "").strip()
    filtered_customers = _apply_customer_search_to_collection(base_customers, search_query)
    page_obj, search_query, page_size = _paginate_customer_queryset(request, filtered_customers)

    context = {
        "customers": page_obj.object_list,
        "page_obj": page_obj,
        "customer_total": _count_collection(base_customers),
        "without_basel_total": None,
        "without_ifrs9_total": None,
        "search_query": search_query,
        "page_size": page_size,
        "list_query_string": _build_list_query_string(
            search_query=search_query,
            page_size=page_size,
        ),
    }

    return render(
        request,
        "credit_scoreshifts/customers/customer_list.html",
        context,
    )


@login_required
def customer_without_questionnaire_export_csv(request: HttpRequest) -> HttpResponse:
    """Export customers without Basell II Scores to CSV format."""
    page = request.GET.get('page', 'current')  # 'current' or 'all'
    customers = _get_filtered_customers_without_questionnaires(request)
    
    # Create CSV response
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="customers_without_basel_II_export_{page}.csv"'
    
    writer = csv.writer(response)
    
    # Write header
    writer.writerow(_get_customer_export_headers())
    
    # Write data rows
    for customer in customers:
        writer.writerow(_get_customer_export_row(customer))
    
    return response


@login_required
def customer_without_questionnaire_export_excel(request: HttpRequest) -> HttpResponse:
    """Export customers without Basel II Scores to Excel format."""
    if not OPENPYXL_AVAILABLE:
        return HttpResponse("Excel export requires openpyxl library. Please install it.", status=500)
    
    page = request.GET.get('page', 'current')  # 'current' or 'all'
    customers = _get_filtered_customers_without_questionnaires(request)
    
    # Create workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Customers Without Basel II Scores"
    
    # Define headers
    headers = _get_customer_export_headers()
    
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
    for row_num, customer in enumerate(customers, 2):
        for col_num, value in enumerate(_get_customer_export_row(customer), 1):
            ws.cell(row=row_num, column=col_num, value=value)
    
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
    response['Content-Disposition'] = f'attachment; filename="customers_without_basel_II_export_{page}.xlsx"'
    
    wb.save(response)
    return response


def _get_filtered_customers_without_ifrs9(request):
    """Stage-backed customers in the current branch with no IFRS9 evaluations in that same branch."""
    branch_scope = get_request_branch_scope(request)
    if not branch_scope:
        return []
    ifrs9_customer_ids = _get_scored_customer_codes_any_branch(
        IFRS9Evaluation,
        "ifrs9_scored_codes_any_branch",
    )
    return _build_stage_backed_customer_records(request, scored_customer_ids=ifrs9_customer_ids)


@login_required
def customer_without_ifrs9_list_view(request: HttpRequest) -> HttpResponse:
    """
    List customers who have no IFRS9 score form evaluations.
    Filters by current branch for both regular users and admins.
    """
    base_customers = _get_filtered_customers_without_ifrs9(request)
    search_query = (request.GET.get("q") or "").strip()
    filtered_customers = _apply_customer_search_to_collection(base_customers, search_query)
    page_obj, search_query, page_size = _paginate_customer_queryset(request, filtered_customers)
    context = {
        "customers": page_obj.object_list,
        "page_obj": page_obj,
        "customer_total": _count_collection(base_customers),
        "without_basel_total": None,
        "without_ifrs9_total": None,
        "search_query": search_query,
        "page_size": page_size,
        "list_query_string": _build_list_query_string(
            search_query=search_query,
            page_size=page_size,
        ),
    }
    return render(
        request,
        "credit_scoreshifts/customers/customer_list.html",
        context,
    )


@login_required
def customer_without_ifrs9_export_csv(request: HttpRequest) -> HttpResponse:
    """Export customers without IFRS9 score forms to CSV."""
    page = request.GET.get('page', 'current')
    customers = _get_filtered_customers_without_ifrs9(request)
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="customers_without_ifrs9_export_{page}.csv"'
    writer = csv.writer(response)
    writer.writerow(_get_customer_export_headers())
    for customer in customers:
        writer.writerow(_get_customer_export_row(customer))
    return response


@login_required
def customer_without_ifrs9_export_excel(request: HttpRequest) -> HttpResponse:
    """Export customers without IFRS9 score forms to Excel."""
    if not OPENPYXL_AVAILABLE:
        return HttpResponse("Excel export requires openpyxl library. Please install it.", status=500)
    page = request.GET.get('page', 'current')
    customers = _get_filtered_customers_without_ifrs9(request)
    wb = Workbook()
    ws = wb.active
    ws.title = "Customers Without IFRS9"
    headers = _get_customer_export_headers()
    header_fill = PatternFill(start_color="0066cc", end_color="0066cc", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    for col_num, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_num)
        cell.value = header
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center', vertical='center')
    for row_num, customer in enumerate(customers, 2):
        for col_num, value in enumerate(_get_customer_export_row(customer), 1):
            ws.cell(row=row_num, column=col_num, value=value)
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
    response['Content-Disposition'] = f'attachment; filename="customers_without_ifrs9_export_{page}.xlsx"'
    wb.save(response)
    return response


@login_required
def add_customer_view(request: HttpRequest) -> HttpResponse:
    current_branch_code, current_branch = _get_current_branch(request)
    form_data = {}

    if is_all_branches_selected(request):
        messages.error(request, "Choose one specific branch before adding a manual fallback customer.")
        return redirect("scorecard:customer_list")

    if not current_branch_code or current_branch is None:
        messages.error(request, "Select a branch first before adding a manual fallback customer.")
        return redirect("scorecard:customer_list")

    if not request.user.is_superuser and not request.user.has_branch_access(
        branch_id=current_branch.id,
        branch_code=current_branch.branch_code,
        branch_name=current_branch.branch_name,
    ):
        messages.error(request, "You do not have permission to add customers in this branch.")
        return redirect("scorecard:customer_list")

    if request.method == "POST":
        form_data = {
            "customer_ref_code": (request.POST.get("customer_ref_code") or "").strip(),
            "customer_name": (request.POST.get("customer_name") or "").strip(),
            "email": (request.POST.get("email") or "").strip(),
            "mobile": (request.POST.get("mobile") or "").strip(),
            "national_id": (request.POST.get("national_id") or "").strip(),
            "customer_type": (request.POST.get("customer_type") or "").strip(),
            "resident_status": (request.POST.get("resident_status") or "").strip(),
            "nationality_code": (request.POST.get("nationality_code") or "").strip(),
            "occupation_code": (request.POST.get("occupation_code") or "").strip(),
            "employer_name": (request.POST.get("employer_name") or "").strip(),
        }

        if not form_data["customer_ref_code"] or not form_data["customer_name"]:
            messages.error(request, "Customer Code and Customer Name are required.")
        else:
            reporting_date = _get_manual_customer_reporting_date(current_branch.branch_name, current_branch.branch_code)
            customer_type_value = form_data["customer_type"].upper() if form_data["customer_type"] else ""

            customer, created = MainCustomer.objects.update_or_create(
                reporting_date=reporting_date,
                customer_ref_code=form_data["customer_ref_code"],
                branch_code=current_branch.branch_code,
                defaults={
                    "branch_name": current_branch.branch_name,
                    "branch_description": current_branch.branch_name,
                    "customer_name": form_data["customer_name"],
                    "customer_type": customer_type_value or None,
                    "email": form_data["email"] or None,
                    "mobile": form_data["mobile"] or None,
                    "national_id": form_data["national_id"] or None,
                    "resident_status": form_data["resident_status"] or None,
                    "nationality_code": form_data["nationality_code"] or None,
                    "occupation_code": form_data["occupation_code"] or None,
                    "employer_name": form_data["employer_name"] or None,
                    "has_loan": False,
                    "has_overdraft": False,
                    "loan_count": 0,
                    "overdraft_count": 0,
                    "is_active_for_scoring": True,
                    "last_synced_at": timezone.now(),
                },
            )
            messages.success(
                request,
                f"Manual fallback customer '{customer.customer_name}' was {'created' if created else 'updated'} successfully for branch '{current_branch.branch_name}'.",
            )
            return redirect("scorecard:customer_list")

    context = {
        "current_branch": current_branch,
        "form_data": form_data,
        "default_reporting_date": _get_manual_customer_reporting_date(current_branch.branch_name, current_branch.branch_code),
    }
    return render(
        request,
        "credit_scoreshifts/customers/add_customer.html",
        context,
    )


@login_required
def branch_master_view(request: HttpRequest) -> HttpResponse:
    """
    Manual branch master used for user branch access and scorecard branch selection.
    """
    can_manage_branch_master = request.user.is_superuser or request.user.has_perm("scorecard.manage_scorecard_branches")
    can_view_branch_master = can_manage_branch_master or request.user.has_perm("scorecard.view_scorecard_branches")

    if not can_view_branch_master:
        messages.error(request, "You do not have permission to access the branch master.")
        return redirect("scorecard:scorecard_dashboard")

    if request.method == "POST" and not can_manage_branch_master:
        messages.error(request, "You have read-only access to the branch master.")
        return redirect("scorecard:branch_master")

    editing_branch = None
    edit_branch_id = request.GET.get("edit")
    if edit_branch_id:
        editing_branch = get_object_or_404(BankBranch, pk=edit_branch_id)

    if request.method == "POST":
        if "cancel" in request.POST:
            return redirect("scorecard:branch_master")
        branch_id = request.POST.get("branch_id")
        instance = get_object_or_404(BankBranch, pk=branch_id) if branch_id else None
        form = BankBranchForm(request.POST, instance=instance)
        if form.is_valid():
            saved_branch = form.save()
            _clear_branch_master_caches()
            action = "updated" if instance else "created"
            messages.success(
                request,
                f"Branch '{saved_branch.branch_name}' was {action} successfully.",
            )
            return redirect("scorecard:branch_master")
        editing_branch = instance
        messages.error(request, "Please correct the branch form before saving.")
    else:
        form = BankBranchForm(instance=editing_branch)

    user_relation_name = _get_branch_user_relation_name()
    branch_queryset = BankBranch.objects.only(
        "id",
        "branch_code",
        "branch_name",
        "bank_name",
        "address",
        "city",
        "province",
        "zipcode",
        "landline",
        "mobile",
    ).order_by("bank_name", "branch_name")
    if user_relation_name:
        branch_queryset = branch_queryset.annotate(
            assigned_users_count=Count(user_relation_name, distinct=True),
        )
    branch_rows = list(branch_queryset)

    branch_records: list[dict[str, object]] = []
    total_assigned_users = 0
    branches_with_users = 0

    for branch in branch_rows:
        assigned_users_count = int(getattr(branch, "assigned_users_count", 0) or 0)
        if assigned_users_count:
            branches_with_users += 1

        branch_records.append(
            {
                "id": branch.id,
                "branch_code": branch.branch_code,
                "branch_name": branch.branch_name,
                "bank_name": branch.bank_name,
                "address": branch.address or "-",
                "city": branch.city or "-",
                "province": branch.province or "-",
                "zipcode": branch.zipcode or "-",
                "landline": branch.landline or "-",
                "mobile": branch.mobile or "-",
                "assigned_users_count": assigned_users_count,
                "status_label": "Assigned" if assigned_users_count else "Unassigned",
                "status_class": "success" if assigned_users_count else "neutral",
                "edit_url": f"?edit={branch.id}",
            }
        )

    context = {
        "branch_form": form,
        "editing_branch": editing_branch,
        "branch_records": branch_records,
        "branch_master_summary": {
            "total_branches": len(branch_records),
            "branches_with_users": branches_with_users,
            "branches_without_users": max(0, len(branch_records) - branches_with_users),
            "total_assigned_users": (
                ScorecardUserBranchAccess.objects.values("user_id").distinct().count()
                if user_relation_name
                else 0
            ),
        },
        "branch_master_diagnostics_url": "?diagnostics=1",
        "can_manage_branch_master": can_manage_branch_master,
    }
    if request.GET.get("diagnostics") == "1":
        return JsonResponse(_get_branch_master_diagnostics())

    return render(request, "credit_scoreshifts/branches/branch_master.html", context)


@login_required
def branch_master_sync_from_main_customer_view(request: HttpRequest) -> HttpResponse:
    """Load unique branches from MainCustomer without touching the add/edit branch form."""
    if request.method != "POST":
        return redirect("scorecard:branch_master")

    can_manage_branch_master = request.user.is_superuser or request.user.has_perm("scorecard.manage_scorecard_branches")
    if not can_manage_branch_master:
        messages.error(request, "You do not have permission to load branches from MainCustomer.")
        return redirect("scorecard:branch_master")

    created, updated, skipped = _load_branch_master_from_main_customer()
    _clear_branch_master_caches()
    if created or updated:
        messages.success(
            request,
            f"Branch Master refresh completed from MainCustomer. Created {created}, updated {updated}, skipped {skipped}.",
        )
    else:
        messages.info(
            request,
            f"No new branch changes were loaded from MainCustomer. {skipped} branch(es) already matched Branch Master.",
        )
    return redirect("scorecard:branch_master")


@login_required
def branch_master_delete_view(request: HttpRequest, branch_id: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("scorecard:branch_master")

    if not (request.user.is_superuser or request.user.has_perm("scorecard.manage_scorecard_branches")):
        messages.error(request, "You do not have permission to delete branches.")
        return redirect("scorecard:scorecard_dashboard")

    user_relation_name = _get_branch_user_relation_name()
    branch_queryset = BankBranch.objects.all()
    if user_relation_name:
        branch_queryset = branch_queryset.annotate(assigned_users_count=Count(user_relation_name, distinct=True))
    branch = get_object_or_404(branch_queryset, pk=branch_id)
    if int(getattr(branch, "assigned_users_count", 0) or 0) > 0:
        messages.error(
            request,
            f"Branch '{branch.branch_name}' cannot be deleted because it is still assigned to user access.",
        )
        return redirect("scorecard:branch_master")

    branch_name = branch.branch_name
    branch.delete()
    _clear_branch_master_caches()
    messages.success(request, f"Branch '{branch_name}' was deleted successfully.")
    return redirect("scorecard:branch_master")
