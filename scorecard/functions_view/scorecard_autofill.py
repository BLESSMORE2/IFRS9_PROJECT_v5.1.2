from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Iterable

from django.db.models import Max

from scorecard.models import (
    Attribute,
    CustomerCorporate,
    CustomerLoan,
    IFRS9Attribute,
    IFRS9Option,
    MainCustomer,
    Option,
)


BASEL_INDIVIDUAL_TEMPLATE_CODE = "AICSC1-17"
IFRS9_CONSUMER_TEMPLATE_CODE = "IFRS9PD-CONSUMER-001"

BASEL_CORPORATE_TEMPLATE_CODES = {
    "ARCSC1-17",
    "ACCSC1-17",
    "AFCSC1-17",
}

IFRS9_CORPORATE_TEMPLATE_CODES = {
    "IFRS9PD-CORPORATE-001",
    "IFRS9PD-FARMING-001",
    "IFRS9PD-LOCALAUTHORITIES-001",
    "IFRS9PD-MFINANCE-001",
    "IFRS9PD-RETAIL-001",
    "IFRS9PD-SCHOOLS-001",
    "IFRS9PD-TERTIARY-001",
}

GENDER_AUTOFILL_MAPPING = {
    "F": "FEMALE",
    "FEMALE": "FEMALE",
    "WOMAN": "FEMALE",
    "M": "MALE",
    "MALE": "MALE",
    "MAN": "MALE",
}

RESIDENCE_STATUS_AUTOFILL_MAPPING = {
    "R": ("PERMANENT",),
    "P": ("PERMANENT",),
    "PERM": ("PERMANENT",),
    "PERMANENT": ("PERMANENT",),
    "RESIDENT": ("PERMANENT",),
    "T": ("TEMPORARY",),
    "TEMP": ("TEMPORARY",),
    "TEMPORARY": ("TEMPORARY",),
    "N": ("NON", "RESIDENT"),
    "NR": ("NON", "RESIDENT"),
    "N/R": ("NON", "RESIDENT"),
    "NON RESIDENT": ("NON", "RESIDENT"),
    "NON-RESIDENT": ("NON", "RESIDENT"),
}


@dataclass
class AutofillResult:
    attribute_values: dict[int, str]
    applied_labels: list[str]
    applied_attribute_ids: list[int]
    missing_required_labels: list[str]
    missing_required_attribute_ids: list[int]
    profile_snapshot: dict[str, str]


PROFILE_SNAPSHOT_FIELDS: tuple[tuple[str, str], ...] = (
    ("gender", "Gender"),
    ("birth_date", "Birth Date"),
    ("marital_status", "Marital Status"),
    ("resident_status", "Residence Status"),
    ("nationality_code", "Nationality"),
    ("employment_type", "Employment Type"),
    ("occupation_code", "Occupation Code"),
    ("annual_income", "Annual Income"),
    ("income_slab", "Income Slab"),
    ("accommodation_type", "Accommodation Type"),
    ("designation_code", "Designation Code"),
    ("work_sector_code", "Work Sector Code"),
    ("employer_code", "Employer Code"),
    ("employer_name", "Employer Name"),
    ("bank_relationship_flag", "Bank Relationship Flag"),
    ("pension_flag", "Pension Flag"),
    ("source_of_funds", "Source Of Funds"),
    ("account_purpose", "Account Purpose"),
)

CORPORATE_PROFILE_SNAPSHOT_FIELDS: tuple[tuple[str, str], ...] = (
    ("client_name", "Corporate Customer Name"),
    ("organization_qualifier", "Organization Qualifier"),
    ("industry_code", "Industry Code"),
    ("sub_industry_code", "Sub-Industry Code"),
    ("nature_of_business_1", "Nature Of Business 1"),
    ("nature_of_business_2", "Nature Of Business 2"),
    ("nature_of_business_3", "Nature Of Business 3"),
    ("incorporation_date", "Incorporation Date"),
    ("registration_number", "Registration Number"),
    ("registration_date", "Registration Date"),
    ("years_in_business", "Years In Business"),
    ("gross_turnover", "Gross Turnover"),
    ("employee_size", "Employee Size"),
    ("number_of_offices", "Number Of Offices"),
    ("is_sovereign", "Sovereign Flag"),
    ("sovereign_type", "Sovereign Type"),
    ("is_central_state", "Central State Flag"),
    ("is_public_sector", "Public Sector Flag"),
    ("purpose_of_account_opening", "Purpose Of Account Opening"),
)

LOAN_PROFILE_SNAPSHOT_FIELDS: tuple[tuple[str, str], ...] = (
    ("loan_reporting_date", "Loan Reporting Date"),
    ("loan_id", "Loan ID"),
    ("loan_amount", "Loan Amount"),
    ("collateral_amount", "Collateral Value"),
    ("start_date", "Loan Start Date"),
    ("maturity_date", "Loan Maturity Date"),
    ("overdue_amount", "Overdue Amount"),
    ("last_payment_date", "Last Payment Date"),
    ("delinquent_days", "Delinquent Days"),
)


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().upper().split())


def _serialize_snapshot_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def build_customer_profile_snapshot(customer: MainCustomer | None) -> dict[str, str]:
    if customer is None:
        return {}
    snapshot: dict[str, str] = {}
    for field_name, _ in PROFILE_SNAPSHOT_FIELDS:
        snapshot[field_name] = _serialize_snapshot_value(getattr(customer, field_name, ""))
    return snapshot


def build_corporate_profile_snapshot(customer: CustomerCorporate | None) -> dict[str, str]:
    if customer is None:
        return {}
    snapshot: dict[str, str] = {}
    for field_name, _ in CORPORATE_PROFILE_SNAPSHOT_FIELDS:
        snapshot[field_name] = _serialize_snapshot_value(getattr(customer, field_name, ""))
    snapshot["resident_status"] = _serialize_snapshot_value(customer.resident_status)
    snapshot["source_of_funds"] = _serialize_snapshot_value(customer.source_of_funds)
    return snapshot


def build_loan_profile_snapshot(loan: CustomerLoan | None) -> dict[str, str]:
    if loan is None:
        return {}
    return {
        "loan_reporting_date": _serialize_snapshot_value(loan.reporting_date),
        "loan_id": _serialize_snapshot_value(loan.loan_id),
        "loan_amount": _serialize_snapshot_value(loan.loan_amount),
        "collateral_amount": _serialize_snapshot_value(loan.collateral_amount),
        "start_date": _serialize_snapshot_value(loan.start_date),
        "maturity_date": _serialize_snapshot_value(loan.maturity_date),
        "overdue_amount": _serialize_snapshot_value(loan.overdue_amount),
        "last_payment_date": _serialize_snapshot_value(loan.last_payment_date),
        "delinquent_days": _serialize_snapshot_value(loan.delinquent_days),
    }


def compare_profile_snapshots(
    previous_snapshot: dict[str, Any] | None,
    current_snapshot: dict[str, Any] | None,
) -> list[dict[str, str]]:
    previous_snapshot = previous_snapshot or {}
    current_snapshot = current_snapshot or {}
    differences: list[dict[str, str]] = []

    for field_name, label in PROFILE_SNAPSHOT_FIELDS + CORPORATE_PROFILE_SNAPSHOT_FIELDS + LOAN_PROFILE_SNAPSHOT_FIELDS:
        previous_value = _serialize_snapshot_value(previous_snapshot.get(field_name, ""))
        current_value = _serialize_snapshot_value(current_snapshot.get(field_name, ""))
        if previous_value != current_value:
            differences.append(
                {
                    "field": field_name,
                    "label": label,
                    "previous": previous_value or "Blank",
                    "current": current_value or "Blank",
                }
            )
    return differences


def _age_from_birth_date(birth_date: date | None) -> int | None:
    if birth_date is None:
        return None
    today = date.today()
    years = today.year - birth_date.year
    if (today.month, today.day) < (birth_date.month, birth_date.day):
        years -= 1
    return max(0, years)


def _option_matches(option: Option | IFRS9Option, keywords: Iterable[str]) -> bool:
    label = _normalize_text(option.label)
    return all(keyword in label for keyword in keywords)


