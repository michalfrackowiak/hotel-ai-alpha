# Generated manually for phone voice settings.

import django.core.validators
from django.db import migrations, models


def populate_phone_defaults(apps, schema_editor):
    AgentSettings = apps.get_model("agents", "AgentSettings")
    for obj in AgentSettings.objects.all():
        changed = False
        if not obj.phone_greeting_en:
            obj.phone_greeting_en = (
                "Good day, you've reached {hotel}. I'm Aurora, an AI assistant. "
                "This call may be handled by AI. For Polish, say Polish. How can I help you with your reservation?"
            )
            changed = True
        if not obj.phone_greeting_pl:
            obj.phone_greeting_pl = (
                "Dzień dobry, tu {hotel}. Mówi Aurora, asystentka AI. "
                "Ta rozmowa może być obsługiwana przez sztuczną inteligencję. For English, say English. "
                "W czym mogę pomóc przy rezerwacji?"
            )
            changed = True
        if not obj.phone_consent_message_en:
            obj.phone_consent_message_en = (
                "This call is handled by an AI assistant. By continuing, you agree to speak with the AI assistant. "
                "You can ask to speak with a person at any time."
            )
            changed = True
        if not obj.phone_consent_message_pl:
            obj.phone_consent_message_pl = (
                "Ta rozmowa jest obsługiwana przez asystentką AI. Kontynuując, zgadzasz się na rozmowę z asystentem AI. "
                "W każdej chwili możesz poprosić o połączenie z człowiekiem."
            )
            changed = True
        if changed:
            obj.save(
                update_fields=[
                    "phone_greeting_en",
                    "phone_greeting_pl",
                    "phone_consent_message_en",
                    "phone_consent_message_pl",
                ]
            )


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0005_alter_conversation_channel"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentsettings",
            name="phone_enabled",
            field=models.BooleanField(default=False, verbose_name="phone enabled"),
        ),
        migrations.AddField(
            model_name="agentsettings",
            name="phone_greeting_en",
            field=models.TextField(blank=True, verbose_name="phone greeting (English)"),
        ),
        migrations.AddField(
            model_name="agentsettings",
            name="phone_greeting_pl",
            field=models.TextField(blank=True, verbose_name="phone greeting (Polish)"),
        ),
        migrations.AddField(
            model_name="agentsettings",
            name="phone_consent_message_en",
            field=models.TextField(blank=True, verbose_name="phone AI disclosure (English)"),
        ),
        migrations.AddField(
            model_name="agentsettings",
            name="phone_consent_message_pl",
            field=models.TextField(blank=True, verbose_name="phone AI disclosure (Polish)"),
        ),
        migrations.AddField(
            model_name="agentsettings",
            name="phone_max_seconds",
            field=models.PositiveSmallIntegerField(
                default=900,
                validators=[
                    django.core.validators.MinValueValidator(60),
                    django.core.validators.MaxValueValidator(3600),
                ],
                verbose_name="max phone call seconds",
            ),
        ),
        migrations.AddField(
            model_name="agentsettings",
            name="phone_transfer_number",
            field=models.CharField(blank=True, max_length=40, verbose_name="phone transfer number"),
        ),
        migrations.AddField(
            model_name="agentsettings",
            name="phone_silence_timeout_seconds",
            field=models.PositiveSmallIntegerField(
                default=20,
                validators=[
                    django.core.validators.MinValueValidator(5),
                    django.core.validators.MaxValueValidator(120),
                ],
                verbose_name="phone silence timeout (seconds)",
            ),
        ),
        migrations.RunPython(populate_phone_defaults, migrations.RunPython.noop),
    ]
