from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from io import BytesIO
import re

import openpyxl
import pandas as pd

from django import forms
from django.apps import apps
from django.contrib import messages
from django.core.cache import cache
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Max, Q
from django.db.models.functions import Trim, Upper
from django.db.utils import OperationalError, ProgrammingError
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from scorecard.functions_view.audit import log_scorecard_audit
from scorecard.functions_view.main_customer_lookup import (
    current_branch_display_name,
    get_request_branch_scope,
    is_all_branches_selected,
    resolve_branch_context,
)
from scorecard.models import BankBranch


UPLOAD_ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".csv"}
COLLATERAL_UPLOAD_COLUMNS = [
    "customer_ref_code",
    "loan_id (optional lookup fallback)",
    "reporting_date (optional)",
    "collateral_value",
    "collateral_type",
    "currency_code (optional)",
]
PAYMENT_UPLOAD_COLUMNS = [
    "loan_id",
    "reporting_date (optional)",
    "cash_flow_date",
    "total_cash_flow_amount",
    "currency_code (optional)",
]
REGISTER_PAGE_SIZE_OPTIONS = (20, 50, 100)
DEFAULT_REGISTER_PAGE_SIZE = 20
COLLATERAL_UPLOAD_TEMPLATE_HEADERS = [
    "customer_ref_code",
    "loan_id",
    "reporting_date",
    "collateral_value",
    "collateral_type",
    "currency_code",
]
SUPPORTING_DATA_CACHE_TTL_SECONDS = 120

PAYMENT_UPLOAD_TEMPLATE_HEADERS = [
    "loan_id",
    "reporting_date",
    "cash_flow_date",
    "total_cash_flow_amount",
    "currency_code",
]


def _resolve_current_branch(request: HttpRequest):
    return resolve_branch_context(request)


def _branch_scope_display(current_branch, request: HttpRequest):
    if current_branch is not None:
        return current_branch
    return SimpleNamespace(
        id=None,
        branch_name=current_branch_display_name(request) or "ALL ASSIGNED BRANCHES",
        branch_code="ALL",
        bank_name="",
    )


def _load_ifrs9_staging_models():
    try:
        loan_model = apps.get_model("IFRS9", "stg_loans_data")
        payment_model = apps.get_model("IFRS9", "stg_payment_schedule")
        collateral_model = apps.get_model("IFRS9", "stg_collateral_data")
    except LookupError:
        return None, None, None, (
            "The IFRS9 workspace is not available in this environment. "
            "This feature needs the IFRS9 app and the staging models "
            "`stg_loans_data`, `stg_payment_schedule`, and `stg_collateral_data`."
        )
    return loan_model, payment_model, collateral_model, None


def _branch_loan_cache_key(branch: BankBranch, suffix: str) -> str:
    branch_code = "".join((branch.branch_code or "").strip().upper().split()) or "NONE"
    branch_name = " ".join((branch.branch_name or "").strip().upper().split()) or "NONE"
    return f"scorecard:ifrs9_supporting:{branch_code}:{branch_name}:{suffix}"


def _serialize_branch_loans(loans) -> list[dict[str, str]]:
    return [
        {
            "v_loan_id": str(getattr(loan, "v_loan_id", "") or ""),
            "v_cust_name": (getattr(loan, "v_cust_name", "") or "").strip(),
            "v_prod_name": (getattr(loan, "v_prod_name", "") or "").strip(),
            "v_cust_ref_code": (getattr(loan, "v_cust_ref_code", "") or "").strip(),
            "v_ccy_code": (getattr(loan, "v_ccy_code", "") or "").strip(),
            "v_branch_name": (getattr(loan, "v_branch_name", "") or "").strip(),
            "v_branch_code": (getattr(loan, "v_branch_code", "") or "").strip(),
        }
        for loan in loans
    ]


def _deserialize_branch_loans(rows):
    return [SimpleNamespace(**row) for row in rows]


def _get_branch_filter(branch: BankBranch) -> Q:
    branch_code = (branch.branch_code or "").strip()
    branch_name = (branch.branch_name or "").strip()
    query = Q()
    if branch_code:
        query |= Q(v_branch_code__iexact=branch_code)
    if branch_name:
        query |= Q(v_branch_name__iexact=branch_name)
    return query


def _get_branch_active_loans(branch: BankBranch, loan_model):
    cache_key = _branch_loan_cache_key(branch, "active_loans")
    cached_snapshot = cache.get(cache_key)
    if cached_snapshot is not None:
        return cached_snapshot.get("latest_date"), _deserialize_branch_loans(cached_snapshot.get("loans", []))

    branch_filter = _get_branch_filter(branch)
    if not branch_filter.children:
        return None, []

    latest_date = (
        loan_model.objects.filter(branch_filter)
        .aggregate(latest_date=Max("fic_mis_date"))
        .get("latest_date")
    )
    if latest_date is None:
        cache.set(cache_key, {"latest_date": None, "loans": []}, SUPPORTING_DATA_CACHE_TTL_SECONDS)
        return None, []

    loans = list(
        loan_model.objects.filter(branch_filter, fic_mis_date=latest_date)
        .exclude(v_loan_id__isnull=True)
        .exclude(v_loan_id="")
        .only(
            "v_loan_id",
            "v_cust_name",
            "v_prod_name",
            "v_cust_ref_code",
            "v_ccy_code",
            "v_branch_name",
            "v_branch_code",
        )
        .order_by("v_cust_name", "v_loan_id")
    )
    cache.set(cache_key, {"latest_date": latest_date, "loans": _serialize_branch_loans(loans)}, SUPPORTING_DATA_CACHE_TTL_SECONDS)
    return latest_date, loans


def _get_branch_active_loan_keys(branch: BankBranch, loan_model) -> tuple[date | None, int, list[str], list[str]]:
    cache_key = _branch_loan_cache_key(branch, "active_loan_keys")
    cached_snapshot = cache.get(cache_key)
    if cached_snapshot is not None:
        return (
            cached_snapshot.get("latest_date"),
            cached_snapshot.get("active_loan_count", 0),
            cached_snapshot.get("loan_id_keys", []),
            cached_snapshot.get("customer_ref_keys", []),
        )

    branch_filter = _get_branch_filter(branch)
    if not branch_filter.children:
        return None, 0, [], []

    latest_date = (
        loan_model.objects.filter(branch_filter)
        .aggregate(latest_date=Max("fic_mis_date"))
        .get("latest_date")
    )
    if latest_date is None:
        cache.set(cache_key, {"latest_date": None, "active_loan_count": 0, "loan_id_keys": [], "customer_ref_keys": []}, SUPPORTING_DATA_CACHE_TTL_SECONDS)
        return None, 0, [], []

    loan_rows = list(
        loan_model.objects.filter(branch_filter, fic_mis_date=latest_date)
        .exclude(v_loan_id__isnull=True)
        .exclude(v_loan_id="")
        .values_list("v_loan_id", "v_cust_ref_code")
    )
    loan_id_keys = sorted({_normalize_text_key(loan_id) for loan_id, _customer_ref in loan_rows if _normalize_text_key(loan_id)})
    customer_ref_keys = sorted({_normalize_customer_reference(customer_ref) for _loan_id, customer_ref in loan_rows if _normalize_customer_reference(customer_ref)})
    snapshot = {"latest_date": latest_date, "active_loan_count": len(loan_rows), "loan_id_keys": loan_id_keys, "customer_ref_keys": customer_ref_keys}
    cache.set(cache_key, snapshot, SUPPORTING_DATA_CACHE_TTL_SECONDS)
    return latest_date, len(loan_rows), loan_id_keys, customer_ref_keys


def _get_scope_active_loans(branch_scope: list[BankBranch], loan_model):
    latest_dates: list[date] = []
    combined_loans = []
    seen_keys: set[tuple[str, str, str]] = set()
    for branch in branch_scope:
        latest_date, loans = _get_branch_active_loans(branch, loan_model)
        if latest_date is not None:
            latest_dates.append(latest_date)
        for loan in loans:
            loan_key = (
                str(getattr(loan, "v_loan_id", "") or ""),
                (getattr(loan, "v_branch_code", "") or "").strip(),
                (getattr(loan, "v_branch_name", "") or "").strip(),
            )
            if loan_key in seen_keys:
                continue
            seen_keys.add(loan_key)
            combined_loans.append(loan)
    latest_reporting_date = max(latest_dates) if latest_dates else None
    return latest_reporting_date, combined_loans


def _get_scope_active_loan_keys(branch_scope: list[BankBranch], loan_model) -> tuple[date | None, int, list[str], list[str]]:
    latest_dates: list[date] = []
    active_loan_count = 0
    loan_id_keys: set[str] = set()
    customer_ref_keys: set[str] = set()
    for branch in branch_scope:
        latest_date, branch_count, branch_loan_id_keys, branch_customer_ref_keys = _get_branch_active_loan_keys(
            branch,
            loan_model,
        )
        if latest_date is not None:
            latest_dates.append(latest_date)
        active_loan_count += branch_count
        loan_id_keys.update(branch_loan_id_keys)
        customer_ref_keys.update(branch_customer_ref_keys)
    latest_reporting_date = max(latest_dates) if latest_dates else None
    return latest_reporting_date, active_loan_count, sorted(loan_id_keys), sorted(customer_ref_keys)


def _loan_choices(loans) -> list[tuple[str, str]]:
    choices = [("", "Select active loan")]
    for loan in loans:
        customer_name = (getattr(loan, "v_cust_name", "") or "").strip() or "Unknown customer"
        product_name = (getattr(loan, "v_prod_name", "") or "").strip()
        label = f"{loan.v_loan_id} - {customer_name}"
        if product_name:
            label = f"{label} ({product_name})"
        choices.append((str(loan.v_loan_id), label))
    return choices


def _loan_metadata(loans) -> dict[str, dict[str, str]]:
    return {
        str(loan.v_loan_id): {
            "customer_name": (getattr(loan, "v_cust_name", "") or "").strip(),
            "customer_ref_code": (getattr(loan, "v_cust_ref_code", "") or "").strip(),
            "currency_code": (getattr(loan, "v_ccy_code", "") or "").strip(),
            "product_name": (getattr(loan, "v_prod_name", "") or "").strip(),
            "branch_name": (getattr(loan, "v_branch_name", "") or "").strip(),
            "branch_code": (getattr(loan, "v_branch_code", "") or "").strip(),
        }
        for loan in loans
    }


