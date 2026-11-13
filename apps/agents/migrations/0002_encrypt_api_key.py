from __future__ import annotations

from django.db import migrations

import apps.agents.crypto
import apps.agents.fields


def encrypt_existing_keys(apps, schema_editor):
    AIProvider = apps.get_model("agents", "AIProvider")
    for pk, value in AIProvider.objects.values_list("pk", "api_key"):
        if value and not apps.agents.crypto.is_encrypted(value):
            AIProvider.objects.filter(pk=pk).update(
                api_key=apps.agents.crypto.encrypt(value)
            )


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="aiprovider",
            name="api_key",
            field=apps.agents.fields.EncryptedCharField(
                blank=True, max_length=1024, verbose_name="API key"
            ),
        ),
        migrations.RunPython(encrypt_existing_keys, migrations.RunPython.noop),
    ]