def _option_matches_exact_words(option: Option | IFRS9Option, keywords: Iterable[str]) -> bool:
    label_words = set(re.findall(r"[A-Z0-9]+", _normalize_text(option.label)))
    return all(_normalize_text(keyword) in label_words for keyword in keywords)


def _ordered_attribute_options(attribute: Attribute | IFRS9Attribute) -> list[Option | IFRS9Option]:
    option_source = getattr(attribute, "options", None)
    if option_source is None:
        return []

    if isinstance(option_source, list):
        options = option_source
    else:
        options = option_source.all() if hasattr(option_source, "all") else option_source

    if hasattr(options, "order_by"):
        return list(options.order_by("display_order", "id"))

    return sorted(
        list(options),
        key=lambda option: (
            getattr(option, "display_order", 0) or 0,
            getattr(option, "id", 0) or 0,
        ),
    )


def _find_option_by_keywords(
    attribute: Attribute | IFRS9Attribute,
    keyword_groups: list[list[str]],
) -> Option | IFRS9Option | None:
    options = _ordered_attribute_options(attribute)
    for keywords in keyword_groups:
        for option in options:
            if _option_matches(option, keywords):
                return option
    return None


def _find_option_by_exact_words(
    attribute: Attribute | IFRS9Attribute,
    keywords: Iterable[str],
) -> Option | IFRS9Option | None:
    for option in _ordered_attribute_options(attribute):
        if _option_matches_exact_words(option, keywords):
            return option
    return None


def _to_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _latest_customer_loan(customer: MainCustomer | CustomerCorporate | None) -> CustomerLoan | None:
    state = getattr(customer, "_state", None)
    if state is None or getattr(state, "adding", False):
        return None

    customer_code = (
        getattr(customer, "customer_ref_code", None)
        or getattr(customer, "customer_code", None)
        or getattr(customer, "client_code", None)
    )
    raw_code = str(customer_code or "").strip()
    if not raw_code:
        return None

    candidate_codes = {raw_code}
    stripped_code = raw_code.lstrip("0")
    if stripped_code:
        candidate_codes.add(stripped_code)

    latest_date = (
        CustomerLoan.objects.filter(customer_code__in=candidate_codes)
        .aggregate(latest=Max("reporting_date"))
        .get("latest")
    )
    if latest_date is None:
        return None
    return (
        CustomerLoan.objects.filter(customer_code__in=candidate_codes, reporting_date=latest_date)
        .order_by("-source_last_sync_at", "-id")
        .first()
    )


def _attribute_text(attribute: Attribute | IFRS9Attribute) -> str:
    parts = [
        getattr(attribute, "code", ""),
        getattr(attribute, "label", ""),
        getattr(attribute, "group_label", ""),
    ]
    risk_driver = getattr(attribute, "risk_driver", None)
    if risk_driver is not None:
        parts.extend(
            [
                getattr(risk_driver, "code", ""),
                getattr(risk_driver, "name", ""),
            ]
        )
    return _normalize_text(" ".join(str(part or "") for part in parts))


def _months_between(start_date: date | None, end_date: date | None) -> int | None:
    if start_date is None or end_date is None:
        return None
    if end_date < start_date:
        return None
    months = (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month)
    if end_date.day < start_date.day:
        months -= 1
    return max(0, months)


def _loan_days_in_arrears(loan: CustomerLoan) -> int | None:
    delinquent_days = getattr(loan, "delinquent_days", None)
    if delinquent_days is not None:
        try:
            return max(0, int(delinquent_days))
        except (TypeError, ValueError):
            pass
    overdue_amount = _to_decimal(getattr(loan, "overdue_amount", None))
    if overdue_amount is None or overdue_amount <= 0:
        return 0
    last_payment_date = getattr(loan, "last_payment_date", None)
    if last_payment_date is None:
        return None
    return max(0, (date.today() - last_payment_date).days)


def _contains_any(text: str, tokens: Iterable[str]) -> bool:
    return any(token in text for token in tokens)


def _is_affirmative_flag(value: Any) -> bool:
    text = _normalize_text(value)
    return text in {"Y", "YES", "TRUE", "1", "NPL"} or "NPL" in text


def _loan_tenor_option(attribute: Attribute | IFRS9Attribute, loan: CustomerLoan) -> Option | IFRS9Option | None:
    months = _months_between(getattr(loan, "start_date", None), getattr(loan, "maturity_date", None))
    if months is None:
        return None

    years = months / 12
    for option in _ordered_attribute_options(attribute):
        label = _normalize_text(option.label)
        if "MONTH" in label:
            if "0 - 6" in label and months <= 6:
                return option
            if "6 - 12" in label and 6 < months <= 12:
                return option
            if "12 - 24" in label and 12 < months <= 24:
                return option
            if "24 - 36" in label and 24 < months <= 36:
                return option
            if "36 - 48" in label and 36 < months <= 48:
                return option
            if "MORE THAN 48" in label and months > 48:
                return option
            if "LESS THAN 6" in label and months < 6:
                return option
        if "YEAR" in label:
            if "1 -2" in label or "1 - 2" in label:
                if 12 <= months <= 24:
                    return option
            if "2 -3" in label or "2 - 3" in label:
                if 24 < months <= 36:
                    return option
            if "OVER 3" in label and months > 36:
                return option
            if "MORE THAN 4" in label and months > 48:
                return option
            match = re.search(r"(?<!\d)(\d+)\s*YEAR", label)
            if match and abs(years - int(match.group(1))) <= 0.5:
                return option
    return None


def _loan_amount_option(attribute: Attribute | IFRS9Attribute, loan: CustomerLoan) -> Option | IFRS9Option | None:
    amount = _to_decimal(getattr(loan, "loan_amount", None))
    if amount is None:
        return None
    overdue_amount = _to_decimal(getattr(loan, "overdue_amount", None))
    if overdue_amount is not None and overdue_amount > 0:
        arrears_option = _find_option_by_keywords(attribute, [["EXISTING", "ARREARS"]])
        if arrears_option:
            return arrears_option
    if amount < Decimal("1000"):
        return _find_option_by_keywords(attribute, [["BELOW", "1,000"], ["BELOW", "$1,000"]])
    if amount <= Decimal("5000"):
        return _find_option_by_keywords(attribute, [["1,000", "5,000"], ["$1,000", "$5,000"]])
    if amount <= Decimal("10000"):
        return _find_option_by_keywords(attribute, [["5,000", "10,000"], ["$5,000", "$10,000"]])
    return _find_option_by_keywords(attribute, [["ABOVE", "10,000"], ["ABOVE", "$10,000"]])


def _collateral_security_option(attribute: Attribute | IFRS9Attribute, loan: CustomerLoan) -> Option | IFRS9Option | None:
    loan_amount = _to_decimal(getattr(loan, "loan_amount", None))
    collateral_amount = _to_decimal(getattr(loan, "collateral_amount", None))
    if loan_amount is None or loan_amount <= 0 or collateral_amount is None:
        return None
    if collateral_amount <= 0:
        return _find_option_by_keywords(attribute, [["NO", "COLLATERAL"], ["UNSECURED", "COLLATERAL"], ["UNSECURED"]])

    coverage_percent = (collateral_amount / loan_amount) * Decimal("100")
    if coverage_percent >= Decimal("140"):
        return _find_option_by_keywords(attribute, [["140"], ["ABOVE"], ["ADEQUATELY", "SECURED"], ["TANGIBLE", "SECURITY"]])
    if Decimal("50") < coverage_percent < Decimal("100"):
        return _find_option_by_keywords(attribute, [["LESS", "100", "MORE", "50"], ["PARTIALLY", "SECURED"], ["PARTIALLY", "COVERED"]])
    if coverage_percent >= Decimal("100"):
        return _find_option_by_keywords(attribute, [["ADEQUATELY", "SECURED"], ["TANGIBLE", "SECURITY"], ["CASH", "COVER"]])
    if coverage_percent > 0:
        return _find_option_by_keywords(attribute, [["MOVABLE"], ["NGCB"], ["PARTIALLY", "SECURED"], ["NO", "SECURITY"]])
    return None


