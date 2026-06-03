from django.db import migrations, models
from Users.migration_safe_ops import AddFieldIfMissing


class Migration(migrations.Migration):

    dependencies = [
        ("Users", "0013_useraccesslog"),
    ]

    operations = [
        AddFieldIfMissing(
            model_name="customuser",
            name="failed_login_attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        AddFieldIfMissing(
            model_name="customuser",
            name="lock_immediately_on_next_failure",
            field=models.BooleanField(default=False),
        ),
        AddFieldIfMissing(
            model_name="customuser",
            name="lockout_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
