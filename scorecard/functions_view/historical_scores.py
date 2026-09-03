import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from django.utils import timezone

from scorecard.models import CustomerLoan, CustomerOverdraft, CreditEvaluation, HistoricalScore, IFRS9Evaluation


APPROVED_LIKE_STATUSES = ("approved", "submitted", "returned", "completed")


@dataclass
class HistoricalScoreCaptureResult:
    reporting_date: date
    created: int
    updated: int
    both: int
    basel_only: int
    ifrs9_only: int

    @property
    def total_rows(self) -> int:
        return self.created + self.updated


def month_end(value: date) -> date:
    last_day = calendar.monthrange(value.year, value.month)[1]
    return value.replace(day=last_day)


def previous_month_end(value: date) -> date:
    if value.month == 1:
        previous_month_date = value.replace(year=value.year - 1, month=12, day=1)
    else:
        previous_month_date = value.replace(month=value.month - 1, day=1)
    return month_end(previous_month_date)


def next_month_end(value: date) -> date:
    if value.month == 12:
        next_month_date = value.replace(year=value.year + 1, month=1, day=1)
    else:
        next_month_date = value.replace(month=value.month + 1, day=1)
    return month_end(next_month_date)


def is_month_end(value: date) -> bool:
    return value == month_end(value)


def normalize_reporting_date(value: date | datetime | None = None) -> date:
    if value is None:
        base_date = timezone.localdate()
    elif isinstance(value, datetime):
        if timezone.is_naive(value):
            value = timezone.make_aware(value, timezone.get_current_timezone())
        base_date = timezone.localtime(value).date()
    else:
        base_date = value
    return month_end(base_date)


def missing_historical_month_ends(
    captured_reporting_dates: set[date],
    latest_due_reporting_date: date,
    local_date: date,
) -> list[date]:
    if not captured_reporting_dates:
        bootstrap_previous_reporting_date = previous_month_end(local_date)
        candidate_dates: list[date] = []
        if bootstrap_previous_reporting_date <= latest_due_reporting_date:
            candidate_dates.append(bootstrap_previous_reporting_date)
        if latest_due_reporting_date not in candidate_dates:
            candidate_dates.append(latest_due_reporting_date)
        return candidate_dates

    candidate_dates = []
    cursor = min(captured_reporting_dates)
    while True:
        cursor = next_month_end(cursor)
        if cursor > latest_due_reporting_date:
            break
        candidate_dates.append(cursor)
    return candidate_dates


def _historical_date_has_all_seed_rows(reporting_date: date, seed_rows: list[dict[str, Any]]) -> bool:
    if not seed_rows:
        return True

    expected_keys = {(row["branch_name"], row["customer_id"]) for row in seed_rows}
    existing_count = HistoricalScore.objects.filter(reporting_date=reporting_date).count()
    if existing_count < len(expected_keys):
        return False

    existing_keys = set(
        HistoricalScore.objects.filter(reporting_date=reporting_date)
        .values_list("branch_name", "customer_id")
    )
    return expected_keys.issubset(existing_keys)


def _clean_text(value: Any) -> str:
    return (str(value or "")).strip()


def _basel_current_score(evaluation: CreditEvaluation):
    if evaluation.status in {"submitted", "returned"} and evaluation.approved_weighted_percent is not None:
        return evaluation.approved_weighted_percent
    return evaluation.total_weighted_percent


def _basel_current_grade(evaluation: CreditEvaluation) -> str:
    if evaluation.status in {"submitted", "returned"} and evaluation.approved_grade:
        return evaluation.approved_grade
    return evaluation.final_grade or ""


def _basel_override_grade(evaluation: CreditEvaluation) -> str:
    return _clean_text(getattr(evaluation, "override_grade", ""))


def _ifrs9_current_score(evaluation: IFRS9Evaluation):
    if evaluation.status in {"submitted", "returned"} and evaluation.approved_weighted_percent is not None:
        return evaluation.approved_weighted_percent
    return evaluation.total_weighted_percent


def _active_exposure_customer_codes_for_reporting_date(reporting_date: date) -> set[str]:
    """Customers are eligible for history only when active loan/overdraft data exists for that month."""

    loan_codes = (
        CustomerLoan.objects.filter(reporting_date=reporting_date)
        .exclude(customer_code__isnull=True)
        .exclude(customer_code__exact="")
        .values_list("customer_code", flat=True)
        .distinct()
    )
    overdraft_codes = (
        CustomerOverdraft.objects.filter(reporting_date=reporting_date)
        .exclude(customer_code__isnull=True)
        .exclude(customer_code__exact="")
        .values_list("customer_code", flat=True)
        .distinct()
    )
    return {_clean_text(code) for code in loan_codes}.union({_clean_text(code) for code in overdraft_codes})


