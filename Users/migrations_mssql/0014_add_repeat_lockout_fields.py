from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("Users", "0013_useraccesslog"),
    ]

    operations = [
        migrations.AddField(
            model_name="customuser",
            name="failed_login_attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="customuser",
            name="lock_immediately_on_next_failure",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="customuser",
            name="lockout_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
