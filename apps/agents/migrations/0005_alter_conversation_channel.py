# Generated manually for phone channel support.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0004_refresh_default_prompts"),
    ]

    operations = [
        migrations.AlterField(
            model_name="conversation",
            name="channel",
            field=models.CharField(
                choices=[("chat", "Chat"), ("voice", "Voice"), ("phone", "Phone")],
                default="chat",
                max_length=10,
                verbose_name="channel",
            ),
        ),
    ]
