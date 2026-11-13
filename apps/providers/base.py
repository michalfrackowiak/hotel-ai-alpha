"""Provider-agnostic LLM interface.

Every connector receives the same :class:`ChatMessage` objects and returns an
:class:`LLMResponse`. Tool schemas use plain JSON Schema so they can be mapped
to OpenAI, Anthropic or Ollama wire formats.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

ToolSpec = dict[str, Any]


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChatMessage:
    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""


@dataclass(slots=True)
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderHealth:
    ok: bool
    message: str
    latency_ms: int | None = None


@dataclass(slots=True)
class ProviderConfig:
    model: str
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.3
    max_tokens: int = 800
    timeout: float = 45.0
    extra: dict[str, Any] = field(default_factory=dict)


class LLMProviderError(Exception):
    """A provider failed in a way the agent can recover from."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status_code: int | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.details = details or {}


class LLMConfigurationError(LLMProviderError):
    """The provider is missing credentials or settings."""


class BaseLLMProvider(ABC):
    provider_type: str = "base"
    requires_api_key: bool = True

    def __init__(self, config: ProviderConfig):
        self.config = config

    @abstractmethod
    def chat(self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None) -> LLMResponse:
        """Return the next assistant turn."""

    def health_check(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            self.chat([ChatMessage(role="user", content="Reply with the single word OK.")])
        except LLMProviderError as exc:
            return ProviderHealth(ok=False, message=exc.message)
        latency = int((time.perf_counter() - started) * 1000)
        return ProviderHealth(ok=True, message="Connection OK", latency_ms=latency)


def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """Normalise tool arguments that may arrive as a JSON string or a dict."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def tool_spec_name(spec: ToolSpec) -> str:
    return spec.get("name") or spec.get("function", {}).get("name", "")
