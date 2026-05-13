from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("Users", "0012_add_scorecard_module"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserAccessLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("session_key", models.CharField(db_index=True, max_length=64)),
                ("login_time", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("logout_time", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("session_duration_seconds", models.PositiveIntegerField(blank=True, null=True)),
                (
                    "end_reason",
                    models.CharField(
                        choices=[
                            ("active", "Active"),
                            ("manual_logout", "Manual Sign Out"),
                            ("idle_timeout", "Idle Timeout"),
                            ("absolute_timeout", "Maximum Session Timeout"),
                        ],
                        db_index=True,
                        default="active",
                        max_length=32,
                    ),
                ),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.CharField(blank=True, default="", max_length=255)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="access_log_entries",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "User Access Log",
                "verbose_name_plural": "User Access Logs",
                "ordering": ["-login_time", "-id"],
            },
        ),
    ]
