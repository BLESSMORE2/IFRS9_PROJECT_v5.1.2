from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from scorecard.functions_view.historical_scores import (
    build_historical_score_payloads,
    capture_historical_scores,
    normalize_reporting_date,
)


class Command(BaseCommand):
    help = "Capture a month-end historical score snapshot from Basel and IFRS9 evaluation tables."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reporting-date",
            type=str,
            help="Reporting date for the snapshot in YYYY-MM-DD format. The command stores the month-end date for that month.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview what would be captured without saving anything.",
        )

    def handle(self, *args, **options):
        reporting_date_raw = options.get("reporting_date")
        dry_run = bool(options.get("dry_run"))

        if reporting_date_raw:
            try:
                requested_date = date.fromisoformat(reporting_date_raw)
            except ValueError as exc:
                raise CommandError("Use --reporting-date in YYYY-MM-DD format.") from exc
        else:
            requested_date = timezone.localdate()

        reporting_date = normalize_reporting_date(requested_date)

        if dry_run:
            prepared_rows, coverage_totals = build_historical_score_payloads(reporting_date)
            self.stdout.write(self.style.WARNING("Dry run only. No rows were saved."))
            self.stdout.write(
                f"Reporting date: {reporting_date:%Y-%m-%d} | Rows prepared: {len(prepared_rows)} | "
                f"Both: {coverage_totals['both']} | Basel only: {coverage_totals['basel_only']} | IFRS9 only: {coverage_totals['ifrs9_only']}"
            )
            return

        result = capture_historical_scores(reporting_date)
        self.stdout.write(self.style.SUCCESS("Historical score snapshot captured successfully."))
        self.stdout.write(
            f"Reporting date: {result.reporting_date:%Y-%m-%d} | Created: {result.created} | Updated: {result.updated} | "
            f"Both: {result.both} | Basel only: {result.basel_only} | IFRS9 only: {result.ifrs9_only}"
        )
