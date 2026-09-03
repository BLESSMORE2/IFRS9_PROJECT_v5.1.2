from django.core.management.base import BaseCommand

from scorecard.scheduler_runtime import run_scheduler_loop


class Command(BaseCommand):
    help = "Run a continuous Django-based scheduler service for API import schedules."

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval",
            type=int,
            default=30,
            help="How many seconds to wait between schedule checks. Default is 30.",
        )
        parser.add_argument(
            "--run-once",
            action="store_true",
            help="Check schedules once and exit.",
        )

    def handle(self, *args, **options):
        interval = max(5, options["interval"])
        run_once = options["run_once"]

        def _writer(level: str, message: str) -> None:
            if level == "success":
                self.stdout.write(self.style.SUCCESS(message))
            elif level == "warning":
                self.stdout.write(self.style.WARNING(message))
            elif level == "error":
                self.stderr.write(self.style.ERROR(message))
            else:
                self.stdout.write(message)

        run_scheduler_loop(interval=interval, run_once=run_once, writer=_writer)
