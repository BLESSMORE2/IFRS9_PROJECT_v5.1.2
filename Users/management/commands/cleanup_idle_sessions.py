from django.core.management.base import BaseCommand
from django.utils import timezone

from Users.access_logs import reconcile_stale_user_session_logs, sweep_idle_user_sessions


class Command(BaseCommand):
    help = "Close and remove idle authenticated user sessions from the live session register."

    def handle(self, *args, **options):
        now = timezone.now()
        closed_idle = sweep_idle_user_sessions(ended_at=now)
        closed_stale = reconcile_stale_user_session_logs(ended_at=now)
        total_closed = closed_idle + closed_stale

        self.stdout.write(
            self.style.SUCCESS(
                f"Closed {total_closed} idle or stale authenticated session log(s)."
            )
        )