def _repayment_history_option(attribute: Attribute | IFRS9Attribute, loan: CustomerLoan) -> Option | IFRS9Option | None:
    overdue_amount = _to_decimal(getattr(loan, "overdue_amount", None))
    days = _loan_days_in_arrears(loan)
    past_due_text = _normalize_text(getattr(loan, "past_due_indicator", ""))

    if _is_affirmative_flag(getattr(loan, "npl_indicator_current", "")) or (days is not None and days > 90):
        option = _find_option_by_keywords(attribute, [["MORE", "90"], ["CURRENT", "NPL"], ["ARREARS"]])
        if option:
            return option
    if overdue_amount is None or overdue_amount <= 0:
        option = _find_option_by_keywords(
            attribute,
            [["CURRENT", "UP", "DATE"], ["NO", "DIFFICULTY"], ["PAID", "TIME"], ["NO", "ARREARS"], ["NEW", "BORROWER"]],
        )
        if option:
            return option
    if days is not None:
        if days <= 30:
            return _find_option_by_keywords(attribute, [["30", "DEFAULT"], ["STRUGGLED"], ["PIECEMEAL"]])
        if days <= 60:
            return _find_option_by_keywords(attribute, [["60", "DEFAULT"], ["STRUGGLED"], ["PIECEMEAL"]])
        if days <= 90:
            return _find_option_by_keywords(attribute, [["90", "DEFAULT"], ["RESTRUCTURED"], ["ARREARS"]])

    if _contains_any(past_due_text, {"Y", "YES", "TRUE", "PAST DUE"}):
        return _find_option_by_keywords(attribute, [["ARREARS"], ["STRUGGLED"], ["PIECEMEAL"]])
    return None


def _gender_option(attribute: Attribute | IFRS9Attribute, value: str) -> Option | IFRS9Option | None:
    text = _normalize_text(value)
    database_option_word = GENDER_AUTOFILL_MAPPING.get(text)
    if not database_option_word:
        return None
    return _find_option_by_exact_words(attribute, [database_option_word])


def _marital_option(attribute: Attribute | IFRS9Attribute, value: str) -> Option | IFRS9Option | None:
    text = _normalize_text(value)
    if not text:
        return None

    direct_code_mapping = {
        "M": [["MARRIED"], ["MARIED"]],
        "S": [["SINGLE"]],
        "D": [["DIVORCED"]],
        "W": [["WIDOWED"]],
        "E": [["ENGAGED"]],
        "SEP": [["SEPARATED"]],
    }
    if text in direct_code_mapping:
        return _find_option_by_keywords(attribute, direct_code_mapping[text])

    mapping = [
        (["MARR"], [["MARRIED"], ["MARIED"]]),
        (["WIDOW"], [["WIDOWED"]]),
        (["ENGAG"], [["ENGAGED"]]),
        (["DIVOR"], [["DIVORCED"]]),
        (["SEPAR"], [["SEPARATED"]]),
        (["SING"], [["SINGLE"]]),
        (["UNMARR"], [["SINGLE"]]),
    ]
    for tokens, keywords in mapping:
        if any(token in text for token in tokens):
            return _find_option_by_keywords(attribute, keywords)
    return None


def _resident_option(attribute: Attribute | IFRS9Attribute, value: str) -> Option | IFRS9Option | None:
    text = _normalize_text(value)
    if not text:
        return None

    exact_option_words = RESIDENCE_STATUS_AUTOFILL_MAPPING.get(text)
    if exact_option_words:
        return _find_option_by_exact_words(attribute, exact_option_words)
    if any(token in text for token in ["NON", "N/R", "NR"]):
        return _find_option_by_exact_words(attribute, ["NON", "RESIDENT"])
    if "TEMP" in text:
        return _find_option_by_exact_words(attribute, ["TEMPORARY"])
    if any(token in text for token in ["PERM", "RESIDENT", "PERMANENT"]):
        return _find_option_by_exact_words(attribute, ["PERMANENT"])
    return None


def _occupation_option(attribute: Attribute | IFRS9Attribute, customer: MainCustomer) -> Option | IFRS9Option | None:
    employment_type = _normalize_text(customer.employment_type)
    occupation_code = _normalize_text(customer.occupation_code)
    text = " ".join(
        filter(
            None,
            [
                employment_type,
                occupation_code,
                _normalize_text(customer.designation_code),
                _normalize_text(customer.employer_name),
            ],
        )
    )
    if not text:
        return None

    if "PENSION" in text or "RETIRED" in text or _normalize_text(customer.pension_flag) in {"Y", "YES", "TRUE", "1"}:
        return _find_option_by_keywords(attribute, [["PENSION"], ["RETIRED"]])
    if occupation_code in {"PNSR"} or _normalize_text(customer.pension_flag) in {"P"}:
        return _find_option_by_keywords(attribute, [["PENSION"], ["RETIRED"]])
    if "UNEMPLOY" in text:
        return _find_option_by_keywords(attribute, [["UNEMPLOY"]])
    if employment_type in {"S", "E", "SAL", "SALARIED"}:
        return _find_option_by_keywords(attribute, [["SALARIED"], ["EMPLOYEE"]])
    if employment_type in {"P", "PENSION"}:
        return _find_option_by_keywords(attribute, [["PENSION"], ["RETIRED"]])
    if any(token in occupation_code for token in ["FRM", "FARM", "AGRI"]):
        return _find_option_by_keywords(attribute, [["BUSINESS"], ["OWNER"], ["INFORMAL"], ["TRADER"]])
    if occupation_code in {"BNKR", "NUR", "MIL", "TCR", "SGG", "SECO", "DADM", "SUP"}:
        return _find_option_by_keywords(attribute, [["SALARIED"], ["EMPLOYEE"]])
    if occupation_code in {"BCW", "ENT"}:
        return _find_option_by_keywords(attribute, [["BUSINESS"], ["OWNER"], ["INFORMAL"], ["TRADER"]])
    if occupation_code in {"OTH"} and employment_type == "N":
        return _find_option_by_keywords(attribute, [["BUSINESS"], ["OWNER"], ["INFORMAL"], ["TRADER"]])
    if "CONSULT" in text:
        return _find_option_by_keywords(attribute, [["CONSULT"]])
    if "COMMISSION" in text:
        return _find_option_by_keywords(attribute, [["COMMISSION"]])
    if "INFORMAL" in text or "TRADER" in text:
        return _find_option_by_keywords(attribute, [["INFORMAL"], ["TRADER"]])
    if any(token in text for token in ["BUSINESS", "OWNER", "PROPRIETOR", "SELF EMPLOY", "SELF-EMPLOY"]):
        return _find_option_by_keywords(attribute, [["BUSINESS"], ["OWNER"]])
    if any(token in text for token in ["SALAR", "EMPLOYEE", "EMPLOYED", "STAFF"]):
        return _find_option_by_keywords(attribute, [["SALARIED"], ["EMPLOYEE"]])
    return None


def _residential_status_option(attribute: Attribute | IFRS9Attribute, value: str) -> Option | IFRS9Option | None:
    text = _normalize_text(value)
    if not text:
        return None

    direct_code_mapping = {
        "1": [["OWNER", "FREEHOLD"]],
        "2": [["OWNER", "MORTGAGED"], ["MORTGAGED"]],
        "3": [["RENT", "BUY"]],
        "4": [["COMPANY", "HOUSE"], ["COMPANY"]],
        "5": [["RENTED"]],
        "6": [["PARENTS"], ["OTHER", "ARRANGEMENTS"], ["OTHER"]],
    }
    if text in direct_code_mapping:
        return _find_option_by_keywords(attribute, direct_code_mapping[text])

    if "MORTGAGE" in text:
        return _find_option_by_keywords(attribute, [["MORTGAGED"]])
    if "RENT TO BUY" in text or "RENTTOBUY" in text:
        return _find_option_by_keywords(attribute, [["RENT"], ["BUY"]])
    if "COMPANY" in text:
        return _find_option_by_keywords(attribute, [["COMPANY"]])
    if "RENT" in text or "LODGER" in text:
        return _find_option_by_keywords(attribute, [["RENTED"]])
    if any(token in text for token in ["PARENT", "FAMILY", "RELATIVE"]):
        return _find_option_by_keywords(attribute, [["PARENTS"], ["OTHER"]])
    if "OWNER" in text or "FREEHOLD" in text:
        return _find_option_by_keywords(attribute, [["OWNER"], ["FREEHOLD"]])
    return None


