"""Ollama connector for self-hosted models."""

from __future__ import annotations

import json
from typing import Any

import httpx

from .base import (
    BaseLLMProvider,
    ChatMessage,
    LLMProviderError,
    LLMResponse,
    ProviderConfig,
    ToolCall,
    ToolSpec,
    parse_tool_arguments,
)


class OllamaProvider(BaseLLMProvider):
    provider_type = "ollama"
    requires_api_key = False
    default_base_url = "http://localhost:11434"

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self.base_url = (config.base_url or self.default_base_url).rstrip("/")

    def _message_payload(self, message: ChatMessage) -> dict[str, Any]:
        if message.role == "assistant" and message.tool_calls:
            return {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        }
                    }
                    for call in message.tool_calls
                ],
            }
        if message.role == "tool":
            return {"role": "tool", "content": message.content, "tool_name": message.name}
        return {"role": message.role, "content": message.content}

    def chat(self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [self._message_payload(message) for message in messages],
            "stream": False,
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.max_tokens,
            },
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": spec} for spec in tools]

        try:
            with httpx.Client(timeout=self.config.timeout) as client:
                response = client.post(f"{self.base_url}/api/chat", json=payload)
        except httpx.TimeoutException as exc:
            raise LLMProviderError("Ollama timed out.", provider=self.provider_type) from exc
        except httpx.RequestError as exc:
            raise LLMProviderError(
                "Could not reach Ollama. Is the server running?", provider=self.provider_type, retryable=True
            ) from exc

        if response.status_code >= 400:
            raise LLMProviderError(
                f"Ollama returned an error ({response.status_code}): {response.text[:300]}",
                provider=self.provider_type,
                status_code=response.status_code,
                retryable=response.status_code >= 500,
            )

        try:
            data = response.json()
            message = data.get("message", {})
        except ValueError as exc:
            raise LLMProviderError("Ollama returned an unexpected response.", provider=self.provider_type) from exc

        tool_calls: list[ToolCall] = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function", {})
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            tool_calls.append(
                ToolCall(
                    id=call.get("id") or f"ollama_{index}",
                    name=function.get("name", ""),
                    arguments=parse_tool_arguments(arguments),
                )
            )

        usage = {
            "prompt_tokens": data.get("prompt_eval_count"),
            "completion_tokens": data.get("eval_count"),
        }
        return LLMResponse(
            content=(message.get("content") or "").strip(),
            tool_calls=tool_calls,
            provider=self.provider_type,
            model=data.get("model", self.config.model),
            usage={key: value for key, value in usage.items() if value is not None},
            raw=data,
        )