def _customer_choices(loans) -> list[tuple[str, str]]:
    choices = [("", "Select active customer")]
    seen_customers: set[str] = set()
    for loan in loans:
        customer_ref_code = (getattr(loan, "v_cust_ref_code", "") or "").strip()
        normalized_ref = _normalize_customer_reference(customer_ref_code)
        if not customer_ref_code or normalized_ref in seen_customers:
            continue
        seen_customers.add(normalized_ref)
        customer_name = (getattr(loan, "v_cust_name", "") or "").strip() or "Unknown customer"
        branch_name = (getattr(loan, "v_branch_name", "") or "").strip()
        label = f"{customer_ref_code} - {customer_name}"
        if branch_name:
            label = f"{label} ({branch_name})"
        choices.append((customer_ref_code, label))
    return choices


def _customer_metadata(loans) -> dict[str, dict[str, str]]:
    metadata: dict[str, dict[str, object]] = {}
    for loan in loans:
        customer_ref_code = (getattr(loan, "v_cust_ref_code", "") or "").strip()
        normalized_ref = _normalize_customer_reference(customer_ref_code)
        if not customer_ref_code or not normalized_ref:
            continue
        row = metadata.setdefault(
            customer_ref_code,
            {
                "customer_name": (getattr(loan, "v_cust_name", "") or "").strip(),
                "customer_ref_code": customer_ref_code,
                "currency_code": (getattr(loan, "v_ccy_code", "") or "").strip(),
                "branch_name": (getattr(loan, "v_branch_name", "") or "").strip(),
                "branch_code": (getattr(loan, "v_branch_code", "") or "").strip(),
                "loan_ids": [],
                "product_names": [],
            },
        )
        loan_id = str(getattr(loan, "v_loan_id", "") or "").strip()
        product_name = (getattr(loan, "v_prod_name", "") or "").strip()
        if loan_id and loan_id not in row["loan_ids"]:
            row["loan_ids"].append(loan_id)
        if product_name and product_name not in row["product_names"]:
            row["product_names"].append(product_name)

    return {
        customer_ref_code: {
            "customer_name": str(row.get("customer_name") or ""),
            "customer_ref_code": str(row.get("customer_ref_code") or customer_ref_code),
            "currency_code": str(row.get("currency_code") or ""),
            "branch_name": str(row.get("branch_name") or ""),
            "branch_code": str(row.get("branch_code") or ""),
            "loan_ids": ", ".join(row.get("loan_ids") or []),
            "product_names": ", ".join(row.get("product_names") or []),
            "loan_count": str(len(row.get("loan_ids") or [])),
        }
        for customer_ref_code, row in metadata.items()
    }


def _loan_customer_map(loans) -> dict[str, object]:
    customer_map = {}
    for loan in loans:
        customer_ref_code = _normalize_customer_reference(getattr(loan, "v_cust_ref_code", ""))
        if customer_ref_code and customer_ref_code not in customer_map:
            customer_map[customer_ref_code] = loan
    return customer_map


def _parse_register_page_size(request: HttpRequest) -> int:
    raw_value = (request.GET.get("page_size") or "").strip()
    try:
        page_size = int(raw_value)
    except (TypeError, ValueError):
        return DEFAULT_REGISTER_PAGE_SIZE
    return page_size if page_size in REGISTER_PAGE_SIZE_OPTIONS else DEFAULT_REGISTER_PAGE_SIZE


def _build_list_query_string(request: HttpRequest, *exclude_keys: str) -> str:
    params = request.GET.copy()
    for key in exclude_keys:
        if key in params:
            del params[key]
    return params.urlencode()


def _build_list_query_pairs(request: HttpRequest, *exclude_keys: str) -> list[tuple[str, str]]:
    exclude = set(exclude_keys)
    return [
        (key, value)
        for key, values in request.GET.lists()
        if key not in exclude
        for value in values
    ]


def _normalize_text_key(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    elif isinstance(value, Decimal) and value == value.to_integral_value():
        value = int(value)
    text = " ".join(str(value).strip().split())
    if re.fullmatch(r"-?\d+\.0+", text):
        text = text.split(".", 1)[0]
    return text.upper()


def _normalize_customer_reference(value) -> str:
    normalized = _normalize_text_key(value).lstrip("0")
    return normalized or "0" if str(value or "").strip() else ""


def _apply_form_styles(form: forms.Form) -> forms.Form:
    for field in form.fields.values():
        css_class = field.widget.attrs.get("class", "")
        if isinstance(field.widget, forms.DateInput):
            field.widget.attrs["class"] = (css_class + " support-input").strip()
            field.widget.attrs.setdefault("type", "date")
        elif isinstance(field.widget, forms.Select):
            field.widget.attrs["class"] = (css_class + " support-select").strip()
        else:
            field.widget.attrs["class"] = (css_class + " support-input").strip()
    return form


def _normalize_upload_column_name(value) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower())
    return text.strip("_")


def _upload_value(row: dict, *aliases: str):
    for alias in aliases:
        value = row.get(alias)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return value
    return None


def _parse_optional_decimal(value, *, field_label: str, row_number: int) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text == "":
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError(f"Row {row_number}: {field_label} must be a valid number.")


def _parse_required_text(value, *, field_label: str, row_number: int) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        raise ValueError(f"Row {row_number}: {field_label} is required.")
    return text


def _parse_optional_date(value, *, field_label: str, row_number: int) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"Row {row_number}: {field_label} must be a valid date.")
    return parsed.date()


def _collateral_capture_date() -> date:
    return timezone.localdate()


def _get_active_loan_by_id(state: dict, loan_id) -> object | None:
    if loan_id is None:
        return None
    text_loan_id = str(loan_id).strip()
    if not text_loan_id:
        return None
    direct_match = state["loan_map"].get(text_loan_id)
    if direct_match is not None:
        return direct_match
    normalized_loan_id = _normalize_text_key(text_loan_id)
    return state["normalized_loan_map"].get(normalized_loan_id)


def _get_active_customer_loan_by_ref(state: dict, customer_ref_code) -> object | None:
    normalized_ref = _normalize_customer_reference(customer_ref_code)
    if not normalized_ref:
        return None
    return state["customer_loan_map"].get(normalized_ref)


def _load_uploaded_rows(uploaded_file) -> list[dict]:
    file_name = (uploaded_file.name or "").lower()
    file_extension = ""
    if "." in file_name:
        file_extension = file_name[file_name.rfind("."):]
    if file_extension not in UPLOAD_ALLOWED_EXTENSIONS:
        raise ValueError("Only .xlsx, .xls, or .csv files are supported.")

    uploaded_file.seek(0)
    try:
        if file_extension == ".csv":
            dataframe = pd.read_csv(uploaded_file)
        else:
            dataframe = pd.read_excel(uploaded_file)
    except Exception as exc:
        raise ValueError(f"Could not read the uploaded file. Please confirm it is a valid Excel or CSV file. Details: {exc}")

    dataframe = dataframe.dropna(how="all")
    if dataframe.empty:
        raise ValueError("The uploaded file is empty.")

    dataframe.columns = [_normalize_upload_column_name(column) for column in dataframe.columns]
    dataframe = dataframe.where(pd.notnull(dataframe), None)
    return dataframe.to_dict(orient="records")


class SupportingDataUploadForm(forms.Form):
    file = forms.FileField(
        label="Excel File",
        widget=forms.ClearableFileInput(attrs={"accept": ".xlsx,.xls,.csv"}),
    )

    def __init__(self, *args, expected_columns=None, **kwargs):
        super().__init__(*args, **kwargs)
        expected_columns = expected_columns or []
        if expected_columns:
            self.fields["file"].help_text = "Expected columns: " + ", ".join(expected_columns)
        _apply_form_styles(self)

    def clean_file(self):
        uploaded_file = self.cleaned_data["file"]
        file_name = (uploaded_file.name or "").lower()
        if not any(file_name.endswith(extension) for extension in UPLOAD_ALLOWED_EXTENSIONS):
            raise forms.ValidationError("Please upload an Excel or CSV file in .xlsx, .xls, or .csv format.")
        return uploaded_file