def _income_option(attribute: Attribute | IFRS9Attribute, customer: MainCustomer, *, monthly: bool) -> Option | IFRS9Option | None:
    amount = customer.annual_income
    if amount is None:
        return None

    value = float(amount)
    if monthly:
        value = value / 12

    if attribute.code == "NET_MONTHLY_INCOME":
        if isinstance(attribute, Attribute):
            if value > 5000:
                return _find_option_by_keywords(attribute, [["ABOVE", "5,000"], ["ABOVE", "$5,000"]])
            if value >= 3001:
                return _find_option_by_keywords(attribute, [["3,001"], ["$3,001"]])
            if value >= 1001:
                return _find_option_by_keywords(attribute, [["1,001"], ["$1,001"]])
            if value >= 501:
                return _find_option_by_keywords(attribute, [["501"], ["$501"]])
            if value >= 251:
                return _find_option_by_keywords(attribute, [["251"], ["$251"]])
            return _find_option_by_keywords(attribute, [["BELOW", "251"], ["250"], ["251"]])
        if value > 5000:
            return _find_option_by_keywords(attribute, [["OVER", "5,000"], ["OVER", "$5,000"]])
        if value >= 1001:
            return _find_option_by_keywords(attribute, [["1,001"], ["$1,001"]])
        if value >= 501:
            return _find_option_by_keywords(attribute, [["501"], ["$501"]])
        if value >= 251:
            return _find_option_by_keywords(attribute, [["251"], ["$251"]])
        return _find_option_by_keywords(attribute, [["250"], ["BELOW"]])
    return None


def _income_stream_option(attribute: Attribute | IFRS9Attribute, customer: MainCustomer) -> Option | IFRS9Option | None:
    employment_type = _normalize_text(customer.employment_type)
    occupation_code = _normalize_text(customer.occupation_code)
    pension_flag = _normalize_text(customer.pension_flag)
    source_of_funds = _normalize_text(customer.source_of_funds)

    if pension_flag in {"Y", "YES", "TRUE", "1", "P"} or employment_type in {"P", "PENSION"} or occupation_code == "PNSR":
        return _find_option_by_keywords(attribute, [["PENSION"]])
    if employment_type in {"S", "E", "SAL", "SALARIED"} or source_of_funds == "02":
        option = _find_option_by_keywords(attribute, [["SALARY"], ["INCOME", "ONLY"]])
        if option:
            return option
        return _find_option_by_keywords(attribute, [["SALARY"]])
    if any(token in occupation_code for token in ["FRM", "FARM", "AGRI", "BUS", "TRADER"]):
        return _find_option_by_keywords(attribute, [["NON", "SALARY"], ["INVESTMENTS"], ["RENTAL"]])
    if source_of_funds in {"04"}:
        return _find_option_by_keywords(attribute, [["NON", "SALARY"], ["INVESTMENTS"], ["RENTAL"]])
    if source_of_funds in {"09", "SALARY"}:
        option = _find_option_by_keywords(attribute, [["SALARY"], ["OTHER"], ["INCOME"]])
        if option:
            return option
        return _find_option_by_keywords(attribute, [["SALARY"]])
    if employment_type == "N":
        return _find_option_by_keywords(attribute, [["NON", "SALARY"], ["INVESTMENTS"], ["RENTAL"]])
    return None


def _employer_sector_option(attribute: Attribute | IFRS9Attribute, customer: MainCustomer) -> Option | IFRS9Option | None:
    occupation_code = _normalize_text(customer.occupation_code)
    text = " ".join(
        filter(
            None,
            [
                _normalize_text(customer.work_sector_code),
                _normalize_text(customer.industry_code),
                _normalize_text(customer.sub_industry_code),
                _normalize_text(customer.occupation_code),
                _normalize_text(customer.employer_name),
            ],
        )
    )
    if not text:
        return None

    mapping = [
        (["AGR", "AGRI", "FRM", "FARM"], [["AGRICULTURE"]]),
        (["TELCO", "TELECOM"], [["TELECOMMUNICATIONS"]]),
        (["ICT", "TECH", "INFORMATION"], [["INFORMATION"], ["COMMUNICATIONS"], ["TECHNOLOGY"]]),
        (["BANK", "FINANCE", "INSURANCE"], [["FINANCE"], ["INSURANCE"]]),
        (["POWER", "ENERGY", "ELECTRIC"], [["POWER"], ["ENERGY"]]),
        (["PROCESS", "VALUE ADD"], [["PROCESSING"], ["VALUE"], ["ADDITION"]]),
        (["SERVICE", "DISTRIBUT"], [["DISTRIBUTION"], ["SERVICES"]]),
        (["STATE", "GOVERNMENT", "PARASTATAL", "MINISTRY"], [["STATE"], ["PARASTATAL"]]),
        (["MINING"], [["MINING"]]),
        (["MANUFACTUR"], [["MANUFACTURING"]]),
        (["TOURISM", "HOSPITAL"], [["TOURISM"], ["HOSPITALITY"]]),
    ]
    for tokens, keywords in mapping:
        if any(token in text for token in tokens):
            option = _find_option_by_keywords(attribute, keywords)
            if option:
                return option

    if "UNEMPLOY" in text:
        return _find_option_by_keywords(attribute, [["UNEMPLOY"]])
    if occupation_code == "BNKR":
        return _find_option_by_keywords(attribute, [["FINANCE"], ["INSURANCE"]])
    if occupation_code in {"NUR", "MIL", "TCR", "SGG", "SECO", "DADM"}:
        return _find_option_by_keywords(attribute, [["STATE"], ["PARASTATAL"]])
    if occupation_code == "FRM":
        return _find_option_by_keywords(attribute, [["AGRICULTURE"]])
    return None


def _age_option(attribute: Attribute | IFRS9Attribute, birth_date: date | None) -> Option | IFRS9Option | None:
    age = _age_from_birth_date(birth_date)
    if age is None:
        return None

    for option in _ordered_attribute_options(attribute):
        label = _normalize_text(option.label)
        range_match = re.search(r"\b(\d+)\s*-\s*(\d+)\b", label)
        if range_match:
            lower, upper = (int(value) for value in range_match.groups())
            if lower <= age <= upper:
                return option
            continue

        threshold_match = re.search(r"\b(ABOVE|OVER|MORE THAN)\s+(\d+)\b", label)
        if threshold_match and age > int(threshold_match.group(2)):
            return option

        threshold_match = re.search(r"\b(BELOW|UNDER|LESS THAN)\s+(\d+)\b", label)
        if threshold_match and age < int(threshold_match.group(2)):
            return option
    return None


def _type_of_employer_option(attribute: IFRS9Attribute, customer: MainCustomer) -> IFRS9Option | None:
    employment_type = _normalize_text(customer.employment_type)
    occupation_code = _normalize_text(customer.occupation_code)
    text = " ".join(
        filter(
            None,
            [
                employment_type,
                occupation_code,
                _normalize_text(customer.work_sector_code),
                _normalize_text(customer.industry_code),
                _normalize_text(customer.sub_industry_code),
                _normalize_text(customer.employer_name),
            ],
        )
    )
    if not text:
        return None

    if occupation_code in {"MIL", "NUR", "TCR", "SGG", "SECO", "DADM"}:
        option = _find_option_by_keywords(attribute, [["STATE"], ["GOVERNMENT"]])
        if option:
            return option
    if occupation_code == "BNKR":
        option = _find_option_by_keywords(attribute, [["PRIVATE"]])
        if option:
            return option
    if employment_type in {"S", "E", "SAL", "SALARIED"}:
        option = _find_option_by_keywords(attribute, [["PRIVATE"]])
        if option:
            return option
    if any(token in occupation_code for token in ["FRM", "FARM", "AGRI"]):
        option = _find_option_by_keywords(attribute, [["FARMERS"], ["SMES"]])
        if option:
            return option
    if occupation_code == "PNSR":
        option = _find_option_by_keywords(attribute, [["STATE"], ["GOVERNMENT"], ["PRIVATE"]])
        if option:
            return option
    mapping = [
        (["PARASTATAL", "SOE", "STATE OWNED"], [["PARASTATAL"], ["STATE", "OWNED"]]),
        (["GOVERNMENT", "MINISTRY", "STATE"], [["STATE"], ["GOVERNMENT"]]),
        (["PRIVATE"], [["PRIVATE"]]),
        (["FARM", "AGRIC", "SME"], [["FARMERS"], ["SMES"]]),
        (["SELF EMPLOY", "SELF-EMPLOY"], [["SELF-EMPLOYED"]]),
    ]
    for tokens, keywords in mapping:
        if any(token in text for token in tokens):
            option = _find_option_by_keywords(attribute, keywords)
            if option:
                return option
    return None


