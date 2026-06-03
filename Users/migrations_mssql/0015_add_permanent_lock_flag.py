from django.db import migrations, models
from Users.migration_safe_ops import AddFieldIfMissing


class Migration(migrations.Migration):

    dependencies = [
        ("Users", "0014_add_repeat_lockout_fields"),
    ]

    operations = [
        AddFieldIfMissing(
            model_name="customuser",
            name="permanently_locked",
            field=models.BooleanField(default=False),
        ),
    ]
