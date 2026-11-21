"""Provider registry and the offline demo policy."""

from __future__ import annotations

from datetime import timedelta

import pytest

from apps.agents.models import AgentSettings, AIProvider
from apps.providers import LLMConfigurationError, ProviderConfig, resolve_provider
from apps.providers.demo import DemoProvider
from apps.providers.registry import build_provider, provider_class


def test_demo_provider_is_always_healthy():
    provider = DemoProvider(ProviderConfig(model="demo-policy-v1"))

    health = provider.health_check()

    assert health.ok is True
    assert provider.provider_type == "demo"


def test_unknown_provider_type_raises():
    with pytest.raises(LLMConfigurationError):
        provider_class("does-not-exist")


@pytest.mark.django_db
def test_resolve_provider_falls_back_to_demo_without_records():
    record, provider = resolve_provider()

    assert record is None
    assert isinstance(provider, DemoProvider)


@pytest.mark.django_db
def test_resolve_provider_uses_default_record():
    demo = AIProvider.objects.create(
        name="Demo",
        slug="demo",
        provider_type=AIProvider.Type.DEMO,
        model="demo-policy-v1",
        is_active=True,
        is_default=True,
    )
    settings_obj = AgentSettings.load()
    settings_obj.default_provider = demo
    settings_obj.save()

    record, provider = resolve_provider(settings_obj)

    assert record == demo
    assert isinstance(provider, DemoProvider)
    assert build_provider(demo).provider_type == "demo"


@pytest.mark.django_db
def test_only_one_default_provider_is_kept():
    first = AIProvider.objects.create(
        name="First", provider_type=AIProvider.Type.DEMO, model="demo", is_default=True
    )
    second = AIProvider.objects.create(
        name="Second", provider_type=AIProvider.Type.DEMO, model="demo", is_default=True
    )

    first.refresh_from_db()
    assert first.is_default is False
    assert second.is_default is True


@pytest.mark.django_db
def test_provider_slug_is_generated():
    provider = AIProvider.objects.create(
        name="My OpenAI", provider_type=AIProvider.Type.OPENAI, model="gpt-4o-mini"
    )

    assert provider.slug == "my-openai"


def test_demo_policy_strips_sentence_punctuation_from_email():
    provider = DemoProvider(ProviderConfig(model="demo-policy-v1"))

    slots = provider._extract_slots(
        "", "My name is Anna Kowalska and my email is anna@example.com."
    )

    assert slots["email"] == "anna@example.com"


def test_demo_extracts_natural_language_date_ranges():
    from datetime import date

    from django.utils import timezone

    provider = DemoProvider(ProviderConfig(model="demo-policy-v1"))
    today = timezone.localdate()

    def nov(day: int):
        candidate = date(today.year, 11, day)
        return candidate if candidate >= today else date(today.year + 1, 11, day)

    slots = provider._extract_slots("", "check in 20 of November check out 22 of November")
    assert slots["check_in"] == nov(20)
    assert slots["check_out"] == nov(22)

    slots = provider._extract_slots(
        "", "arrival on the 20th of November and check out on the 22nd of November"
    )
    assert slots["check_in"] == nov(20)
    assert slots["check_out"] == nov(22)


def test_demo_extracts_relative_start_with_duration():
    from django.utils import timezone

    provider = DemoProvider(ProviderConfig(model="demo-policy-v1"))
    today = timezone.localdate()

    slots = provider._extract_slots("", "from tomorrow for 5 days")
    assert slots["check_in"] == today + timedelta(days=1)
    assert slots["check_out"] == today + timedelta(days=6)