def _flag_is_enabled(value: Any) -> bool:
    return _normalize_text(value) in {"1", "Y", "YES", "TRUE", "T"}


def _customer_corporate_profile(customer: MainCustomer | None) -> CustomerCorporate | None:
    if customer is None:
        return None
    raw_code = str(customer.customer_ref_code or "").strip()
    if not raw_code:
        return None
    normalized_code = raw_code.lstrip("0") or "0"
    candidate_codes = {raw_code, normalized_code}
    return CustomerCorporate.objects.filter(client_code__in=candidate_codes).first()


def _corporate_profile_text(customer: CustomerCorporate) -> str:
    values = [
        customer.client_name,
        customer.nature_of_business_1,
        customer.nature_of_business_2,
        customer.nature_of_business_3,
        customer.registration_authority,
        customer.sovereign_type,
    ]
    return " ".join(filter(None, (_normalize_text(value) for value in values)))


def _business_age_months(customer: CustomerCorporate) -> int | None:
    years = customer.years_in_business
    if years is not None and years > 0:
        return int(years) * 12

    start_date = customer.incorporation_date or customer.registration_date
    if start_date:
        today = date.today()
        months = (today.year - start_date.year) * 12 + today.month - start_date.month
        if today.day < start_date.day:
            months -= 1
        if years == 0 and months > 12:
            return 0
        return max(0, months)

    if years is not None:
        return max(0, int(years) * 12)
    return None


def _specific_business_activities(customer: CustomerCorporate) -> list[str]:
    ignored_values = {"OTHER", "OTHERS", "OTHERSKILLEDPERSONNEL", "NIL", "N/A", "NA"}
    activities: list[str] = []
    for value in (
        customer.nature_of_business_1,
        customer.nature_of_business_2,
        customer.nature_of_business_3,
    ):
        normalized = _normalize_text(value)
        if normalized and normalized not in ignored_values and normalized not in activities:
            activities.append(normalized)
    return activities


def _is_registered_company_name(text: str) -> bool:
    return bool(
        re.search(r"\b(PVT|PRIVATE)\s+(LTD|LIMITED)\b", text)
        or re.search(r"\b(PLC|LIMITED|COMPANY)\b", text)
    )


def _is_cooperative_name(text: str) -> bool:
    return bool(re.search(r"\bCO[ -]?OPERATIVE\b", text))


def _is_public_entity(customer: CustomerCorporate, text: str) -> bool:
    if any(
        _flag_is_enabled(value)
        for value in (
            customer.is_public_sector,
            customer.is_sovereign,
            customer.is_central_state,
        )
    ):
        return True
    return any(
        token in text
        for token in (
            "GOVERNMENT",
            "MINISTRY",
            "PARASTATAL",
            "STATE OWNED",
            "MUNICIPAL",
            "CITY COUNCIL",
            "TOWN COUNCIL",
            "RURAL DISTRICT COUNCIL",
        )
    )


def _basel_farming_years_option(
    attribute: Attribute,
    customer: CustomerCorporate,
) -> Option | None:
    months = _business_age_months(customer)
    if months is None:
        return None
    if months > 60:
        return _find_option_by_keywords(attribute, [["OVER", "5"]])
    if months >= 48:
        return _find_option_by_keywords(attribute, [["4", "-", "5"]])
    if months >= 36:
        return _find_option_by_keywords(attribute, [["3", "-", "4"]])
    if months >= 24:
        return _find_option_by_keywords(attribute, [["2", "-", "3"]])
    return _find_option_by_keywords(attribute, [["LESS THAN", "2"]])


def _basel_retail_years_option(
    attribute: Attribute,
    customer: CustomerCorporate,
) -> Option | None:
    months = _business_age_months(customer)
    if months is None:
        return None
    if months > 48:
        return _find_option_by_keywords(attribute, [["MORE THAN", "4"]])
    if months >= 36:
        return _find_option_by_keywords(attribute, [["3", "-", "4"]])
    if months >= 24:
        return _find_option_by_keywords(attribute, [["2", "-", "3"]])
    if months >= 12:
        return _find_option_by_keywords(attribute, [["1", "-", "2"]])
    if months > 0:
        return _find_option_by_keywords(attribute, [["LESS THAN", "1"]])
    return _find_option_by_keywords(attribute, [["START", "UP"], ["GREENFIELD"]])


def _basel_retail_legal_status_option(
    attribute: Attribute,
    customer: CustomerCorporate,
) -> Option | None:
    text = _corporate_profile_text(customer)
    if "PARTNERSHIP" in text:
        return _find_option_by_keywords(attribute, [["PARTNERSHIP"]])
    if "SOLE PROPRIETOR" in text or "SOLE TRADER" in text:
        return _find_option_by_keywords(attribute, [["SOLE", "PROPRIETOR"]])
    if _is_cooperative_name(text):
        if customer.registration_number:
            return _find_option_by_keywords(attribute, [["REGISTERED", "ASSOCIATION"]])
        return _find_option_by_keywords(attribute, [["UNREGISTERED", "INFORMAL"]])
    if _is_registered_company_name(text) or any(
        token in text for token in ("ASSOCIATION", "REGISTERED TRUST")
    ):
        return _find_option_by_keywords(attribute, [["REGISTERED", "COMPANY"]])
    if "UNREGISTERED" in text or "INFORMAL BODY" in text:
        return _find_option_by_keywords(attribute, [["UNREGISTERED", "INFORMAL"]])
    return None


def _basel_retail_size_option(
    attribute: Attribute,
    customer: CustomerCorporate,
) -> Option | None:
    if _is_cooperative_name(_corporate_profile_text(customer)):
        return _find_option_by_keywords(attribute, [["COOPERATIVE"]])
    return None


def _basel_corporate_ownership_option(
    attribute: Attribute,
    customer: CustomerCorporate,
) -> Option | None:
    text = _corporate_profile_text(customer)
    if _is_public_entity(customer, text):
        return _find_option_by_keywords(attribute, [["STATE OWNED"], ["PARASTATAL"]])
    if "SUBSIDIARY" in text:
        return _find_option_by_keywords(attribute, [["SUBSIDIARY"]])
    if _is_registered_company_name(text):
        return _find_option_by_keywords(attribute, [["STAND ALONE"]])
    if "UNINCORPORATED" in text:
        return _find_option_by_keywords(attribute, [["UNINCORPORATED"]])
    return None


def _basel_business_diversification_option(
    attribute: Attribute,
    customer: CustomerCorporate,
) -> Option | None:
    activity_count = len(_specific_business_activities(customer))
    if activity_count >= 2:
        return _find_option_by_keywords(attribute, [["FAIRLY DIVERSIFIED"]])
    if activity_count == 1:
        return _find_option_by_keywords(attribute, [["UNDIVERSIFIED"]])
    return None


def _ifrs9_corporate_lifecycle_option(
    attribute: IFRS9Attribute,
    customer: CustomerCorporate,
) -> IFRS9Option | None:
    months = _business_age_months(customer)
    if months is None:
        return None
    if months <= 24:
        return _find_option_by_keywords(attribute, [["BIRTH"]])
    if months <= 60:
        return _find_option_by_keywords(attribute, [["GROWTH"]])
    return _find_option_by_keywords(attribute, [["MATURITY"]])


def _ifrs9_product_diversification_option(
    attribute: IFRS9Attribute,
    customer: CustomerCorporate,
) -> IFRS9Option | None:
    activity_count = len(_specific_business_activities(customer))
    if activity_count >= 2:
        return _find_option_by_keywords(attribute, [["AVERAGE"]])
    if activity_count == 1:
        return _find_option_by_keywords(attribute, [["NARROW"]])
    return None


