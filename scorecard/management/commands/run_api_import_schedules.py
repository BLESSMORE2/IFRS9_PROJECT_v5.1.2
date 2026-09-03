from django.core.management.base import BaseCommand
from django.utils import timezone

from scorecard.functions_view.api import _format_duration, run_due_import_schedules


class Command(BaseCommand):
    help = "Run due API import schedules for the scorecard API module."

    def handle(self, *args, **options):
        results = run_due_import_schedules(timezone.now())
        if not results:
            self.stdout.write(self.style.WARNING("No due API import schedules were found."))
            return

        self.stdout.write(self.style.WARNING(f"Running {len(results)} due API import schedule(s)."))

        for result in results:
            schedule = result["schedule"]
            if result["status"] == "success":
                stats = result["stats"]
                duration_seconds = result["duration_seconds"]
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Completed {schedule.name}: "
                        f"fetched={stats.fetched}, created={stats.created}, updated={stats.updated}, "
                        f"unchanged={stats.unchanged}, skipped={stats.skipped}, "
                        f"duration={_format_duration(duration_seconds)}"
                    )
                )
            else:
                self.stderr.write(self.style.ERROR(f"Schedule failed for {schedule.name}: {result['error']}"))
