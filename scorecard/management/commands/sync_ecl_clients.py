from django.core.management.base import BaseCommand, CommandError

from scorecard.functions_view.api import (
    fetch_paginated_clients,
    upsert_corporate_clients,
    upsert_individual_clients,
)


class Command(BaseCommand):
    help = (
        "Sync corporate and individual client master data from the ECL API into the "
        "API detail tables (CustomerCorporate and CustomerIndividual)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--base-url",
            required=True,
            help="Base API URL, e.g. http://192.168.4.48:8090",
        )
        parser.add_argument(
            "--api-key",
            required=True,
            help="API key for the ECL service.",
        )
        parser.add_argument(
            "--corporate-reporting-date",
            help="Reporting date for the corporate endpoint (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--individual-reporting-date",
            help="Reporting date for the individual endpoint (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--page-size",
            type=int,
            default=1000,
            help="Page size for both endpoints. Default is 1000.",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=60,
            help="HTTP timeout in seconds. Default is 60.",
        )
        parser.add_argument(
            "--types",
            nargs="+",
            choices=["corporate", "individual"],
            default=["corporate", "individual"],
            help="Which client types to sync. Default is both.",
        )

    def handle(self, *args, **options):
        base_url = options["base_url"]
        api_key = options["api_key"]
        page_size = options["page_size"]
        timeout = options["timeout"]
        types = set(options["types"])

        if "corporate" in types and not options.get("corporate_reporting_date"):
            raise CommandError("--corporate-reporting-date is required when syncing corporate clients.")

        if "individual" in types and not options.get("individual_reporting_date"):
            raise CommandError("--individual-reporting-date is required when syncing individual clients.")

        if "corporate" in types:
            corporate_date = options["corporate_reporting_date"]
            self.stdout.write(self.style.WARNING(f"Fetching corporate clients for reporting_date={corporate_date}"))
            corporate_records = fetch_paginated_clients(
                base_url=base_url,
                api_key=api_key,
                endpoint_path="/api/v1/ecl/clients/corporate",
                reporting_date=corporate_date,
                page_size=page_size,
                timeout=timeout,
            )
            corporate_stats = upsert_corporate_clients(corporate_records)
            self.stdout.write(
                self.style.SUCCESS(
                    "Corporate sync complete: "
                    f"fetched={corporate_stats.fetched}, "
                    f"created={corporate_stats.created}, "
                    f"updated={corporate_stats.updated}, "
                    f"unchanged={corporate_stats.unchanged}, "
                    f"skipped={corporate_stats.skipped}, "
                    f"duplicates={corporate_stats.duplicate_skipped}, "
                    f"missing_required={corporate_stats.missing_required_skipped}"
                )
            )

        if "individual" in types:
            individual_date = options["individual_reporting_date"]
            self.stdout.write(self.style.WARNING(f"Fetching individual clients for reporting_date={individual_date}"))
            individual_records = fetch_paginated_clients(
                base_url=base_url,
                api_key=api_key,
                endpoint_path="/api/v1/ecl/clients/individual",
                reporting_date=individual_date,
                page_size=page_size,
                timeout=timeout,
            )
            individual_stats = upsert_individual_clients(individual_records)
            self.stdout.write(
                self.style.SUCCESS(
                    "Individual sync complete: "
                    f"fetched={individual_stats.fetched}, "
                    f"created={individual_stats.created}, "
                    f"updated={individual_stats.updated}, "
                    f"unchanged={individual_stats.unchanged}, "
                    f"skipped={individual_stats.skipped}, "
                    f"duplicates={individual_stats.duplicate_skipped}, "
                    f"missing_required={individual_stats.missing_required_skipped}"
                )
            )