def _ifrs9_farming_experience_option(
    attribute: IFRS9Attribute,
    customer: CustomerCorporate,
) -> IFRS9Option | None:
    months = _business_age_months(customer)
    if months is None:
        return None
    if months > 60:
        return _find_option_by_keywords(attribute, [["OVER", "5"]])
    if months > 24:
        return _find_option_by_keywords(attribute, [["OVER", "2", "LESS THAN", "5"]])
    return None


def _local_authority_classification_option(
    attribute: IFRS9Attribute,
    customer: CustomerCorporate,
) -> IFRS9Option | None:
    text = _corporate_profile_text(customer)
    if "RURAL DISTRICT COUNCIL" in text or re.search(r"\bRDC\b", text):
        return _find_option_by_keywords(attribute, [["RURAL", "DISTRICT"]])
    if "TOWN COUNCIL" in text or "TOWN BOARD" in text:
        return _find_option_by_keywords(attribute, [["TOWN", "COUNCIL"]])
    if "CITY COUNCIL" in text:
        return _find_option_by_keywords(attribute, [["CITY", "COUNCIL"]])
    return None


def _mfinance_nature_option(
    attribute: IFRS9Attribute,
    customer: CustomerCorporate,
) -> IFRS9Option | None:
    text = _corporate_profile_text(customer)
    explicit_mapping = [
        (("CROSS BORDER",), [["CROSS", "BORDER"]]),
        (("VENDOR", "HAWKER"), [["VENDOR"]]),
        (("MICROFINANCE", "SALARY LOAN", "CONSUMER LOAN"), [["SALARY", "MICROFINANCE"]]),
        (("MANUFACTUR", "FACTORY"), [["MANUFACTURING"]]),
        (("RETAIL", "SHOP", "STORE", "SUPERMARKET"), [["RETAILING"]]),
        (("FARM", "AGRIC", "CROP", "LIVESTOCK"), [["AGRICULTURE"]]),
        (("SERVICE", "CONSULT", "TRANSPORT", "LOGISTICS"), [["SERVICES"]]),
    ]
    for tokens, option_keywords in explicit_mapping:
        if any(token in text for token in tokens):
            return _find_option_by_keywords(attribute, option_keywords)

    industry_code = _normalize_text(customer.industry_code)
    industry_mapping = {
        "00100": [["AGRICULTURE"]],
        "00300": [["RETAILING"]],
        "00900": [["MANUFACTURING"]],
        "00400": [["SERVICES"]],
        "00600": [["SERVICES"]],
        "00700": [["SERVICES"]],
        "00800": [["SERVICES"]],
        "01000": [["SERVICES"]],
        "01100": [["SERVICES"]],
        "01200": [["SERVICES"]],
    }
    if industry_code in industry_mapping:
        return _find_option_by_keywords(attribute, industry_mapping[industry_code])
    return _find_option_by_keywords(attribute, [["OTHER"]]) if industry_code else None


def _mfinance_business_experience_option(
    attribute: IFRS9Attribute,
    customer: CustomerCorporate,
) -> IFRS9Option | None:
    months = _business_age_months(customer)
    if months is None:
        return None
    if months > 24:
        return _find_option_by_keywords(attribute, [["MORE THAN", "2"]])
    if months > 12:
        return _find_option_by_keywords(attribute, [["MORE THAN", "1", "LESS THAN", "2"]])
    if months >= 6:
        return _find_option_by_keywords(attribute, [["6 MONTHS", "12 MONTHS"]])
    if months >= 1:
        return _find_option_by_keywords(attribute, [["1 MONTH", "6 MONTHS"]])
    return _find_option_by_keywords(attribute, [["START UP"]])


def _ifrs9_retail_track_record_option(
    attribute: IFRS9Attribute,
    customer: CustomerCorporate,
) -> IFRS9Option | None:
    months = _business_age_months(customer)
    if months is None:
        return None
    if months > 120:
        return _find_option_by_keywords(attribute, [["OVER", "10"]])
    if months >= 72:
        return _find_option_by_keywords(attribute, [["6 TO 10"]])
    if months >= 36:
        return _find_option_by_keywords(attribute, [["3 TO 6"]])
    if months > 12:
        return _find_option_by_keywords(attribute, [["LESS THAN 3", "MORE THAN 12"]])
    return _find_option_by_keywords(attribute, [["UP TO 12"]])


def _ifrs9_retail_ownership_option(
    attribute: IFRS9Attribute,
    customer: CustomerCorporate,
) -> IFRS9Option | None:
    text = _corporate_profile_text(customer)
    if _is_cooperative_name(text):
        return _find_option_by_keywords(attribute, [["COOPERATIVES"]])
    if "PARTNERSHIP" in text:
        return _find_option_by_keywords(attribute, [["PARTNERSHIP"]])
    if "SOLE PROPRIETOR" in text or "SOLE TRADER" in text:
        return _find_option_by_keywords(attribute, [["SOLE", "PROPRIETOR"]])
    if _is_registered_company_name(text):
        return _find_option_by_keywords(attribute, [["REGISTERED", "COMPANY"]])
    return None


def _school_ownership_option(
    attribute: IFRS9Attribute,
    customer: CustomerCorporate,
) -> IFRS9Option | None:
    text = _corporate_profile_text(customer)
    if _is_public_entity(customer, text):
        return _find_option_by_keywords(attribute, [["GOVERNMENT"], ["LOCAL", "AUSTORITY"]])
    if any(token in text for token in ("MISSION", "CHURCH", "CATHOLIC", "ANGLICAN", "METHODIST", "ADVENTIST", "LUTHERAN")):
        return _find_option_by_keywords(attribute, [["MISSION"], ["INTERNATIONAL"]])
    if "TRUST" in text:
        return _find_option_by_keywords(attribute, [["TRUST", "OWNED"]])
    if any(token in text for token in ("PRIVATE", "PVT", "FAMILY OWNED")):
        return _find_option_by_keywords(attribute, [["PRIVATE", "FAMILY"]])
    return None


def _school_type_option(
    attribute: IFRS9Attribute,
    customer: CustomerCorporate,
) -> IFRS9Option | None:
    text = _corporate_profile_text(customer)
    has_primary = "PRIMARY" in text
    has_secondary = "SECONDARY" in text or "HIGH SCHOOL" in text
    if has_primary == has_secondary:
        return None
    if has_primary:
        return _find_option_by_keywords(attribute, [["PRIMARY"]])
    return _find_option_by_keywords(attribute, [["SECONDARY"]])


def _tertiary_ownership_option(
    attribute: IFRS9Attribute,
    customer: CustomerCorporate,
) -> IFRS9Option | None:
    text = _corporate_profile_text(customer)
    if _is_public_entity(customer, text) or "STATE UNIVERSITY" in text:
        return _find_option_by_keywords(attribute, [["STATE", "OWNED"]])
    if any(token in text for token in ("CHURCH", "MISSION", "CATHOLIC", "ANGLICAN", "METHODIST", "ADVENTIST", "LUTHERAN")):
        return _find_option_by_keywords(attribute, [["CHURCH", "OWNED"]])
    if any(token in text for token in ("PRIVATE", "PVT")):
        return _find_option_by_keywords(attribute, [["PRIVATE", "OWNED"]])
    return None


def _tertiary_type_option(
    attribute: IFRS9Attribute,
    customer: CustomerCorporate,
) -> IFRS9Option | None:
    text = _corporate_profile_text(customer)
    if "UNIVERSITY" in text:
        return _find_option_by_keywords(attribute, [["UNIVERSITIES"]])
    if "TEACHERS COLLEGE" in text or "TEACHER TRAINING" in text:
        return _find_option_by_keywords(attribute, [["TEACHERS", "COLLEGE"]])
    if any(token in text for token in ("TECHNICAL COLLEGE", "POLYTECHNIC")):
        return _find_option_by_keywords(attribute, [["TECHNICAL", "COLLEGES"]])
    if any(token in text for token in ("VOCATIONAL", "TRAINING CENTRE", "TRAINING CENTER")):
        return _find_option_by_keywords(attribute, [["VOCATIONAL", "TRAINING"]])
    return None


