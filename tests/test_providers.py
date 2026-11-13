"""Provider registry and the offline demo policy."""

from __future__ import annotations

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
