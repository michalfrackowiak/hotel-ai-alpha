"""Anthropic Messages API connector."""

from __future__ import annotations

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
)


class AnthropicProvider(BaseLLMProvider):
    provider_type = "anthropic"
    default_base_url = "https://api.anthropic.com"
    api_version = "2023-06-01"

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        if not config.api_key:
            raise LLMProviderError("Anthropic API key is not configured.", provider=self.provider_type)
        base = (config.base_url or self.default_base_url).rstrip("/")
        self.base_url = base if base.endswith("/v1") else f"{base}/v1"

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.config.api_key,
            "anthropic-version": self.api_version,
            "content-type": "application/json",
        }

    def _convert_messages(self, messages: list[ChatMessage]) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []

        for message in messages:
            if message.role == "system":
                if message.content:
                    system_parts.append(message.content)
                continue

            if message.role == "tool":
                converted.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.tool_call_id,
                                "content": message.content or "",
                            }
                        ],
                    }
                )
                continue

            if message.role == "assistant":
                blocks: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                for call in message.tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.name,
                            "input": call.arguments,
                        }
                    )
                if blocks:
                    converted.append({"role": "assistant", "content": blocks})
                continue

            if message.content:
                converted.append({"role": "user", "content": [{"type": "text", "text": message.content}]})

        return "\n\n".join(system_parts), converted

    def chat(self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None) -> LLMResponse:
        system, converted = self._convert_messages(messages)
        payload: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "messages": converted,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {
                    "name": spec["name"],
                    "description": spec.get("description", ""),
                    "input_schema": spec.get("parameters", {"type": "object", "properties": {}}),
                }
                for spec in tools
            ]

        try:
            with httpx.Client(timeout=self.config.timeout) as client:
                response = client.post(f"{self.base_url}/messages", headers=self._headers(), json=payload)
        except httpx.TimeoutException as exc:
            raise LLMProviderError("The language model timed out.", provider=self.provider_type) from exc
        except httpx.RequestError as exc:
            raise LLMProviderError(
                "Could not reach the language model provider.", provider=self.provider_type, retryable=True
            ) from exc

        if response.status_code >= 400:
            detail = _error_detail(response)
            raise LLMProviderError(
                f"Anthropic returned an error ({response.status_code}): {detail}",
                provider=self.provider_type,
                status_code=response.status_code,
                retryable=response.status_code >= 500 or response.status_code == 429,
            )

        try:
            data = response.json()
            blocks = data.get("content", [])
        except ValueError as exc:
            raise LLMProviderError("Anthropic returned an unexpected response.", provider=self.provider_type) from exc

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in blocks:
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(block.get("text", ""))
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        arguments=block.get("input") or {},
                    )
                )

        return LLMResponse(
            content="".join(text_parts).strip(),
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