def _set_autofill_value(
    attribute_values: dict[int, str],
    applied_labels: list[str],
    attribute: Attribute | IFRS9Attribute,
    option: Option | IFRS9Option | None,
) -> None:
    if option is None:
        return
    attribute_values[attribute.id] = str(option.id)
    applied_labels.append(attribute.label)


def _build_autofill_result(
    attributes: list[Attribute] | list[IFRS9Attribute],
    values: dict[int, str],
    labels: list[str],
    profile_snapshot: dict[str, str],
) -> AutofillResult:
    missing_required = [
        attribute
        for attribute in attributes
        if attribute.is_required and attribute.id not in values
    ]
    return AutofillResult(
        values,
        labels,
        sorted(values.keys()),
        [attribute.label for attribute in missing_required],
        [attribute.id for attribute in missing_required],
        profile_snapshot,
    )


def _merge_loan_autofill(
    result: AutofillResult,
    attributes: list[Attribute] | list[IFRS9Attribute],
    loan: CustomerLoan | None,
    *,
    allow_descriptive_loan_fields: bool = True,
) -> AutofillResult:
    if loan is None:
        return result

    values = dict(result.attribute_values)
    labels = list(result.applied_labels)
    applied_ids = set(result.applied_attribute_ids)

    def apply(attribute: Attribute | IFRS9Attribute, option: Option | IFRS9Option | None) -> None:
        if option is None or attribute.id in values:
            return
        values[attribute.id] = str(option.id)
        labels.append(f"{attribute.label} (Loan data)")
        applied_ids.add(attribute.id)

    for attribute in attributes:
        text = _attribute_text(attribute)
        label_text = _normalize_text(getattr(attribute, "label", ""))
        if ("LOAN AMOUNT" in text or "SIZE OF LOAN" in text) and "BALANCE SHEET" not in text:
            apply(attribute, _loan_amount_option(attribute, loan))
        elif "LOAN TENOR" in text or "LOAN TENURE" in text:
            apply(attribute, _loan_tenor_option(attribute, loan))
        elif allow_descriptive_loan_fields and (
            "COLLATERAL SECURITY" in text or "LOAN SECURITY" in text or label_text == "SECURITY"
        ):
            apply(attribute, _collateral_security_option(attribute, loan))
        elif allow_descriptive_loan_fields and (
            "REPAYMENT HISTORY" in text or "REPAYMENT TRACK RECORD" in text or "DEFAULT/REPAYMENT HISTORY" in text
        ):
            apply(attribute, _repayment_history_option(attribute, loan))

    missing_required = [
        attribute
        for attribute in attributes
        if attribute.is_required and attribute.id not in values
    ]
    profile_snapshot = dict(result.profile_snapshot)
    profile_snapshot.update(build_loan_profile_snapshot(loan))
    return AutofillResult(
        values,
        labels,
        sorted(applied_ids),
        [attribute.label for attribute in missing_required],
        [attribute.id for attribute in missing_required],
        profile_snapshot,
    )


def _build_basel_individual_result(
    customer: MainCustomer,
    attributes: list[Attribute],
    loan: CustomerLoan | None = None,
) -> AutofillResult:
    by_code = {attribute.code: attribute for attribute in attributes}
    values: dict[int, str] = {}
    labels: list[str] = []

    _set_autofill_value(values, labels, by_code.get("GENDER"), _gender_option(by_code.get("GENDER"), customer.gender) if by_code.get("GENDER") else None)
    _set_autofill_value(values, labels, by_code.get("AGE"), _age_option(by_code.get("AGE"), customer.birth_date) if by_code.get("AGE") else None)
    _set_autofill_value(values, labels, by_code.get("MARITAL_STATUS"), _marital_option(by_code.get("MARITAL_STATUS"), customer.marital_status) if by_code.get("MARITAL_STATUS") else None)
    _set_autofill_value(values, labels, by_code.get("RESIDENCE_STATUS"), _resident_option(by_code.get("RESIDENCE_STATUS"), customer.resident_status) if by_code.get("RESIDENCE_STATUS") else None)
    _set_autofill_value(values, labels, by_code.get("RESIDENTIAL_STATUS"), _residential_status_option(by_code.get("RESIDENTIAL_STATUS"), customer.accommodation_type) if by_code.get("RESIDENTIAL_STATUS") else None)
    _set_autofill_value(values, labels, by_code.get("SOURCE_OF_INCOME"), _income_stream_option(by_code.get("SOURCE_OF_INCOME"), customer) if by_code.get("SOURCE_OF_INCOME") else None)
    _set_autofill_value(values, labels, by_code.get("NET_MONTHLY_INCOME"), _income_option(by_code.get("NET_MONTHLY_INCOME"), customer, monthly=True) if by_code.get("NET_MONTHLY_INCOME") else None)
    _set_autofill_value(values, labels, by_code.get("OCCUPATIONAL_STATUS"), _occupation_option(by_code.get("OCCUPATIONAL_STATUS"), customer) if by_code.get("OCCUPATIONAL_STATUS") else None)
    _set_autofill_value(values, labels, by_code.get("EMPLOYER_ECONOMIC_SECTOR"), _employer_sector_option(by_code.get("EMPLOYER_ECONOMIC_SECTOR"), customer) if by_code.get("EMPLOYER_ECONOMIC_SECTOR") else None)

    result = _build_autofill_result(
        attributes,
        values,
        labels,
        build_customer_profile_snapshot(customer),
    )
    return _merge_loan_autofill(result, attributes, loan)


def _build_basel_corporate_result(
    template_code: str,
    customer: CustomerCorporate,
    attributes: list[Attribute],
    loan: CustomerLoan | None = None,
) -> AutofillResult:
    by_code = {attribute.code: attribute for attribute in attributes}
    values: dict[int, str] = {}
    labels: list[str] = []

    def apply(attribute_code: str, option: Option | None) -> None:
        attribute = by_code.get(attribute_code)
        if attribute:
            _set_autofill_value(values, labels, attribute, option)

    if template_code == "AFCSC1-17":
        attribute = by_code.get("YEARS_FARMING")
        apply("YEARS_FARMING", _basel_farming_years_option(attribute, customer) if attribute else None)
    elif template_code == "ACCSC1-17":
        attribute = by_code.get("OWNERSHIP_CONTROL")
        apply("OWNERSHIP_CONTROL", _basel_corporate_ownership_option(attribute, customer) if attribute else None)
        attribute = by_code.get("BUSINESS_DIVERSIFICATION")
        apply("BUSINESS_DIVERSIFICATION", _basel_business_diversification_option(attribute, customer) if attribute else None)
    elif template_code == "ARCSC1-17":
        attribute = by_code.get("LEGAL_STATUS")
        apply("LEGAL_STATUS", _basel_retail_legal_status_option(attribute, customer) if attribute else None)
        attribute = by_code.get("YEARS_IN_BUSINESS")
        apply("YEARS_IN_BUSINESS", _basel_retail_years_option(attribute, customer) if attribute else None)
        attribute = by_code.get("SIZE_OF_ORGANIZATION")
        apply("SIZE_OF_ORGANIZATION", _basel_retail_size_option(attribute, customer) if attribute else None)

    result = _build_autofill_result(
        attributes,
        values,
        labels,
        build_corporate_profile_snapshot(customer),
    )
    return _merge_loan_autofill(result, attributes, loan)


def build_basel_autofill(
    template_code: str,
    customer: MainCustomer | None,
    attributes_by_driver: dict[int, list[Attribute]],
    corporate_customer: CustomerCorporate | None = None,
    loan: CustomerLoan | None = None,
) -> AutofillResult:
    if customer is None:
        return AutofillResult({}, [], [], [], [], {})

    normalized_template_code = (template_code or "").upper()
    attributes = [attribute for attrs in attributes_by_driver.values() for attribute in attrs]
    loan = loan or _latest_customer_loan(customer)
    if normalized_template_code == BASEL_INDIVIDUAL_TEMPLATE_CODE:
        return _build_basel_individual_result(customer, attributes, loan)
    if normalized_template_code not in BASEL_CORPORATE_TEMPLATE_CODES:
        return AutofillResult({}, [], [], [], [], {})

    corporate_customer = corporate_customer or _customer_corporate_profile(customer)
    if corporate_customer is None:
        return _build_autofill_result(attributes, {}, [], {})
    return _build_basel_corporate_result(
        normalized_template_code,
        corporate_customer,
        attributes,
        loan,
    )


