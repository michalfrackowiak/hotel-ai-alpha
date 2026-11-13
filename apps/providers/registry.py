"""Provider registry: database records in, configured connector instances out."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .anthropic import AnthropicProvider
from .base import (
    BaseLLMProvider,
    LLMConfigurationError,
    LLMProviderError,
    ProviderConfig,
    ProviderHealth,
)
from .demo import DemoProvider
from .ollama import OllamaProvider
from .openai import OpenAIProvider

if TYPE_CHECKING:
    from apps.agents.models import AgentSettings, AIProvider

logger = logging.getLogger(__name__)

PROVIDER_CLASSES: dict[str, type[BaseLLMProvider]] = {
    OpenAIProvider.provider_type: OpenAIProvider,
    AnthropicProvider.provider_type: AnthropicProvider,
    OllamaProvider.provider_type: OllamaProvider,
    DemoProvider.provider_type: DemoProvider,
}


def provider_class(provider_type: str) -> type[BaseLLMProvider]:
    try:
        return PROVIDER_CLASSES[provider_type]
    except KeyError as exc:
        raise LLMConfigurationError(f"Unknown provider type {provider_type!r}.") from exc


def config_from_record(record: AIProvider) -> ProviderConfig:
    return ProviderConfig(
        model=record.model,
        api_key=record.api_key,
        base_url=record.base_url,
        temperature=record.temperature,
        max_tokens=record.max_tokens,
        timeout=record.timeout_seconds,
        extra=record.extra or {},
    )


def build_provider(record: AIProvider) -> BaseLLMProvider:
    cls = provider_class(record.provider_type)
    return cls(config_from_record(record))


def demo_provider() -> DemoProvider:
    return DemoProvider(ProviderConfig(model="demo-policy-v1"))


def resolve_provider(settings_obj: AgentSettings | None = None) -> tuple[AIProvider | None, BaseLLMProvider]:
    """Pick the configured default provider, then any active provider, then demo."""
    from apps.agents.models import AgentSettings, AIProvider

    if settings_obj is None:
        settings_obj = AgentSettings.load()

    record = settings_obj.default_provider
    if record is not None and not record.is_active:
        record = None
    if record is None:
        record = AIProvider.objects.filter(is_active=True).order_by("-is_default", "pk").first()

    if record is None:
        return None, demo_provider()

    try:
        return record, build_provider(record)
    except LLMProviderError:
        logger.warning("Provider %s is not usable.", record.slug)
        raise


def test_provider(record: AIProvider) -> ProviderHealth:
    try:
        return build_provider(record).health_check()
    except LLMProviderError as exc:
        return ProviderHealth(ok=False, message=exc.message)
    except Exception as exc:  # noqa: BLE001 - surface any connector bug in the admin
        return ProviderHealth(ok=False, message=f"Unexpected error: {exc}")
