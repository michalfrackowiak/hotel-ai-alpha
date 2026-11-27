# Generated manually for phone call domain model.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("agents", "0006_agentsettings_phone_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="PhoneCall",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "provider",
                    models.CharField(
                        choices=[("openline", "OpenLine")],
                        default="openline",
                        max_length=30,
                        verbose_name="provider",
                    ),
                ),
                ("external_id", models.CharField(max_length=128, unique=True, verbose_name="external call id")),
                (
                    "direction",
                    models.CharField(
                        choices=[("inbound", "Inbound"), ("outbound", "Outbound")],
                        default="inbound",
                        max_length=16,
                        verbose_name="direction",
                    ),
                ),
                ("from_number", models.CharField(blank=True, max_length=64, verbose_name="from number")),
                ("to_number", models.CharField(blank=True, max_length=64, verbose_name="to number")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("ringing", "Ringing"),
                            ("answered", "Answered"),
                            ("active", "Active"),
                            ("ended", "Ended"),
                            ("failed", "Failed"),
                            ("transferred", "Transferred"),
                        ],
                        db_index=True,
                        default="ringing",
                        max_length=20,
                        verbose_name="status",
                    ),
                ),
                ("started_at", models.DateTimeField(blank=True, null=True, verbose_name="started at")),
                ("answered_at", models.DateTimeField(blank=True, null=True, verbose_name="answered at")),
                ("ended_at", models.DateTimeField(blank=True, null=True, verbose_name="ended at")),
                ("duration_seconds", models.PositiveIntegerField(blank=True, null=True, verbose_name="duration (seconds)")),
                ("recording_url", models.URLField(blank=True, verbose_name="recording URL")),
                ("transcript", models.JSONField(blank=True, default=list, verbose_name="transcript")),
                ("summary", models.TextField(blank=True, verbose_name="summary")),
                ("metadata", models.JSONField(blank=True, default=dict, verbose_name="metadata")),
                (
                    "conversation",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="phone_calls",
                        to="agents.conversation",
                        verbose_name="conversation",
                    ),
                ),
            ],
            options={
                "verbose_name": "phone call",
                "verbose_name_plural": "phone calls",
                "ordering": ["-created_at"],
            },
        ),
    ]
