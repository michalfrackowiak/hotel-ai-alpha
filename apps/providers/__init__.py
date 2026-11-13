"""Pluggable LLM connectors for the hotel receptionist."""

from .base import (
    BaseLLMProvider,
    ChatMessage,
    LLMConfigurationError,
    LLMProviderError,
    LLMResponse,
    ProviderConfig,
    ProviderHealth,
    ToolCall,
    ToolSpec,
)
from .registry import PROVIDER_CLASSES, build_provider, provider_class, resolve_provider, test_provider

__all__ = [
    "BaseLLMProvider",
    "ChatMessage",
    "LLMConfigurationError",
    "LLMProviderError",
    "LLMResponse",
    "ProviderConfig",
    "ProviderHealth",
    "ToolCall",
    "ToolSpec",
    "PROVIDER_CLASSES",
    "build_provider",
    "provider_class",
    "resolve_provider",
    "test_provider",
]
