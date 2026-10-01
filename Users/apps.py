from django.apps import AppConfig


class UsersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "Users"

    def ready(self):
        from .session_sweeper import start_idle_session_sweeper

        start_idle_session_sweeper()
