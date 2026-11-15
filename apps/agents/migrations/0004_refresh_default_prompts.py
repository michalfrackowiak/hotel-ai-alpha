from __future__ import annotations

from django.db import migrations


OLD_PROMPT_MARKER_EN = "Collect the guest's first name, last name, and an email address or phone number."
OLD_PROMPT_MARKER_PL = "Zbierz imię, nazwisko oraz adres e-mail lub numer telefonu gościa."
OLD_GREETING_MARKER = "Mogę sprawdzić dostępność i zarezerwować dla Ciebie pokój."


def refresh_default_prompts(apps, schema_editor):
    """Move existing default prompts and greetings to the 7-step scenario.

    Only rows that still hold the previous defaults are touched, so an operator
    who customised the prompts keeps their text.
    """
    from apps.agents.defaults import GREETING_EN, GREETING_PL, SYSTEM_PROMPT_EN, SYSTEM_PROMPT_PL

    AgentSettings = apps.get_model("agents", "AgentSettings")
    for settings_obj in AgentSettings.objects.all():
        updates = {}
        if OLD_PROMPT_MARKER_EN in (settings_obj.system_prompt_en or ""):
            updates["system_prompt_en"] = SYSTEM_PROMPT_EN
        if OLD_PROMPT_MARKER_PL in (settings_obj.system_prompt_pl or ""):
            updates["system_prompt_pl"] = SYSTEM_PROMPT_PL
        if OLD_GREETING_MARKER in (settings_obj.greeting_pl or ""):
            updates["greeting_pl"] = GREETING_PL
            updates["greeting_en"] = GREETING_EN
        if updates:
            AgentSettings.objects.filter(pk=settings_obj.pk).update(**updates)


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0003_alter_conversation_language"),
    ]

    operations = [
        migrations.RunPython(refresh_default_prompts, migrations.RunPython.noop),
    ]
