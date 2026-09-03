from django.apps import AppConfig
from django.db.models.signals import post_migrate


class ScorecardConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'scorecard'

    def ready(self):
        from scorecard import signals  # noqa: F401
        from scorecard.default_role_seeder import run_default_role_seeder
        from scorecard.scheduler_runtime import autostart_scheduler_if_needed
        from scorecard.template_seed_seeder import run_template_seed_seeder

        post_migrate.connect(
            run_default_role_seeder,
            sender=self,
            dispatch_uid="scorecard_seed_default_roles",
        )
        post_migrate.connect(
            run_template_seed_seeder,
            sender=self,
            dispatch_uid="scorecard_seed_default_templates",
        )
        autostart_scheduler_if_needed()

