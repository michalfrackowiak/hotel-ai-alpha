from __future__ import annotations

import secrets

from django.db import migrations, models

import apps.hotel.models


def populate_access_tokens(apps, schema_editor):
    Booking = apps.get_model("hotel", "Booking")
    used: set[str] = set()
    for booking in Booking.objects.all().iterator():
        token = secrets.token_urlsafe(24)
        while token in used:
            token = secrets.token_urlsafe(24)
        used.add(token)
        Booking.objects.filter(pk=booking.pk).update(access_token=token)


class Migration(migrations.Migration):
    dependencies = [
        ("hotel", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="access_token",
            field=models.CharField(blank=True, max_length=64, null=True, verbose_name="access token"),
        ),
        migrations.AddField(
            model_name="booking",
            name="confirmation_email_sent_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="confirmation email sent at"),
        ),
        migrations.RunPython(populate_access_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="booking",
            name="access_token",
            field=models.CharField(
                default=apps.hotel.models.generate_access_token,
                editable=False,
                max_length=64,
                unique=True,
                verbose_name="access token",
            ),
        ),
    ]
