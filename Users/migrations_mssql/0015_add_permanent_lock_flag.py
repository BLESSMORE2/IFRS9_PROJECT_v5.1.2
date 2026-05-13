from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("Users", "0014_add_repeat_lockout_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="customuser",
            name="permanently_locked",
            field=models.BooleanField(default=False),
        ),
    ]