def build_basel_individual_autofill(
    template_code: str,
    customer: MainCustomer | None,
    attributes_by_driver: dict[int, list[Attribute]],
) -> AutofillResult:
    return build_basel_autofill(template_code, customer, attributes_by_driver)


def _build_ifrs9_consumer_result(
    customer: MainCustomer,
    attributes: list[IFRS9Attribute],
    loan: CustomerLoan | None = None,
) -> AutofillResult:
    by_code = {attribute.code: attribute for attribute in attributes}
    values: dict[int, str] = {}
    labels: list[str] = []

    _set_autofill_value(values, labels, by_code.get("GENDER"), _gender_option(by_code.get("GENDER"), customer.gender) if by_code.get("GENDER") else None)
    _set_autofill_value(values, labels, by_code.get("AGE"), _age_option(by_code.get("AGE"), customer.birth_date) if by_code.get("AGE") else None)
    _set_autofill_value(values, labels, by_code.get("MARITAL_STATUS"), _marital_option(by_code.get("MARITAL_STATUS"), customer.marital_status) if by_code.get("MARITAL_STATUS") else None)
    _set_autofill_value(values, labels, by_code.get("CITIZENSHIP_RESIDENCE_STATUS"), _resident_option(by_code.get("CITIZENSHIP_RESIDENCE_STATUS"), customer.resident_status) if by_code.get("CITIZENSHIP_RESIDENCE_STATUS") else None)
    _set_autofill_value(values, labels, by_code.get("RESIDENTIAL_STATUS"), _residential_status_option(by_code.get("RESIDENTIAL_STATUS"), customer.accommodation_type) if by_code.get("RESIDENTIAL_STATUS") else None)
    _set_autofill_value(values, labels, by_code.get("NET_MONTHLY_INCOME"), _income_option(by_code.get("NET_MONTHLY_INCOME"), customer, monthly=True) if by_code.get("NET_MONTHLY_INCOME") else None)
    _set_autofill_value(values, labels, by_code.get("INCOME_STREAMS"), _income_stream_option(by_code.get("INCOME_STREAMS"), customer) if by_code.get("INCOME_STREAMS") else None)
    _set_autofill_value(values, labels, by_code.get("OCCUPATIONAL_STATUS"), _occupation_option(by_code.get("OCCUPATIONAL_STATUS"), customer) if by_code.get("OCCUPATIONAL_STATUS") else None)
    _set_autofill_value(values, labels, by_code.get("TYPE_OF_EMPLOYER"), _type_of_employer_option(by_code.get("TYPE_OF_EMPLOYER"), customer) if by_code.get("TYPE_OF_EMPLOYER") else None)

    result = _build_autofill_result(
        attributes,
        values,
        labels,
        build_customer_profile_snapshot(customer),
    )
    return _merge_loan_autofill(result, attributes, loan, allow_descriptive_loan_fields=False)


def _build_ifrs9_corporate_result(
    template_code: str,
    customer: CustomerCorporate,
    attributes: list[IFRS9Attribute],
    loan: CustomerLoan | None = None,
) -> AutofillResult:
    by_code = {attribute.code: attribute for attribute in attributes}
    values: dict[int, str] = {}
    labels: list[str] = []

    def apply(attribute_code: str, option: IFRS9Option | None) -> None:
        attribute = by_code.get(attribute_code)
        if attribute:
            _set_autofill_value(values, labels, attribute, option)

    if template_code == "IFRS9PD-CORPORATE-001":
        attribute = by_code.get("BUSINESS_LIFE_CYCLE_STAGE")
        apply("BUSINESS_LIFE_CYCLE_STAGE", _ifrs9_corporate_lifecycle_option(attribute, customer) if attribute else None)
        attribute = by_code.get("PRODUCT_RANGE_DIVERSIFICATION")
        apply("PRODUCT_RANGE_DIVERSIFICATION", _ifrs9_product_diversification_option(attribute, customer) if attribute else None)
    elif template_code == "IFRS9PD-FARMING-001":
        attribute = by_code.get("EXPERIENCE")
        apply("EXPERIENCE", _ifrs9_farming_experience_option(attribute, customer) if attribute else None)
    elif template_code == "IFRS9PD-LOCALAUTHORITIES-001":
        attribute = by_code.get("LOCAL_AUTHORITY_CLASSIFICATION")
        apply("LOCAL_AUTHORITY_CLASSIFICATION", _local_authority_classification_option(attribute, customer) if attribute else None)
    elif template_code == "IFRS9PD-MFINANCE-001":
        attribute = by_code.get("NATURE_OF_BORROWER")
        apply("NATURE_OF_BORROWER", _mfinance_nature_option(attribute, customer) if attribute else None)
        attribute = by_code.get("BUSINESS_EXPERIENCE")
        apply("BUSINESS_EXPERIENCE", _mfinance_business_experience_option(attribute, customer) if attribute else None)
    elif template_code == "IFRS9PD-RETAIL-001":
        attribute = by_code.get("BUSINESS_TRACK_RECORD")
        apply("BUSINESS_TRACK_RECORD", _ifrs9_retail_track_record_option(attribute, customer) if attribute else None)
        attribute = by_code.get("OWNERSHIP_STRUCTURE")
        apply("OWNERSHIP_STRUCTURE", _ifrs9_retail_ownership_option(attribute, customer) if attribute else None)
    elif template_code == "IFRS9PD-SCHOOLS-001":
        attribute = by_code.get("OWNERSHIP_GOVERNANCE")
        apply("OWNERSHIP_GOVERNANCE", _school_ownership_option(attribute, customer) if attribute else None)
        attribute = by_code.get("TYPE_OF_SCHOOL")
        apply("TYPE_OF_SCHOOL", _school_type_option(attribute, customer) if attribute else None)
    elif template_code == "IFRS9PD-TERTIARY-001":
        attribute = by_code.get("OWNERSHIP_GOVERNANCE")
        apply("OWNERSHIP_GOVERNANCE", _tertiary_ownership_option(attribute, customer) if attribute else None)
        attribute = by_code.get("TYPE_OF_INSTITUTION")
        apply("TYPE_OF_INSTITUTION", _tertiary_type_option(attribute, customer) if attribute else None)

    result = _build_autofill_result(
        attributes,
        values,
        labels,
        build_corporate_profile_snapshot(customer),
    )
    return _merge_loan_autofill(result, attributes, loan, allow_descriptive_loan_fields=False)


def build_ifrs9_autofill(
    template_code: str,
    customer: MainCustomer | None,
    attributes_by_driver: dict[int, list[IFRS9Attribute]],
    corporate_customer: CustomerCorporate | None = None,
    loan: CustomerLoan | None = None,
) -> AutofillResult:
    if customer is None:
        return AutofillResult({}, [], [], [], [], {})

    normalized_template_code = (template_code or "").upper()
    attributes = [attribute for attrs in attributes_by_driver.values() for attribute in attrs]
    loan = loan or _latest_customer_loan(customer)
    if normalized_template_code == IFRS9_CONSUMER_TEMPLATE_CODE:
        return _build_ifrs9_consumer_result(customer, attributes, loan)
    if normalized_template_code not in IFRS9_CORPORATE_TEMPLATE_CODES:
        return AutofillResult({}, [], [], [], [], {})

    corporate_customer = corporate_customer or _customer_corporate_profile(customer)
    if corporate_customer is None:
        return _build_autofill_result(attributes, {}, [], {})
    return _build_ifrs9_corporate_result(
        normalized_template_code,
        corporate_customer,
        attributes,
        loan,
    )


def build_ifrs9_consumer_autofill(
    template_code: str,
    customer: MainCustomer | None,
    attributes_by_driver: dict[int, list[IFRS9Attribute]],
) -> AutofillResult:
    return build_ifrs9_autofill(template_code, customer, attributes_by_driver)