def test_demo_extracts_word_number_duration_phrases():
    from django.utils import timezone

    provider = DemoProvider(ProviderConfig(model="demo-policy-v1"))
    today = timezone.localdate()

    slots = provider._extract_slots("", "from tomorrow for three nights")
    assert slots["check_in"] == today + timedelta(days=1)
    assert slots["check_out"] == today + timedelta(days=4)

    slots = provider._extract_slots("", "check in tomorrow for a couple of nights")
    assert slots["check_in"] == today + timedelta(days=1)
    assert slots["check_out"] == today + timedelta(days=3)

    slots = provider._extract_slots("", "staying for a few nights starting tomorrow")
    assert slots["check_in"] == today + timedelta(days=1)
    assert slots["check_out"] == today + timedelta(days=4)

    slots = provider._extract_slots("", "tomorrow for a three-night stay")
    assert slots["check_in"] == today + timedelta(days=1)
    assert slots["check_out"] == today + timedelta(days=4)


def test_demo_extracts_word_number_nights_as_departure_answer():
    from django.utils import timezone

    from apps.providers.base import ChatMessage

    provider = DemoProvider(ProviderConfig(model="demo-policy-v1"))
    today = timezone.localdate()
    check_in = today + timedelta(days=10)
    messages = [
        ChatMessage(role="assistant", content="Please give me your departure date or tell me how many nights you plan to stay."),
    ]

    slots = provider._extract_slots(
        f"arrival {check_in.isoformat()}",
        "three nights",
        messages,
    )
    assert slots["check_in"] == check_in
    assert slots["check_out"] == check_in + timedelta(days=3)

    slots = provider._extract_slots(
        f"arrival {check_in.isoformat()}",
        "na trzy noce",
        [
            ChatMessage(
                role="assistant",
                content="Podaj datę wyjazdu lub informację, na ile nocy planujesz pobyt.",
            )
        ],
    )
    assert slots["check_in"] == check_in
    assert slots["check_out"] == check_in + timedelta(days=3)


def test_demo_extracts_yearless_date_ranges():
    from datetime import date

    from django.utils import timezone

    provider = DemoProvider(ProviderConfig(model="demo-policy-v1"))
    today = timezone.localdate()

    def nov(day: int):
        candidate = date(today.year, 11, day)
        return candidate if candidate >= today else date(today.year + 1, 11, day)

    slots = provider._extract_slots("", "20-22 November")
    assert slots["check_in"] == nov(20)
    assert slots["check_out"] == nov(22)

    slots = provider._extract_slots("", "20 do 22 listopada")
    assert slots["check_in"] == nov(20)
    assert slots["check_out"] == nov(22)

    slots = provider._extract_slots("", "from 20.11 to 22.11 for 2 guests")
    assert slots["check_in"] == nov(20)
    assert slots["check_out"] == nov(22)

    slots = provider._extract_slots("", "20/11 - 22/11")
    assert slots["check_in"] == nov(20)
    assert slots["check_out"] == nov(22)


def test_demo_extracts_word_number_relative_dates():
    from django.utils import timezone

    provider = DemoProvider(ProviderConfig(model="demo-policy-v1"))
    today = timezone.localdate()

    slots = provider._extract_slots("", "in two days for three nights")
    assert slots["check_in"] == today + timedelta(days=2)
    assert slots["check_out"] == today + timedelta(days=5)

    slots = provider._extract_slots("", "za trzy dni na dwie noce")
    assert slots["check_in"] == today + timedelta(days=3)
    assert slots["check_out"] == today + timedelta(days=5)


def test_demo_extracts_tonight_and_weekday():
    from django.utils import timezone

    provider = DemoProvider(ProviderConfig(model="demo-policy-v1"))
    today = timezone.localdate()

    slots = provider._extract_slots("", "tonight for two nights")
    assert slots["check_in"] == today
    assert slots["check_out"] == today + timedelta(days=2)

    slots = provider._extract_slots("", "next Friday for 2 nights")
    assert slots["check_in"] is not None
    assert slots["check_in"] > today
    assert slots["check_in"].weekday() == 4
    assert slots["check_out"] == slots["check_in"] + timedelta(days=2)