class BranchPaymentScheduleForm(forms.Form):
    v_loan_id = forms.ChoiceField(label="Loan", choices=[])
    d_cash_flow_date = forms.DateField(
        label="Cash Flow Date",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    n_cash_flow_amount = forms.DecimalField(
        label="Total Cash Flow Amount",
        required=True,
        max_digits=20,
        decimal_places=6,
    )
    v_ccy_code = forms.CharField(
        label="Currency Code",
        required=False,
        max_length=3,
    )

    def __init__(self, *args, loan_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["v_loan_id"].choices = loan_choices or [("", "Select active loan")]
        self.fields["v_ccy_code"].help_text = "Defaults to the selected loan currency when left blank."
        _apply_form_styles(self)

    def clean_v_ccy_code(self):
        return (self.cleaned_data.get("v_ccy_code") or "").strip().upper()

    def clean(self):
        cleaned_data = super().clean()
        cash_flow = cleaned_data.get("n_cash_flow_amount")

        if cash_flow is None:
            raise forms.ValidationError("Total Cash Flow Amount is required.")

        return cleaned_data


class BranchPaymentScheduleBatchForm(forms.Form):
    v_loan_id = forms.ChoiceField(label="Loan", choices=[])
    v_ccy_code = forms.CharField(
        label="Currency Code",
        required=False,
        max_length=3,
    )

    def __init__(self, *args, loan_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["v_loan_id"].choices = loan_choices or [("", "Select active loan")]
        self.fields["v_ccy_code"].help_text = "Defaults to the selected loan currency when left blank."
        _apply_form_styles(self)

    def clean_v_ccy_code(self):
        return (self.cleaned_data.get("v_ccy_code") or "").strip().upper()


def _clean_schedule_decimal(value, *, field_label: str, required: bool) -> Decimal | None:
    value = "" if value is None else str(value).strip()
    field = forms.DecimalField(
        required=required,
        max_digits=20,
        decimal_places=6,
        error_messages={
            "required": f"{field_label} is required.",
            "invalid": f"{field_label} must be a valid number.",
        },
    )
    return field.clean(value)


def _clean_schedule_date(value) -> date | None:
    value = "" if value is None else str(value).strip()
    field = forms.DateField(
        required=True,
        widget=forms.DateInput(attrs={"type": "date"}),
        error_messages={
            "required": "Cash Flow Date is required.",
            "invalid": "Cash Flow Date must be a valid date.",
        },
    )
    return field.clean(value)


def _parse_payment_schedule_batch_rows(post_data) -> tuple[list[dict], list[dict]]:
    dates = post_data.getlist("schedule_d_cash_flow_date[]")
    cash_flow_amounts = post_data.getlist("schedule_n_cash_flow_amount[]")
    row_count = max(len(dates), len(cash_flow_amounts), 1)
    prepared_rows = []
    rows_for_template = []
    seen_dates = set()

    for index in range(row_count):
        raw_row = {
            "d_cash_flow_date": dates[index] if index < len(dates) else "",
            "n_cash_flow_amount": cash_flow_amounts[index] if index < len(cash_flow_amounts) else "",
            "errors": [],
        }
        if not any(str(value or "").strip() for key, value in raw_row.items() if key != "errors"):
            continue

        cleaned_row = raw_row.copy()
        try:
            cash_flow_date = _clean_schedule_date(raw_row["d_cash_flow_date"])
            if cash_flow_date in seen_dates:
                raw_row["errors"].append("This cash flow date appears more than once for the selected loan.")
            else:
                seen_dates.add(cash_flow_date)
            cleaned_row["d_cash_flow_date"] = cash_flow_date
        except forms.ValidationError as exc:
            raw_row["errors"].extend(exc.messages)
            cash_flow_date = None

        for field_name, label, required in (
            ("n_cash_flow_amount", "Total Cash Flow Amount", True),
        ):
            try:
                cleaned_row[field_name] = _clean_schedule_decimal(
                    raw_row[field_name],
                    field_label=label,
                    required=required,
                )
            except forms.ValidationError as exc:
                raw_row["errors"].extend(exc.messages)

        rows_for_template.append(raw_row)
        if not raw_row["errors"] and cash_flow_date is not None:
            prepared_rows.append(
                {
                    "d_cash_flow_date": cleaned_row["d_cash_flow_date"],
                    "n_cash_flow_amount": cleaned_row["n_cash_flow_amount"],
                }
            )

    if not rows_for_template:
        rows_for_template.append(
            {
                "d_cash_flow_date": "",
                "n_cash_flow_amount": "",
                "errors": ["Add at least one cash flow row before saving."],
            }
        )

    return prepared_rows, rows_for_template


def _collateral_type_choices() -> list[tuple[str, str]]:
    choices = [("", "Select collateral type")]
    try:
        collateral_type_model = apps.get_model("IFRS9", "CollateralType")
        rows = (
            collateral_type_model.objects.filter(is_active=True)
            .order_by("v_collateral_type")
            .values_list("v_collateral_type", flat=True)
        )
        choices.extend((row, row) for row in rows if row)
    except (LookupError, OperationalError, ProgrammingError):
        pass
    return choices


def _collateral_type_lookup() -> dict[str, str]:
    return {
        str(value).strip().upper(): str(value).strip()
        for value, _label in _collateral_type_choices()
        if str(value or "").strip()
    }


def _collateral_customer_type_exists(
    state: dict,
    *,
    customer_ref_code: str,
    collateral_type: str,
    exclude_pk: int | None = None,
) -> bool:
    queryset = state["collateral_model"].objects.filter(
        v_cust_ref_code__iexact=(customer_ref_code or "").strip(),
        collateral_type__iexact=(collateral_type or "").strip(),
    )
    if exclude_pk is not None:
        queryset = queryset.exclude(pk=exclude_pk)
    return queryset.exists()


class BranchCollateralForm(forms.Form):
    v_cust_ref_code = forms.ChoiceField(label="Customer ID", choices=[])
    collateral_value = forms.DecimalField(
        label="Collateral Value",
        max_digits=24,
        decimal_places=2,
    )
    collateral_type = forms.ChoiceField(
        label="Collateral Type",
        choices=[],
    )
    v_ccy_code = forms.CharField(
        label="Currency Code",
        required=True,
        max_length=10,
    )

    def __init__(self, *args, customer_choices=None, collateral_type_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["v_cust_ref_code"].choices = customer_choices or [("", "Select active customer")]
        type_choices = list(collateral_type_choices or _collateral_type_choices())
        current_type = ""
        if self.is_bound:
            current_type = str(self.data.get(self.add_prefix("collateral_type"), "") or "").strip()
        else:
            current_type = str(self.initial.get("collateral_type", "") or "").strip()
        if current_type and current_type not in {value for value, _label in type_choices}:
            type_choices.append((current_type, current_type))
        self.fields["collateral_type"].choices = type_choices
        self.fields["v_cust_ref_code"].widget.attrs["data-customer-search"] = "true"
        self.fields["collateral_type"].help_text = "Select a configured collateral type from LGD > Collateral Types."
        self.fields["v_ccy_code"].help_text = "Required. It auto-fills from the selected customer when available."
        _apply_form_styles(self)

    def clean_collateral_type(self):
        value = " ".join((self.cleaned_data.get("collateral_type") or "").strip().split())
        lookup = _collateral_type_lookup()
        if value and lookup and value.upper() not in lookup:
            raise forms.ValidationError("Select a valid configured collateral type.")
        return lookup.get(value.upper(), value)

    def clean_v_ccy_code(self):
        return (self.cleaned_data.get("v_ccy_code") or "").strip().upper()


def _collateral_upload_preview_columns() -> list[str]:
    return COLLATERAL_UPLOAD_COLUMNS


def _payment_upload_preview_columns() -> list[str]:
    return PAYMENT_UPLOAD_COLUMNS


def _build_supporting_data_template_response(*, filename: str, sheet_title: str, headers: list[str], sample_row: list[str], notes: list[str]) -> HttpResponse:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = sheet_title

    header_fill = openpyxl.styles.PatternFill(fill_type="solid", fgColor="1F4E78")
    header_font = openpyxl.styles.Font(color="FFFFFF", bold=True)
    note_font = openpyxl.styles.Font(color="5B6573", italic=True)

    worksheet.append(headers)
    for column_index, header in enumerate(headers, start=1):
        cell = worksheet.cell(row=1, column=column_index)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = openpyxl.styles.Alignment(horizontal="center")
        worksheet.column_dimensions[openpyxl.utils.get_column_letter(column_index)].width = max(len(header) + 4, 18)

    worksheet.append(sample_row)
    for column_index in range(1, len(headers) + 1):
        worksheet.cell(row=2, column=column_index).font = note_font

    instructions_sheet = workbook.create_sheet("Instructions")
    instructions_sheet["A1"] = "How to use this template"
    instructions_sheet["A1"].font = openpyxl.styles.Font(bold=True, size=13)
    for row_index, note in enumerate(notes, start=3):
        instructions_sheet[f"A{row_index}"] = note
    instructions_sheet.column_dimensions["A"].width = 120

    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)

    response = HttpResponse(
        buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _process_collateral_upload(state: dict, uploaded_file) -> tuple[int, int]:
    rows = _load_uploaded_rows(uploaded_file)
    if state["latest_reporting_date"] is None:
        raise ValueError("No active IFRS9 customers were found for the current branch, so collateral cannot be uploaded yet.")

    collateral_type_lookup = _collateral_type_lookup()
    prepared_rows = []
    seen_customer_type_keys = set()
    for index, row in enumerate(rows, start=2):
        raw_loan_id = _upload_value(row, "v_loan_id", "loan_id", "loan")
        raw_customer_ref = _upload_value(
            row,
            "v_cust_ref_code",
            "customer_ref_code",
            "customer_code",
            "cust_ref_code",
        )

        customer_ref_code = " ".join(str(raw_customer_ref or "").strip().split())
        loan = _get_active_customer_loan_by_ref(state, customer_ref_code)
        if loan is None and raw_loan_id:
            loan = _get_active_loan_by_id(state, raw_loan_id)
            customer_ref_code = (getattr(loan, "v_cust_ref_code", "") or "").strip() if loan else customer_ref_code
        if loan is None:
            raise ValueError(
                f"Row {index}: provide a valid active branch customer reference code."
            )

        reporting_date = _parse_optional_date(
            _upload_value(row, "fic_mis_date", "reporting_date", "mis_date", "date"),
            field_label="Reporting Date",
            row_number=index,
        ) or _collateral_capture_date()
        collateral_value = _parse_optional_decimal(
            _upload_value(row, "collateral_value", "value", "amount"),
            field_label="Collateral Value",
            row_number=index,
        )
        if collateral_value is None:
            raise ValueError(f"Row {index}: Collateral Value is required.")
        collateral_type = _parse_required_text(
            _upload_value(row, "collateral_type", "type"),
            field_label="Collateral Type",
            row_number=index,
        )
        collateral_type_key = collateral_type.strip().upper()
        if collateral_type_lookup and collateral_type_key not in collateral_type_lookup:
            allowed_preview = ", ".join(sorted(collateral_type_lookup.values())[:12])
            if len(collateral_type_lookup) > 12:
                allowed_preview = f"{allowed_preview}, ..."
            raise ValueError(
                f"Row {index}: Collateral Type must be one of the configured LGD collateral types: {allowed_preview}."
            )
        collateral_type = collateral_type_lookup.get(collateral_type_key, collateral_type)
        currency_code = " ".join(
            str(
                _upload_value(row, "v_ccy_code", "currency_code", "currency", "ccy_code")
                or getattr(loan, "v_ccy_code", "")
                or ""
            ).strip().upper().split()
        )
        if not customer_ref_code:
            raise ValueError(
                f"Row {index}: customer reference code is required for collateral."
            )
        duplicate_key = (
            _normalize_customer_reference(customer_ref_code),
            collateral_type.strip().upper(),
        )
        if duplicate_key in seen_customer_type_keys:
            raise ValueError(
                f"Row {index}: duplicate customer/collateral type found in the upload. "
                "A customer can only have one row for the same collateral type across the full collateral register."
            )
        seen_customer_type_keys.add(duplicate_key)
        if _collateral_customer_type_exists(
            state,
            customer_ref_code=customer_ref_code,
            collateral_type=collateral_type,
        ):
            raise ValueError(
                f"Row {index}: customer {customer_ref_code} already has collateral type {collateral_type}. "
                "Edit the existing row instead of uploading an overwrite."
            )

        prepared_rows.append(
            {
                "fic_mis_date": reporting_date,
                "v_cust_ref_code": customer_ref_code,
                "customer_name": (getattr(loan, "v_cust_name", "") or "").strip(),
                "v_branch_name": (getattr(loan, "v_branch_name", "") or "").strip() or state["current_branch"].branch_name,
                "v_branch_code": (getattr(loan, "v_branch_code", "") or "").strip() or state["current_branch"].branch_code,
                "collateral_value": collateral_value,
                "v_ccy_code": currency_code,
                "collateral_type": collateral_type,
                "loan_id": getattr(loan, "v_loan_id", ""),
            }
        )

    created_count = 0
    updated_count = 0
    with transaction.atomic():
        for item in prepared_rows:
            state["collateral_model"].objects.create(
                fic_mis_date=item["fic_mis_date"],
                v_cust_ref_code=item["v_cust_ref_code"],
                customer_name=item["customer_name"],
                v_branch_name=item["v_branch_name"],
                v_branch_code=item["v_branch_code"],
                collateral_value=item["collateral_value"],
                v_ccy_code=item["v_ccy_code"],
                collateral_type=item["collateral_type"],
            )
            created_count += 1
    return created_count, updated_count


def _process_payment_schedule_upload(state: dict, uploaded_file) -> tuple[int, int]:
    rows = _load_uploaded_rows(uploaded_file)
    if state["latest_reporting_date"] is None:
        raise ValueError("No active IFRS9 loans were found for the current branch, so payment schedules cannot be uploaded yet.")

    prepared_rows = []
    for index, row in enumerate(rows, start=2):
        loan = _get_active_loan_by_id(
            state,
            _upload_value(row, "v_loan_id", "loan_id", "loan"),
        )
        if loan is None:
            raise ValueError(f"Row {index}: provide a valid active branch loan id.")

        reporting_date = _parse_optional_date(
            _upload_value(row, "fic_mis_date", "reporting_date", "mis_date", "date"),
            field_label="Reporting Date",
            row_number=index,
        ) or state["latest_reporting_date"]
        cash_flow_date = _parse_optional_date(
            _upload_value(row, "d_cash_flow_date", "cash_flow_date", "payment_date", "repayment_date"),
            field_label="Cash Flow Date",
            row_number=index,
        )
        if cash_flow_date is None:
            raise ValueError(f"Row {index}: Cash Flow Date is required.")

        cash_flow_amount = _parse_optional_decimal(
            _upload_value(
                row,
                "n_cash_flow_amount",
                "cash_flow_amount",
                "total_cash_flow_amount",
                "total_cash_flow",
                "cash_flow",
            ),
            field_label="Total Cash Flow Amount",
            row_number=index,
        )
        if cash_flow_amount is None:
            raise ValueError(f"Row {index}: Total Cash Flow Amount is required.")

        currency_code = " ".join(
            str(
                _upload_value(row, "v_ccy_code", "currency_code", "currency", "ccy_code")
                or getattr(loan, "v_ccy_code", "")
                or ""
            ).strip().upper().split()
        )
        prepared_rows.append(
            {
                "fic_mis_date": reporting_date,
                "v_loan_id": getattr(loan, "v_loan_id", ""),
                "d_cash_flow_date": cash_flow_date,
                "n_cash_flow_amount": cash_flow_amount,
                "v_ccy_code": currency_code,
            }
        )

    created_count = 0
    updated_count = 0
    with transaction.atomic():
        for item in prepared_rows:
            _, created = state["payment_model"].objects.update_or_create(
                fic_mis_date=item["fic_mis_date"],
                v_loan_id=item["v_loan_id"],
                d_cash_flow_date=item["d_cash_flow_date"],
                defaults={
                    "n_cash_flow_amount": item["n_cash_flow_amount"],
                    "v_ccy_code": item["v_ccy_code"],
                },
            )
            if created:
                created_count += 1
            else:
                updated_count += 1
    return created_count, updated_count


def _workspace_shell_context(request: HttpRequest, *, include_loan_details: bool = True) -> dict:
    branch_scope = get_request_branch_scope(request)
    all_branches_selected = is_all_branches_selected(request)
    current_branch = None if all_branches_selected else _resolve_current_branch(request)
    display_branch = _branch_scope_display(current_branch, request)
    if not branch_scope:
        return {
            "current_branch": display_branch if all_branches_selected else None,
            "branch_scope": [],
            "all_branches_selected": all_branches_selected,
            "ifrs9_available": False,
            "workspace_error": "No active branch is available for this user.",
            "forbidden": False,
            "latest_reporting_date": None,
            "active_loans": [],
            "active_loan_count": 0,
            "payment_model": None,
            "collateral_model": None,
            "loan_choice_list": [("", "Select active loan")],
            "customer_choice_list": [("", "Select active customer")],
            "loan_map": {},
            "loan_id_values": [],
            "loan_id_keys": [],
            "loan_id_key_set": set(),
            "normalized_loan_map": {},
            "loan_metadata": {},
            "customer_metadata": {},
            "customer_loan_map": {},
            "customer_ref_keys": [],
            "customer_ref_key_set": set(),
        }

    if (
        not all_branches_selected
        and current_branch is not None
        and not request.user.is_superuser
        and not request.user.has_branch_access(
        branch_id=current_branch.id,
        branch_code=current_branch.branch_code,
        branch_name=current_branch.branch_name,
        )
    ):
        return {
            "current_branch": current_branch,
            "branch_scope": branch_scope,
            "all_branches_selected": all_branches_selected,
            "ifrs9_available": False,
            "workspace_error": "You do not have access to the selected branch.",
            "forbidden": True,
            "latest_reporting_date": None,
            "active_loans": [],
            "active_loan_count": 0,
            "payment_model": None,
            "collateral_model": None,
            "loan_choice_list": [("", "Select active loan")],
            "customer_choice_list": [("", "Select active customer")],
            "loan_map": {},
            "loan_id_values": [],
            "loan_id_keys": [],
            "loan_id_key_set": set(),
            "normalized_loan_map": {},
            "loan_metadata": {},
            "customer_metadata": {},
            "customer_loan_map": {},
            "customer_ref_keys": [],
            "customer_ref_key_set": set(),
        }

    loan_model, payment_model, collateral_model, availability_error = _load_ifrs9_staging_models()
    if availability_error:
        return {
            "current_branch": display_branch,
            "branch_scope": branch_scope,
            "all_branches_selected": all_branches_selected,
            "ifrs9_available": False,
            "workspace_error": availability_error,
            "forbidden": False,
            "latest_reporting_date": None,
            "active_loans": [],
            "active_loan_count": 0,
            "payment_model": None,
            "collateral_model": None,
            "loan_choice_list": [("", "Select active loan")],
            "customer_choice_list": [("", "Select active customer")],
            "loan_map": {},
            "loan_id_values": [],
            "loan_id_keys": [],
            "loan_id_key_set": set(),
            "normalized_loan_map": {},
            "loan_metadata": {},
            "customer_metadata": {},
            "customer_loan_map": {},
            "customer_ref_keys": [],
            "customer_ref_key_set": set(),
        }

    try:
        if include_loan_details:
            if all_branches_selected:
                latest_reporting_date, active_loans = _get_scope_active_loans(branch_scope, loan_model)
            else:
                latest_reporting_date, active_loans = _get_branch_active_loans(current_branch, loan_model)
            active_loan_count = len(active_loans)
            loan_id_keys = sorted(
                {
                    _normalize_text_key(getattr(loan, "v_loan_id", ""))
                    for loan in active_loans
                    if _normalize_text_key(getattr(loan, "v_loan_id", ""))
                }
            )
            customer_ref_keys = sorted(
                {
                    _normalize_customer_reference(getattr(loan, "v_cust_ref_code", ""))
                    for loan in active_loans
                    if _normalize_customer_reference(getattr(loan, "v_cust_ref_code", ""))
                }
            )
        else:
            if all_branches_selected:
                latest_reporting_date, active_loan_count, loan_id_keys, customer_ref_keys = _get_scope_active_loan_keys(
                    branch_scope,
                    loan_model,
                )
            else:
                latest_reporting_date, active_loan_count, loan_id_keys, customer_ref_keys = _get_branch_active_loan_keys(
                    current_branch,
                    loan_model,
                )
            active_loans = []
    except (OperationalError, ProgrammingError):
        return {
            "current_branch": display_branch,
            "branch_scope": branch_scope,
            "all_branches_selected": all_branches_selected,
            "ifrs9_available": False,
            "workspace_error": (
                "The required IFRS9 staging tables are not available in the current database connection."
            ),
            "forbidden": False,
            "latest_reporting_date": None,
            "active_loans": [],
            "active_loan_count": 0,
            "payment_model": None,
            "collateral_model": None,
            "loan_choice_list": [("", "Select active loan")],
            "customer_choice_list": [("", "Select active customer")],
            "loan_map": {},
            "loan_id_keys": [],
            "loan_id_key_set": set(),
            "normalized_loan_map": {},
            "loan_metadata": {},
            "customer_metadata": {},
            "customer_loan_map": {},
            "customer_ref_keys": [],
            "customer_ref_key_set": set(),
        }

    loan_id_values = sorted(
        {
            str(getattr(loan, "v_loan_id", "") or "").strip()
            for loan in active_loans
            if str(getattr(loan, "v_loan_id", "") or "").strip()
        }
    ) or loan_id_keys

    return {
        "current_branch": display_branch,
        "branch_scope": branch_scope,
        "all_branches_selected": all_branches_selected,
        "ifrs9_available": True,
        "workspace_error": "",
        "forbidden": False,
        "latest_reporting_date": latest_reporting_date,
        "active_loans": active_loans,
        "active_loan_count": active_loan_count,
        "payment_model": payment_model,
        "collateral_model": collateral_model,
        "loan_choice_list": _loan_choices(active_loans),
        "customer_choice_list": _customer_choices(active_loans),
        "loan_map": {str(loan.v_loan_id): loan for loan in active_loans},
        "loan_id_values": loan_id_values,
        "loan_id_keys": loan_id_keys,
        "loan_id_key_set": set(loan_id_keys),
        "normalized_loan_map": {
            _normalize_text_key(getattr(loan, "v_loan_id", "")): loan
            for loan in active_loans
            if _normalize_text_key(getattr(loan, "v_loan_id", ""))
        },
        "loan_metadata": _loan_metadata(active_loans),
        "customer_metadata": _customer_metadata(active_loans),
        "customer_loan_map": _loan_customer_map(active_loans),
        "customer_ref_keys": customer_ref_keys,
        "customer_ref_key_set": set(customer_ref_keys),
    }


def _payment_queryset(state: dict, request: HttpRequest | None = None):
    loan_ids = state.get("loan_id_values") or state.get("loan_id_keys") or []
    if not state["payment_model"] or not loan_ids:
        return None
    queryset = (
        state["payment_model"].objects.filter(v_loan_id__in=loan_ids)
        .only(
            "fic_mis_date",
            "v_loan_id",
            "d_cash_flow_date",
            "n_cash_flow_amount",
            "v_ccy_code",
        )
    )
    if request is not None:
        payment_search = (request.GET.get("payment_search") or "").strip()
        if payment_search:
            search_upper = payment_search.upper()
            matching_loan_ids = {
                str(loan_id)
                for loan_id, metadata in (state.get("loan_metadata") or {}).items()
                if search_upper in str(loan_id or "").upper()
                or search_upper in str(metadata.get("customer_name") or "").upper()
                or search_upper in str(metadata.get("product_name") or "").upper()
                or search_upper in str(metadata.get("customer_ref_code") or "").upper()
            }
            queryset = queryset.filter(
                Q(v_loan_id__icontains=payment_search)
                | Q(v_loan_id__in=matching_loan_ids)
            )
    return queryset.order_by("-fic_mis_date", "v_loan_id", "d_cash_flow_date")


def _payment_contract_count(payment_queryset) -> int:
    if payment_queryset is None:
        return 0
    return (
        payment_queryset.exclude(v_loan_id__isnull=True)
        .exclude(v_loan_id="")
        .values("v_loan_id")
        .distinct()
        .count()
    )


def _payment_search_options(state: dict) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    seen_values: set[str] = set()
    for loan_id, metadata in (state.get("loan_metadata") or {}).items():
        loan_id = str(loan_id or "").strip()
        if not loan_id or loan_id in seen_values:
            continue
        seen_values.add(loan_id)
        label_parts = [
            str(metadata.get("customer_name") or "").strip(),
            str(metadata.get("customer_ref_code") or "").strip(),
            str(metadata.get("product_name") or "").strip(),
        ]
        options.append(
            {
                "value": loan_id,
                "label": " | ".join(part for part in label_parts if part),
            }
        )
    return options[:1000]


def _payment_records(state: dict, records) -> list[dict]:
    rows = []
    for record in records:
        loan = state["loan_map"].get(str(record.v_loan_id))
        if loan is None:
            loan = state["normalized_loan_map"].get(_normalize_text_key(record.v_loan_id))
        rows.append(
            {
                "id": record.pk,
                "fic_mis_date": record.fic_mis_date,
                "loan_id": record.v_loan_id,
                "customer_name": (getattr(loan, "v_cust_name", "") or "").strip(),
                "cash_flow_date": record.d_cash_flow_date,
                "cash_flow_amount": record.n_cash_flow_amount,
                "currency_code": record.v_ccy_code,
            }
        )
    return rows


def _collateral_queryset(state: dict, request: HttpRequest | None = None):
    if not state["collateral_model"] or not state["customer_ref_keys"]:
        return None
    queryset = (
        state["collateral_model"].objects.annotate(
            normalized_customer_ref=Upper(Trim("v_cust_ref_code"))
        )
        .filter(normalized_customer_ref__in=state["customer_ref_keys"])
        .only(
            "fic_mis_date",
            "v_cust_ref_code",
            "customer_name",
            "collateral_type",
            "collateral_value",
            "v_ccy_code",
        )
    )
    if request is not None:
        customer_search = (request.GET.get("customer") or "").strip()
        collateral_type = (request.GET.get("collateral_type") or "").strip()
        if customer_search:
            queryset = queryset.filter(
                Q(v_cust_ref_code__icontains=customer_search)
                | Q(customer_name__icontains=customer_search)
            )
        if collateral_type:
            queryset = queryset.filter(collateral_type__iexact=collateral_type)
    return queryset.order_by("-fic_mis_date", "v_cust_ref_code", "collateral_type")


def _collateral_records(state: dict, records) -> list[dict]:
    rows = []
    for record in records:
        linked_loan = state["customer_loan_map"].get(_normalize_customer_reference(record.v_cust_ref_code))
        rows.append(
            {
                "id": record.pk,
                "fic_mis_date": record.fic_mis_date,
                "customer_ref_code": record.v_cust_ref_code,
                "customer_name": record.customer_name,
                "loan_id": getattr(linked_loan, "v_loan_id", ""),
                "collateral_type": record.collateral_type,
                "collateral_value": record.collateral_value,
                "currency_code": record.v_ccy_code,
            }
        )
    return rows


def _supporting_data_diagnostics(
    state: dict,
    *,
    active_tab: str,
    payment_record_count: int,
    collateral_record_count: int,
) -> list[str]:
    diagnostics: list[str] = []
    payment_model = state.get("payment_model")
    collateral_model = state.get("collateral_model")

    if active_tab in {"dashboard", "payment_schedules"} and payment_model and state.get("loan_id_keys") and payment_record_count == 0:
        if payment_model.objects.exists():
            diagnostics.append(
                f"Payment schedules exist in the staging table, but none match the active {state['current_branch'].branch_name} loan ids. "
                "This usually means the loaded loan ids do not match the branch loan snapshot exactly."
            )
        else:
            diagnostics.append(
                "No payment schedule rows were found in the staging table yet."
            )

    if active_tab in {"dashboard", "collateral"} and collateral_model and state.get("customer_ref_keys") and collateral_record_count == 0:
        if collateral_model.objects.exists():
            diagnostics.append(
                f"Collateral rows exist in the staging table, but none match the active {state['current_branch'].branch_name} customer references. "
                "This usually means the loaded customer references do not match the branch loan snapshot exactly."
            )
        else:
            diagnostics.append("No collateral rows were found in the staging table yet.")

    return diagnostics


def _base_template_context(
    state: dict,
    *,
    active_tab: str,
    page_title: str,
    page_note: str,
    payment_record_count: int,
    collateral_record_count: int,
    payment_contract_count: int = 0,
) -> dict:
    return {
        "active_tab": active_tab,
        "page_title": page_title,
        "page_note": page_note,
        "capture_mode": "",
        "current_branch": state["current_branch"],
        "ifrs9_available": state["ifrs9_available"],
        "workspace_error": state["workspace_error"],
        "latest_reporting_date": state["latest_reporting_date"],
        "collateral_capture_date": _collateral_capture_date(),
        "active_loan_count": state["active_loan_count"],
        "loan_metadata_json": json.dumps(state["loan_metadata"] or {}),
        "customer_metadata_json": json.dumps(state["customer_metadata"] or {}),
        "payment_records": [],
        "payment_record_count": payment_record_count,
        "payment_contract_count": payment_contract_count,
        "payment_search_filter": "",
        "payment_search_options": _payment_search_options(state),
        "payment_page_obj": None,
        "collateral_records": [],
        "collateral_record_count": collateral_record_count,
        "collateral_page_obj": None,
        "page_size": DEFAULT_REGISTER_PAGE_SIZE,
        "page_size_options": REGISTER_PAGE_SIZE_OPTIONS,
        "list_query_string": "",
        "list_query_pairs": [],
        "supporting_data_diagnostics": _supporting_data_diagnostics(
            state,
            active_tab=active_tab,
            payment_record_count=payment_record_count,
            collateral_record_count=collateral_record_count,
        ),
        "payment_upload_form": None,
        "collateral_upload_form": None,
        "collateral_upload_columns": _collateral_upload_preview_columns(),
        "payment_upload_columns": _payment_upload_preview_columns(),
        "collateral_customer_filter": "",
        "collateral_type_filter": "",
        "collateral_type_filter_choices": _collateral_type_choices(),
    }


def _render_workspace(request: HttpRequest, state: dict, *, active_tab: str, page_title: str, page_note: str, **extra_context) -> HttpResponse:
    payment_queryset = _payment_queryset(state, request if active_tab == "payment_schedules" else None)
    collateral_queryset = _collateral_queryset(state, request)
    payment_record_count = payment_queryset.count() if payment_queryset is not None else 0
    payment_contract_count = _payment_contract_count(payment_queryset)
    context = _base_template_context(
        state,
        active_tab=active_tab,
        page_title=page_title,
        page_note=page_note,
        payment_record_count=payment_record_count,
        payment_contract_count=payment_contract_count,
        collateral_record_count=collateral_queryset.count() if collateral_queryset is not None else 0,
    )
    context["can_manage_supporting_data"] = (
        request.user.is_superuser
        or request.user.has_perm("scorecard.manage_ifrs9_supporting_data")
    )
    if active_tab == "collateral":
        context["collateral_customer_filter"] = (request.GET.get("customer") or "").strip()
        context["collateral_type_filter"] = (request.GET.get("collateral_type") or "").strip()
    if active_tab == "payment_schedules":
        context["payment_search_filter"] = (request.GET.get("payment_search") or "").strip()

    page_size = _parse_register_page_size(request)
    if active_tab == "collateral" and collateral_queryset is not None:
        collateral_page_obj = Paginator(collateral_queryset, page_size).get_page(request.GET.get("page"))
        context.update(
            {
                "collateral_records": _collateral_records(state, collateral_page_obj.object_list),
                "collateral_page_obj": collateral_page_obj,
                "page_size": page_size,
                "list_query_string": _build_list_query_string(request, "page"),
                "list_query_pairs": _build_list_query_pairs(request, "page", "page_size"),
            }
        )
    elif active_tab == "payment_schedules" and payment_queryset is not None:
        payment_page_obj = Paginator(payment_queryset, page_size).get_page(request.GET.get("page"))
        context.update(
            {
                "payment_records": _payment_records(state, payment_page_obj.object_list),
                "payment_page_obj": payment_page_obj,
                "page_size": page_size,
                "list_query_string": _build_list_query_string(request, "page"),
                "list_query_pairs": _build_list_query_pairs(request, "page", "page_size"),
            }
        )

    context.update(extra_context)
    return render(request, "credit_scoreshifts/ifrs9_supporting_data.html", context)


def _redirect_for_invalid_workspace(request: HttpRequest, state: dict):
    if state.get("forbidden"):
        messages.error(request, state["workspace_error"])
        return redirect("scorecard:scorecard_dashboard")
    if state.get("workspace_error"):
        messages.error(request, state["workspace_error"])
    return None


def _redirect_if_all_branches_selected_for_supporting_data_entry(request: HttpRequest, redirect_url_name: str):
    if not is_all_branches_selected(request):
        return None
    messages.error(
        request,
        "Choose one specific branch before capturing or editing IFRS9 supporting data. All Branches mode is for combined views only.",
    )
    return redirect(redirect_url_name)


def _resolve_capture_mode(request: HttpRequest) -> str:
    mode = (request.GET.get("mode") or "").strip().lower()
    if mode in {"form", "upload"}:
        return mode
    return "form"


def _is_ajax_request(request: HttpRequest) -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _form_error_payload(form: forms.Form) -> dict:
    return {
        "field_errors": {
            field_name: [str(error) for error in errors]
            for field_name, errors in form.errors.items()
            if field_name != "__all__"
        },
        "non_field_errors": [str(error) for error in form.non_field_errors()],
    }


def _get_branch_collateral_record(state: dict, record_id: int):
    if not state["collateral_model"] or not state["customer_ref_key_set"]:
        return None

    record = state["collateral_model"].objects.filter(pk=record_id).first()
    if record is None:
        return None

    normalized_customer_ref = _normalize_customer_reference(record.v_cust_ref_code)
    if normalized_customer_ref not in state["customer_ref_key_set"]:
        return None
    return record


def _get_branch_payment_record(state: dict, record_id: int):
    if not state["payment_model"] or not state["loan_id_key_set"]:
        return None

    record = state["payment_model"].objects.filter(pk=record_id).first()
    if record is None:
        return None

    normalized_loan_id = _normalize_text_key(record.v_loan_id)
    if normalized_loan_id not in state["loan_id_key_set"]:
        return None
    return record


@login_required
@permission_required("scorecard.manage_ifrs9_supporting_data", raise_exception=True)
def ifrs9_supporting_data_view(request: HttpRequest) -> HttpResponse:
    state = _workspace_shell_context(request, include_loan_details=False)
    redirect_response = _redirect_for_invalid_workspace(request, state)
    if redirect_response:
        return redirect_response

    if state["ifrs9_available"] and state["latest_reporting_date"] is None:
        messages.warning(
            request,
            "No active IFRS9 loans were found for the selected branch scope. Load the branch loan snapshot first, then return here.",
        )

    payment_queryset = _payment_queryset(state)
    collateral_queryset = _collateral_queryset(state)
    payment_contract_count = _payment_contract_count(payment_queryset)
    dashboard_highlights = [
        {
            "title": "Collateral Records",
            "value": collateral_queryset.count() if collateral_queryset is not None else 0,
            "copy": "Customer-level collateral entries available for the selected branch scope across all loaded reporting dates.",
            "url": "scorecard:ifrs9_supporting_data_collateral",
            "cta": "Open collateral register",
        },
        {
            "title": "Payment Schedule Contracts",
            "value": payment_contract_count,
            "copy": "Distinct loan IDs with payment schedule rows available for the selected branch scope.",
            "url": "scorecard:ifrs9_supporting_data_payment_schedules",
            "cta": "Open payment schedules",
        },
    ]

    return _render_workspace(
        request,
        state,
        active_tab="dashboard",
        page_title="IFRS9 Supporting Data Dashboard",
        page_note="Monitor branch-scoped supporting data and jump into the maintenance registers from one control point.",
        show_dashboard=True,
        dashboard_highlights=dashboard_highlights,
        show_add_form=False,
        payment_form=None,
        collateral_form=None,
    )


@login_required
@permission_required("scorecard.manage_ifrs9_supporting_data", raise_exception=True)
def ifrs9_supporting_data_collateral_list_view(request: HttpRequest) -> HttpResponse:
    state = _workspace_shell_context(request, include_loan_details=False)
    redirect_response = _redirect_for_invalid_workspace(request, state)
    if redirect_response:
        return redirect_response

    return _render_workspace(
        request,
        state,
        active_tab="collateral",
        page_title="Collateral Register",
        page_note="Review all branch-visible collateral rows stored in `IFRS9.stg_collateral_data`, with the latest reporting dates shown first.",
        show_dashboard=False,
        show_add_form=False,
        collateral_form=BranchCollateralForm(customer_choices=state["customer_choice_list"], prefix="collateral"),
        payment_form=None,
    )


@login_required
@permission_required("scorecard.manage_ifrs9_supporting_data", raise_exception=True)
def ifrs9_supporting_data_collateral_add_view(request: HttpRequest) -> HttpResponse:
    all_branches_redirect = _redirect_if_all_branches_selected_for_supporting_data_entry(
        request,
        "scorecard:ifrs9_supporting_data_collateral",
    )
    if all_branches_redirect:
        return all_branches_redirect
    state = _workspace_shell_context(request)
    redirect_response = _redirect_for_invalid_workspace(request, state)
    if redirect_response:
        return redirect_response

    capture_mode = _resolve_capture_mode(request)
    form = BranchCollateralForm(
        request.POST or None,
        customer_choices=state["customer_choice_list"],
        prefix="collateral",
    )
    upload_form = SupportingDataUploadForm(
        request.POST or None,
        request.FILES or None,
        prefix="collateral_upload",
        expected_columns=_collateral_upload_preview_columns(),
    )

    if request.method == "POST":
        if state["latest_reporting_date"] is None:
            messages.error(
                request,
                "No active IFRS9 customers were found for the current branch, so collateral cannot be captured yet.",
            )
            return redirect("scorecard:ifrs9_supporting_data_collateral")

        if capture_mode == "upload":
            if upload_form.is_valid():
                try:
                    created_count, updated_count = _process_collateral_upload(
                        state,
                        upload_form.cleaned_data["file"],
                    )
                except ValueError as exc:
                    upload_form.add_error("file", str(exc))
                else:
                    total_rows = created_count + updated_count
                    messages.success(
                        request,
                        f"Collateral upload completed successfully: {total_rows} rows processed, {created_count} created, {updated_count} updated.",
                    )
                    log_scorecard_audit(
                        request.user,
                        "ScorecardIFRS9SupportingData",
                        "upload",
                        object_id=f"collateral_upload:{state['current_branch'].branch_code}",
                        change_description=(
                            f"Collateral upload for branch {state['current_branch'].branch_name}; "
                            f"{created_count} created and {updated_count} updated."
                        ),
                    )
                    return redirect("scorecard:ifrs9_supporting_data_collateral")
        elif form.is_valid():
            customer_ref_code = (form.cleaned_data["v_cust_ref_code"] or "").strip()
            loan = _get_active_customer_loan_by_ref(state, customer_ref_code)
            if loan is None:
                form.add_error(
                    "v_cust_ref_code",
                    "Select a valid active customer before saving collateral.",
                )
            else:
                currency_code = (
                    form.cleaned_data["v_ccy_code"]
                    or (getattr(loan, "v_ccy_code", "") or "").strip().upper()
                )
                branch_name = (getattr(loan, "v_branch_name", "") or "").strip() or state["current_branch"].branch_name
                branch_code = (getattr(loan, "v_branch_code", "") or "").strip() or state["current_branch"].branch_code
                customer_name = (getattr(loan, "v_cust_name", "") or "").strip()
                capture_date = _collateral_capture_date()
                with transaction.atomic():
                    if _collateral_customer_type_exists(
                        state,
                        customer_ref_code=customer_ref_code,
                        collateral_type=form.cleaned_data["collateral_type"],
                    ):
                        form.add_error(
                            "collateral_type",
                            (
                                "This customer already has this collateral type in the collateral register. "
                                "Open the existing row and edit it if you want to change the value."
                            ),
                        )
                    else:
                        record = state["collateral_model"].objects.create(
                            fic_mis_date=capture_date,
                            v_cust_ref_code=customer_ref_code,
                            customer_name=customer_name,
                            v_branch_name=branch_name,
                            v_branch_code=branch_code,
                            collateral_value=form.cleaned_data["collateral_value"],
                            v_ccy_code=currency_code,
                            collateral_type=form.cleaned_data["collateral_type"],
                        )
                if form.errors:
                    return _render_workspace(
                        request,
                        state,
                        active_tab="collateral",
                        page_title="Collateral Register",
                        page_note="Capture or update customer-level collateral for active customers in the current branch. Existing branch collateral is listed across all reporting dates, newest first.",
                        show_dashboard=False,
                        show_add_form=True,
                        capture_mode=capture_mode,
                        collateral_form=form,
                        collateral_upload_form=upload_form,
                        payment_form=None,
                    )
                messages.success(
                    request,
                    f"Collateral record created successfully for customer {customer_ref_code} / {form.cleaned_data['collateral_type']}.",
                )
                log_scorecard_audit(
                    request.user,
                    "ScorecardIFRS9SupportingData",
                    "create",
                    object_id=f"collateral:{customer_ref_code}:{form.cleaned_data['collateral_type']}:{capture_date}",
                    change_description=(
                        f"Branch {state['current_branch'].branch_name}; capture date {capture_date}; "
                        f"collateral record created for customer {customer_ref_code} and type {form.cleaned_data['collateral_type']}."
                    ),
                )
                return redirect("scorecard:ifrs9_supporting_data_collateral")

    return _render_workspace(
        request,
        state,
        active_tab="collateral",
        page_title="Collateral Register",
        page_note="Capture or update customer-level collateral for active customers in the current branch. Existing branch collateral is listed across all reporting dates, newest first.",
        show_dashboard=False,
        show_add_form=True,
        capture_mode=capture_mode,
        collateral_form=form,
        collateral_upload_form=upload_form,
        payment_form=None,
    )


@login_required
@permission_required("scorecard.manage_ifrs9_supporting_data", raise_exception=True)
def ifrs9_supporting_data_collateral_edit_view(request: HttpRequest, record_id: int) -> HttpResponse:
    all_branches_redirect = _redirect_if_all_branches_selected_for_supporting_data_entry(
        request,
        "scorecard:ifrs9_supporting_data_collateral",
    )
    if all_branches_redirect:
        return all_branches_redirect
    state = _workspace_shell_context(request)
    redirect_response = _redirect_for_invalid_workspace(request, state)
    if redirect_response:
        return redirect_response

    record = _get_branch_collateral_record(state, record_id)
    if record is None:
        messages.error(request, "The selected collateral record is no longer available in the active branch workspace.")
        return redirect("scorecard:ifrs9_supporting_data_collateral")

    linked_loan = state["customer_loan_map"].get(_normalize_customer_reference(record.v_cust_ref_code))
    initial = {
        "v_cust_ref_code": record.v_cust_ref_code,
        "collateral_value": record.collateral_value,
        "collateral_type": record.collateral_type,
        "v_ccy_code": record.v_ccy_code,
    }
    form = BranchCollateralForm(
        request.POST or None,
        initial=initial,
        customer_choices=state["customer_choice_list"],
        prefix="collateral",
    )

    if request.method == "POST" and form.is_valid():
        customer_ref_code = (form.cleaned_data["v_cust_ref_code"] or "").strip()
        loan = _get_active_customer_loan_by_ref(state, customer_ref_code)
        if loan is None:
            form.add_error(
                "v_cust_ref_code",
                "Select a valid active customer before saving collateral.",
            )
        else:
            if _collateral_customer_type_exists(
                state,
                customer_ref_code=customer_ref_code,
                collateral_type=form.cleaned_data["collateral_type"],
                exclude_pk=record.pk,
            ):
                form.add_error(
                    "collateral_type",
                    "A collateral record already exists for that customer and collateral type. Open that row instead of overwriting it.",
                )
            else:
                record.v_cust_ref_code = customer_ref_code
                record.customer_name = (getattr(loan, "v_cust_name", "") or "").strip()
                record.v_branch_name = (getattr(loan, "v_branch_name", "") or "").strip() or state["current_branch"].branch_name
                record.v_branch_code = (getattr(loan, "v_branch_code", "") or "").strip() or state["current_branch"].branch_code
                record.collateral_value = form.cleaned_data["collateral_value"]
                record.collateral_type = form.cleaned_data["collateral_type"]
                record.v_ccy_code = (
                    form.cleaned_data["v_ccy_code"]
                    or (getattr(loan, "v_ccy_code", "") or "").strip().upper()
                )
                record.save()
                success_message = f"Collateral record updated successfully for customer {record.v_cust_ref_code}."
                messages.success(request, success_message)
                log_scorecard_audit(
                    request.user,
                    "ScorecardIFRS9SupportingData",
                    "update",
                    object_id=f"collateral:{record.v_cust_ref_code}:{record.fic_mis_date}",
                    change_description=(
                        f"Branch {state['current_branch'].branch_name}; reporting date {record.fic_mis_date}; "
                        f"collateral record updated for customer {record.v_cust_ref_code}."
                    ),
                )
                if _is_ajax_request(request):
                    return JsonResponse({"success": True, "message": success_message})
                return redirect("scorecard:ifrs9_supporting_data_collateral")

    if request.method == "POST" and _is_ajax_request(request):
        return JsonResponse({"success": False, **_form_error_payload(form)}, status=400)

    return _render_workspace(
        request,
        state,
        active_tab="collateral",
        page_title="Edit Collateral Record",
        page_note="Adjust the selected collateral row while preserving its original reporting date. Reassigning it to another customer is blocked when that customer already has a row for the same date.",
        show_dashboard=False,
        show_add_form=True,
        capture_mode="form",
        collateral_form=form,
        collateral_upload_form=None,
        payment_form=None,
        editing_record={
            "type": "collateral",
            "label": record.v_cust_ref_code,
            "date": record.fic_mis_date,
        },
    )


@login_required
@permission_required("scorecard.manage_ifrs9_supporting_data", raise_exception=True)
def ifrs9_supporting_data_collateral_delete_view(request: HttpRequest, record_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    all_branches_redirect = _redirect_if_all_branches_selected_for_supporting_data_entry(
        request,
        "scorecard:ifrs9_supporting_data_collateral",
    )
    if all_branches_redirect:
        return all_branches_redirect
    state = _workspace_shell_context(request)
    redirect_response = _redirect_for_invalid_workspace(request, state)
    if redirect_response:
        return redirect_response

    record = _get_branch_collateral_record(state, record_id)
    if record is None:
        messages.error(request, "The selected collateral record is no longer available in the active branch workspace.")
        return redirect("scorecard:ifrs9_supporting_data_collateral")

    customer_ref_code = record.v_cust_ref_code
    reporting_date = record.fic_mis_date
    record.delete()
    messages.success(
        request,
        f"Collateral record deleted successfully for customer {customer_ref_code}.",
    )
    log_scorecard_audit(
        request.user,
        "ScorecardIFRS9SupportingData",
        "delete",
        object_id=f"collateral:{customer_ref_code}:{reporting_date}",
        change_description=(
            f"Branch {state['current_branch'].branch_name}; reporting date {reporting_date}; "
            f"collateral record deleted for customer {customer_ref_code}."
        ),
    )
    return redirect("scorecard:ifrs9_supporting_data_collateral")


@login_required
@permission_required("scorecard.manage_ifrs9_supporting_data", raise_exception=True)
def ifrs9_supporting_data_payment_schedule_list_view(request: HttpRequest) -> HttpResponse:
    state = _workspace_shell_context(request, include_loan_details=True)
    redirect_response = _redirect_for_invalid_workspace(request, state)
    if redirect_response:
        return redirect_response

    return _render_workspace(
        request,
        state,
        active_tab="payment_schedules",
        page_title="Payment Schedule Register",
        page_note="Review all branch-scoped payment schedule rows stored in `IFRS9.stg_payment_schedule`, with the latest reporting dates shown first.",
        show_dashboard=False,
        show_add_form=False,
        payment_form=BranchPaymentScheduleForm(loan_choices=state["loan_choice_list"], prefix="payment"),
        collateral_form=None,
    )


@login_required
@permission_required("scorecard.manage_ifrs9_supporting_data", raise_exception=True)
def ifrs9_supporting_data_payment_schedule_add_view(request: HttpRequest) -> HttpResponse:
    all_branches_redirect = _redirect_if_all_branches_selected_for_supporting_data_entry(
        request,
        "scorecard:ifrs9_supporting_data_payment_schedules",
    )
    if all_branches_redirect:
        return all_branches_redirect
    state = _workspace_shell_context(request)
    redirect_response = _redirect_for_invalid_workspace(request, state)
    if redirect_response:
        return redirect_response

    capture_mode = _resolve_capture_mode(request)
    form = BranchPaymentScheduleBatchForm(
        request.POST or None,
        loan_choices=state["loan_choice_list"],
        prefix="payment",
    )
    payment_schedule_rows = [
        {
            "d_cash_flow_date": "",
            "n_cash_flow_amount": "",
            "errors": [],
        }
    ]
    upload_form = SupportingDataUploadForm(
        request.POST or None,
        request.FILES or None,
        prefix="payment_upload",
        expected_columns=_payment_upload_preview_columns(),
    )

    if request.method == "POST":
        if state["latest_reporting_date"] is None:
            messages.error(
                request,
                "No active IFRS9 loans were found for the current branch, so payment schedules cannot be captured yet.",
            )
            return redirect("scorecard:ifrs9_supporting_data_payment_schedules")

        if capture_mode == "upload":
            if upload_form.is_valid():
                try:
                    created_count, updated_count = _process_payment_schedule_upload(
                        state,
                        upload_form.cleaned_data["file"],
                    )
                except ValueError as exc:
                    upload_form.add_error("file", str(exc))
                else:
                    total_rows = created_count + updated_count
                    messages.success(
                        request,
                        f"Payment schedule upload completed successfully: {total_rows} rows processed, {created_count} created, {updated_count} updated.",
                    )
                    log_scorecard_audit(
                        request.user,
                        "ScorecardIFRS9SupportingData",
                        "upload",
                        object_id=f"payment_upload:{state['current_branch'].branch_code}",
                        change_description=(
                            f"Payment schedule upload for branch {state['current_branch'].branch_name}; "
                            f"{created_count} created and {updated_count} updated."
                        ),
                    )
                    return redirect("scorecard:ifrs9_supporting_data_payment_schedules")
        elif form.is_valid():
            prepared_rows, payment_schedule_rows = _parse_payment_schedule_batch_rows(request.POST)
            has_row_errors = any(row.get("errors") for row in payment_schedule_rows)
            if has_row_errors:
                form.add_error(None, "Please correct the highlighted cash flow rows before saving.")
            elif not prepared_rows:
                form.add_error(None, "Add at least one cash flow row before saving.")
            else:
                loan = state["loan_map"].get(form.cleaned_data["v_loan_id"])
                currency_code = (
                    form.cleaned_data["v_ccy_code"]
                    or (getattr(loan, "v_ccy_code", "") or "").strip().upper()
                )
                created_count = 0
                updated_count = 0
                with transaction.atomic():
                    for row in prepared_rows:
                        _, created = state["payment_model"].objects.update_or_create(
                            fic_mis_date=state["latest_reporting_date"],
                            v_loan_id=loan.v_loan_id,
                            d_cash_flow_date=row["d_cash_flow_date"],
                            defaults={
                                "n_cash_flow_amount": row.get("n_cash_flow_amount"),
                                "v_ccy_code": currency_code,
                            },
                        )
                        if created:
                            created_count += 1
                        else:
                            updated_count += 1
                messages.success(
                    request,
                    f"Payment schedule saved successfully for loan {loan.v_loan_id}: {created_count} created, {updated_count} updated.",
                )
                log_scorecard_audit(
                    request.user,
                    "ScorecardIFRS9SupportingData",
                    "create",
                    object_id=f"payment:{loan.v_loan_id}:{state['latest_reporting_date']}",
                    change_description=(
                        f"Branch {state['current_branch'].branch_name}; reporting date {state['latest_reporting_date']}; "
                        f"payment schedule batch saved for loan {loan.v_loan_id}: {created_count} created, {updated_count} updated."
                    ),
                )
                return redirect("scorecard:ifrs9_supporting_data_payment_schedules")
        elif request.method == "POST" and capture_mode == "form":
            _, payment_schedule_rows = _parse_payment_schedule_batch_rows(request.POST)

    return _render_workspace(
        request,
        state,
        active_tab="payment_schedules",
        page_title="Payment Schedule Register",
        page_note="Capture or update loan-level payment schedules for active loans in the current branch. Existing schedules are listed across all reporting dates, newest first.",
        show_dashboard=False,
        show_add_form=True,
        capture_mode=capture_mode,
        payment_form=form,
        payment_upload_form=upload_form,
        payment_schedule_rows=payment_schedule_rows,
        collateral_form=None,
    )


@login_required
@permission_required("scorecard.manage_ifrs9_supporting_data", raise_exception=True)
def ifrs9_supporting_data_payment_schedule_edit_view(request: HttpRequest, record_id: int) -> HttpResponse:
    all_branches_redirect = _redirect_if_all_branches_selected_for_supporting_data_entry(
        request,
        "scorecard:ifrs9_supporting_data_payment_schedules",
    )
    if all_branches_redirect:
        return all_branches_redirect
    state = _workspace_shell_context(request)
    redirect_response = _redirect_for_invalid_workspace(request, state)
    if redirect_response:
        return redirect_response

    record = _get_branch_payment_record(state, record_id)
    if record is None:
        messages.error(request, "The selected payment schedule row is no longer available in the active branch workspace.")
        return redirect("scorecard:ifrs9_supporting_data_payment_schedules")

    initial = {
        "v_loan_id": record.v_loan_id,
        "d_cash_flow_date": record.d_cash_flow_date,
        "n_cash_flow_amount": record.n_cash_flow_amount,
        "v_ccy_code": record.v_ccy_code,
    }
    form = BranchPaymentScheduleForm(
        request.POST or None,
        initial=initial,
        loan_choices=state["loan_choice_list"],
        prefix="payment",
    )

    if request.method == "POST" and form.is_valid():
        loan = state["loan_map"].get(form.cleaned_data["v_loan_id"])
        conflicting_record = state["payment_model"].objects.filter(
            fic_mis_date=record.fic_mis_date,
            v_loan_id=loan.v_loan_id,
            d_cash_flow_date=form.cleaned_data["d_cash_flow_date"],
        ).exclude(pk=record.pk).first()
        if conflicting_record is not None:
            form.add_error(
                "d_cash_flow_date",
                "A payment schedule row already exists for this loan, reporting date, and cash flow date.",
            )
        else:
            record.v_loan_id = loan.v_loan_id
            record.d_cash_flow_date = form.cleaned_data["d_cash_flow_date"]
            record.n_cash_flow_amount = form.cleaned_data.get("n_cash_flow_amount")
            record.v_ccy_code = (
                form.cleaned_data["v_ccy_code"]
                or (getattr(loan, "v_ccy_code", "") or "").strip().upper()
            )
            record.save()
            success_message = (
                f"Payment schedule entry updated successfully for loan {record.v_loan_id} on {record.d_cash_flow_date}."
            )
            messages.success(request, success_message)
            log_scorecard_audit(
                request.user,
                "ScorecardIFRS9SupportingData",
                "update",
                object_id=f"payment:{record.v_loan_id}:{record.d_cash_flow_date}",
                change_description=(
                    f"Branch {state['current_branch'].branch_name}; reporting date {record.fic_mis_date}; "
                    f"payment schedule updated for loan {record.v_loan_id}."
                ),
            )
            if _is_ajax_request(request):
                return JsonResponse({"success": True, "message": success_message})
            return redirect("scorecard:ifrs9_supporting_data_payment_schedules")

    if request.method == "POST" and _is_ajax_request(request):
        return JsonResponse({"success": False, **_form_error_payload(form)}, status=400)

    return _render_workspace(
        request,
        state,
        active_tab="payment_schedules",
        page_title="Edit Payment Schedule Row",
        page_note="Adjust the selected payment schedule row while preserving its original reporting date. The system blocks duplicates on the same loan and cash flow date.",
        show_dashboard=False,
        show_add_form=True,
        capture_mode="form",
        payment_form=form,
        payment_upload_form=None,
        collateral_form=None,
        editing_record={
            "type": "payment",
            "label": record.v_loan_id,
            "date": record.fic_mis_date,
        },
    )


@login_required
@permission_required("scorecard.manage_ifrs9_supporting_data", raise_exception=True)
def ifrs9_supporting_data_payment_schedule_delete_view(request: HttpRequest, record_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    all_branches_redirect = _redirect_if_all_branches_selected_for_supporting_data_entry(
        request,
        "scorecard:ifrs9_supporting_data_payment_schedules",
    )
    if all_branches_redirect:
        return all_branches_redirect
    state = _workspace_shell_context(request)
    redirect_response = _redirect_for_invalid_workspace(request, state)
    if redirect_response:
        return redirect_response

    record = _get_branch_payment_record(state, record_id)
    if record is None:
        messages.error(request, "The selected payment schedule row is no longer available in the active branch workspace.")
        return redirect("scorecard:ifrs9_supporting_data_payment_schedules")

    loan_id = record.v_loan_id
    cash_flow_date = record.d_cash_flow_date
    reporting_date = record.fic_mis_date
    record.delete()
    messages.success(
        request,
        f"Payment schedule entry deleted successfully for loan {loan_id} on {cash_flow_date}.",
    )
    log_scorecard_audit(
        request.user,
        "ScorecardIFRS9SupportingData",
        "delete",
        object_id=f"payment:{loan_id}:{cash_flow_date}",
        change_description=(
            f"Branch {state['current_branch'].branch_name}; reporting date {reporting_date}; "
            f"payment schedule deleted for loan {loan_id} on {cash_flow_date}."
        ),
    )
    return redirect("scorecard:ifrs9_supporting_data_payment_schedules")


@login_required
@permission_required("scorecard.manage_ifrs9_supporting_data", raise_exception=True)
def ifrs9_supporting_data_collateral_template_view(request: HttpRequest) -> HttpResponse:
    all_branches_redirect = _redirect_if_all_branches_selected_for_supporting_data_entry(
        request,
        "scorecard:ifrs9_supporting_data_collateral",
    )
    if all_branches_redirect:
        return all_branches_redirect
    state = _workspace_shell_context(request)
    redirect_response = _redirect_for_invalid_workspace(request, state)
    if redirect_response:
        return redirect_response

    sample_loan = state["active_loans"][0] if state["active_loans"] else None
    sample_reporting_date = _collateral_capture_date()
    sample_row = [
        getattr(sample_loan, "v_cust_ref_code", "") or "",
        getattr(sample_loan, "v_loan_id", "") or "LN0001",
        str(sample_reporting_date or ""),
        "250000.00",
        "PROPERTY",
        getattr(sample_loan, "v_ccy_code", "") or "USD",
    ]
    notes = [
        "Use the headers on the Template sheet exactly as provided.",
        "Provide customer_ref_code for every collateral row. loan_id is optional and only used as a fallback lookup for older files.",
        "reporting_date is optional. If left blank during upload, today's capture date will be used.",
        "collateral_value and collateral_type are required.",
        f"This template is intended for branch: {(state['current_branch'].branch_name if state['current_branch'] else 'Current branch')}.",
    ]
    log_scorecard_audit(
        request.user,
        "ScorecardIFRS9SupportingData",
        "download",
        object_id=f"collateral_template:{state['current_branch'].branch_code if state['current_branch'] else 'unknown'}",
        change_description=(
            f"Collateral upload template downloaded for branch "
            f"{state['current_branch'].branch_name if state['current_branch'] else 'Unknown branch'}."
        ),
    )
    return _build_supporting_data_template_response(
        filename="ifrs9_collateral_upload_template.xlsx",
        sheet_title="Collateral Template",
        headers=COLLATERAL_UPLOAD_TEMPLATE_HEADERS,
        sample_row=sample_row,
        notes=notes,
    )


@login_required
@permission_required("scorecard.manage_ifrs9_supporting_data", raise_exception=True)
def ifrs9_supporting_data_payment_schedule_template_view(request: HttpRequest) -> HttpResponse:
    all_branches_redirect = _redirect_if_all_branches_selected_for_supporting_data_entry(
        request,
        "scorecard:ifrs9_supporting_data_payment_schedules",
    )
    if all_branches_redirect:
        return all_branches_redirect
    state = _workspace_shell_context(request)
    redirect_response = _redirect_for_invalid_workspace(request, state)
    if redirect_response:
        return redirect_response

    sample_loan = state["active_loans"][0] if state["active_loans"] else None
    sample_reporting_date = state["latest_reporting_date"]
    sample_row = [
        getattr(sample_loan, "v_loan_id", "") or "LN0001",
        str(sample_reporting_date or ""),
        str(sample_reporting_date or ""),
        "11500.00",
        getattr(sample_loan, "v_ccy_code", "") or "USD",
    ]
    notes = [
        "Use the headers on the Template sheet exactly as provided.",
        "loan_id, cash_flow_date, and total_cash_flow_amount are required.",
        "reporting_date is optional. If left blank during upload, the active branch reporting date will be used.",
        f"This template is intended for branch: {(state['current_branch'].branch_name if state['current_branch'] else 'Current branch')}.",
    ]
    log_scorecard_audit(
        request.user,
        "ScorecardIFRS9SupportingData",
        "download",
        object_id=f"payment_template:{state['current_branch'].branch_code if state['current_branch'] else 'unknown'}",
        change_description=(
            f"Payment schedule upload template downloaded for branch "
            f"{state['current_branch'].branch_name if state['current_branch'] else 'Unknown branch'}."
        ),
    )
    return _build_supporting_data_template_response(
        filename="ifrs9_payment_schedule_upload_template.xlsx",
        sheet_title="Payment Schedule Template",
        headers=PAYMENT_UPLOAD_TEMPLATE_HEADERS,
        sample_row=sample_row,
        notes=notes,
    )
