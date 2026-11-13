"""OpenAI Chat Completions connector (also works with OpenAI-compatible APIs)."""

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


class OpenAIProvider(BaseLLMProvider):
    provider_type = "openai"
    default_base_url = "https://api.openai.com/v1"

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        if not config.api_key:
            raise LLMProviderError("OpenAI API key is not configured.", provider=self.provider_type)
        self.base_url = (config.base_url or self.default_base_url).rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    def _message_payload(self, message: ChatMessage) -> dict[str, Any]:
        if message.role == "assistant" and message.tool_calls:
            return {
                "role": "assistant",
                "content": message.content or None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in message.tool_calls
                ],
            }
        if message.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            }
        return {"role": message.role, "content": message.content}

    def chat(self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [self._message_payload(message) for message in messages],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": spec} for spec in tools]
            payload["tool_choice"] = "auto"

        url = f"{self.base_url}/chat/completions"
        try:
            with httpx.Client(timeout=self.config.timeout) as client:
                response = client.post(url, headers=self._headers(), json=payload)
        except httpx.TimeoutException as exc:
            raise LLMProviderError("The language model timed out.", provider=self.provider_type) from exc
        except httpx.RequestError as exc:
            raise LLMProviderError(
                "Could not reach the language model provider.", provider=self.provider_type, retryable=True
            ) from exc

        if response.status_code >= 400:
            detail = _error_detail(response)
            raise LLMProviderError(
                f"OpenAI returned an error ({response.status_code}): {detail}",
                provider=self.provider_type,
                status_code=response.status_code,
                retryable=response.status_code >= 500 or response.status_code == 429,
            )

        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice.get("message", {})
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMProviderError("OpenAI returned an unexpected response.", provider=self.provider_type) from exc

        tool_calls: list[ToolCall] = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function", {})
            tool_calls.append(
                ToolCall(
                    id=call.get("id") or f"openai_{index}",
                    name=function.get("name", ""),
                    arguments=parse_tool_arguments(function.get("arguments")),
                )
            )

        return LLMResponse(
            content=message.get("content") or "",
            tool_calls=tool_calls,
            provider=self.provider_type,
            model=data.get("model", self.config.model),
            usage=data.get("usage") or {},
            raw=data,
        )


def _error_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:300] or "no details"
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(error or data)[:300]