def build_historical_score_seed_rows(reporting_date: date | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    active_customer_codes = _active_exposure_customer_codes_for_reporting_date(reporting_date) if reporting_date else None

    basel_rows = CreditEvaluation.objects.filter(status__in=APPROVED_LIKE_STATUSES).order_by(
        "branch_name", "customer_id", "-updated_at", "-id"
    )
    ifrs9_rows = IFRS9Evaluation.objects.filter(status__in=APPROVED_LIKE_STATUSES).order_by(
        "branch_name", "customer_id", "-updated_at", "-id"
    )

    for evaluation in basel_rows:
        branch_name = _clean_text(evaluation.branch_name)
        customer_id = _clean_text(evaluation.customer_id)
        if not branch_name or not customer_id:
            continue
        if active_customer_codes is not None and customer_id not in active_customer_codes:
            continue
        key = (branch_name, customer_id)
        if key in rows_by_key and rows_by_key[key].get("basel_loaded"):
            continue
        rows_by_key.setdefault(
            key,
            {
                "branch_name": branch_name,
                "customer_name": _clean_text(evaluation.customer_name),
                "customer_id": customer_id,
                "basel_ii_score": None,
                "basel_ii_grade": "",
                "basel_override_grade": "",
                "ifrs_9_score": None,
                "basel_loaded": False,
                "ifrs9_loaded": False,
            },
        )
        rows_by_key[key]["customer_name"] = rows_by_key[key]["customer_name"] or _clean_text(evaluation.customer_name)
        rows_by_key[key]["basel_ii_score"] = _basel_current_score(evaluation)
        rows_by_key[key]["basel_ii_grade"] = _basel_current_grade(evaluation)
        rows_by_key[key]["basel_override_grade"] = _basel_override_grade(evaluation)
        rows_by_key[key]["basel_loaded"] = True

    for evaluation in ifrs9_rows:
        branch_name = _clean_text(evaluation.branch_name)
        customer_id = _clean_text(evaluation.customer_id)
        if not branch_name or not customer_id:
            continue
        if active_customer_codes is not None and customer_id not in active_customer_codes:
            continue
        key = (branch_name, customer_id)
        if key in rows_by_key and rows_by_key[key].get("ifrs9_loaded"):
            continue
        rows_by_key.setdefault(
            key,
            {
                "branch_name": branch_name,
                "customer_name": _clean_text(evaluation.customer_name),
                "customer_id": customer_id,
                "basel_ii_score": None,
                "basel_ii_grade": "",
                "basel_override_grade": "",
                "ifrs_9_score": None,
                "basel_loaded": False,
                "ifrs9_loaded": False,
            },
        )
        rows_by_key[key]["customer_name"] = rows_by_key[key]["customer_name"] or _clean_text(evaluation.customer_name)
        rows_by_key[key]["ifrs_9_score"] = _ifrs9_current_score(evaluation)
        rows_by_key[key]["ifrs9_loaded"] = True

    prepared_rows: list[dict[str, Any]] = []
    coverage_totals = defaultdict(int)
    for payload in rows_by_key.values():
        payload.pop("basel_loaded", None)
        payload.pop("ifrs9_loaded", None)
        if (
            payload["basel_ii_score"] is None
            and not payload["basel_ii_grade"]
            and not payload["basel_override_grade"]
            and payload["ifrs_9_score"] is None
        ):
            continue
        if payload["basel_ii_score"] is not None and payload["ifrs_9_score"] is not None:
            coverage_totals["both"] += 1
        elif payload["basel_ii_score"] is not None or payload["basel_ii_grade"] or payload["basel_override_grade"]:
            coverage_totals["basel_only"] += 1
        else:
            coverage_totals["ifrs9_only"] += 1
        prepared_rows.append(payload)

    return prepared_rows, dict(coverage_totals)


def build_historical_score_payloads(
    reporting_date: date,
    seed_rows: list[dict[str, Any]] | None = None,
    coverage_totals: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if seed_rows is None or coverage_totals is None:
        seed_rows, coverage_totals = build_historical_score_seed_rows(reporting_date)
    prepared_rows = [{**payload, "reporting_date": reporting_date} for payload in seed_rows]
    return prepared_rows, dict(coverage_totals)


def capture_historical_scores(
    reporting_date: date | datetime | None = None,
    seed_rows: list[dict[str, Any]] | None = None,
    coverage_totals: dict[str, int] | None = None,
) -> HistoricalScoreCaptureResult:
    normalized_reporting_date = normalize_reporting_date(reporting_date)
    prepared_rows, coverage_totals = build_historical_score_payloads(
        normalized_reporting_date,
        seed_rows=seed_rows,
        coverage_totals=coverage_totals,
    )

    created_count = 0
    updated_count = 0
    for payload in prepared_rows:
        _, created = HistoricalScore.objects.update_or_create(
            reporting_date=payload["reporting_date"],
            branch_name=payload["branch_name"],
            customer_id=payload["customer_id"],
            defaults={
                "customer_name": payload["customer_name"],
                "basel_ii_score": payload["basel_ii_score"],
                "basel_ii_grade": payload["basel_ii_grade"],
                "basel_override_grade": payload.get("basel_override_grade", ""),
                "ifrs_9_score": payload["ifrs_9_score"],
            },
        )
        if created:
            created_count += 1
        else:
            updated_count += 1

    return HistoricalScoreCaptureResult(
        reporting_date=normalized_reporting_date,
        created=created_count,
        updated=updated_count,
        both=coverage_totals.get("both", 0),
        basel_only=coverage_totals.get("basel_only", 0),
        ifrs9_only=coverage_totals.get("ifrs9_only", 0),
    )


def run_due_historical_score_capture(now: datetime | None = None) -> dict[str, Any]:
    current_value = now or timezone.localtime()
    if timezone.is_naive(current_value):
        current_value = timezone.make_aware(current_value, timezone.get_current_timezone())
    local_date = timezone.localtime(current_value).date()
    latest_due_reporting_date = (
        normalize_reporting_date(local_date) if is_month_end(local_date) else previous_month_end(local_date)
    )
    captured_reporting_dates = set(
        HistoricalScore.objects.filter(reporting_date__lte=latest_due_reporting_date)
        .order_by()
        .values_list("reporting_date", flat=True)
        .distinct()
    )
    # Capture only the most recent due month-end. This prevents old gaps from
    # being filled months later and keeps scheduler work bounded.
    candidate_dates = [] if latest_due_reporting_date in captured_reporting_dates else [latest_due_reporting_date]

    captured_results: list[HistoricalScoreCaptureResult] = []
    skipped_dates: list[date] = []
    no_active_exposure_dates: list[date] = []
    for reporting_date in candidate_dates:
        seed_rows, coverage_totals = build_historical_score_seed_rows(reporting_date)
        if not seed_rows:
            no_active_exposure_dates.append(reporting_date)
            continue
        if _historical_date_has_all_seed_rows(reporting_date, seed_rows):
            skipped_dates.append(reporting_date)
            continue
        captured_results.append(
            capture_historical_scores(
                reporting_date,
                seed_rows=seed_rows,
                coverage_totals=coverage_totals,
            )
        )

    if not captured_results:
        latest_checked = candidate_dates[-1] if candidate_dates else latest_due_reporting_date
        if no_active_exposure_dates:
            return {
                "performed": False,
                "reason": "no_active_exposures",
                "reporting_date": latest_checked,
                "checked_reporting_dates": candidate_dates,
                "message": "No active loan or overdraft exposure was found for the due historical month-end.",
            }
        return {
            "performed": False,
            "reason": "already_captured",
            "reporting_date": latest_checked,
            "checked_reporting_dates": candidate_dates,
            "message": "Historical scores already exist for all due month-end dates.",
        }

    return {
        "performed": True,
        "reason": "captured_multiple" if len(captured_results) > 1 else "captured",
        "reporting_date": captured_results[-1].reporting_date,
        "captured_reporting_dates": [result.reporting_date for result in captured_results],
        "skipped_reporting_dates": skipped_dates,
        "created": sum(result.created for result in captured_results),
        "updated": sum(result.updated for result in captured_results),
        "both": sum(result.both for result in captured_results),
        "basel_only": sum(result.basel_only for result in captured_results),
        "ifrs9_only": sum(result.ifrs9_only for result in captured_results),
        "snapshot_as_of": timezone.localtime(current_value).strftime("%Y-%m-%d %H:%M:%S"),
        "message": (
            "Historical scores captured for "
            + ", ".join(result.reporting_date.strftime("%Y-%m-%d") for result in captured_results)
            + ". "
            + f"Created {sum(result.created for result in captured_results)}, "
            + f"updated {sum(result.updated for result in captured_results)}."
        ),
    }
