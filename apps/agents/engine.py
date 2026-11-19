"""Conversation engine: prompt assembly, tool loop and persistence."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.hotel.booking_service import normalize_language
from apps.hotel.models import RoomType
from apps.providers import (
    ChatMessage,
    LLMProviderError,
    LLMResponse,
    ToolCall,
    resolve_provider,
)
from apps.providers.registry import demo_provider

from .models import AgentSettings, Conversation, Message
from .tools import TOOL_SPECS, ToolExecutor

logger = logging.getLogger(__name__)

EXPLICIT_EN = re.compile(
    r"\benglish\b|\bangl(?:ielsk\w*|ielsku)\b|\bprze(?:łącz|łącz).*angielsk\w*\b",
    re.IGNORECASE,
)
EXPLICIT_PL = re.compile(
    r"\bpolish\b|\bpolsk(?:i|u|im|iego)\b|\bpo polsku\b",
    re.IGNORECASE,
)
# The greeting offers "press 1 / press 2"; these only apply before the first reply.
NUMBER_EN = re.compile(r"^\s*2\s*$")
NUMBER_PL = re.compile(r"^\s*1\s*$")
POLISH_DIACRITICS = re.compile(r"[ąćęłńóśźż]", re.IGNORECASE)
POLISH_WORDS = re.compile(
    r"\b(tak|nie|proszę|dziękuję|dzień|dobry|pokój|pokoje|rezerwacj\w*|noc\w*|goś\w*|"
    r"jestem|nazywam|chcę|chciałbym|chciałabym|od|do|dla|na)\b",
    re.IGNORECASE,
)
ENGLISH_WORDS = re.compile(
    r"\b(yes|no|please|thanks|thank|hello|hi|room|rooms|booking|book|night|nights|guest|guests|"
    r"from|to|for|the|and|with|my|name|is|email|phone|check.?in|check.?out)\b",
    re.IGNORECASE,
)


def detect_language(text: str, current: str, allow_number_switch: bool = True) -> str:
    """Return the language the guest is using in ``text``.

    Explicit requests win. Otherwise Polish diacritics or Polish function words
    switch to Polish, and a clearly English sentence switches to English. The
    current language is kept when the signal is ambiguous, so names, email
    addresses and bare numbers never flip the conversation.
    """
    text = text or ""
    if EXPLICIT_EN.search(text):
        return "en"
    if EXPLICIT_PL.search(text):
        return "pl"
    if allow_number_switch:
        if NUMBER_EN.match(text):
            return "en"
        if NUMBER_PL.match(text):
            return "pl"
    if POLISH_DIACRITICS.search(text):
        return "pl"
    polish_hits = len(POLISH_WORDS.findall(text))
    english_hits = len(ENGLISH_WORDS.findall(text))
    if current == "pl" and english_hits >= 2 and polish_hits == 0:
        return "en"
    if current == "en" and polish_hits >= 2 and english_hits == 0:
        return "pl"
    return current


class AgentUnavailableError(Exception):
    pass


@dataclass
class AgentTurn:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    booking_reference: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "tool_calls": self.tool_calls,
            "provider": self.provider,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "booking_reference": self.booking_reference,
            "error": self.error,
        }


class AgentEngine:
    def __init__(
        self,
        conversation: Conversation,
        *,
        settings_obj: AgentSettings | None = None,
        provider=None,
        provider_record=None,
    ):
        self.conversation = conversation
        self.settings = settings_obj or AgentSettings.load()
        self.executor = ToolExecutor(conversation)
        self.provider = provider
        self.provider_record = provider_record

    # ------------------------------------------------------------------ setup
    def _ensure_provider(self) -> None:
        if self.provider is not None:
            return
        try:
            self.provider_record, self.provider = resolve_provider(self.settings)
        except LLMProviderError as exc:
            logger.warning("Default provider unusable (%s); using demo policy.", exc)
            self.provider = demo_provider()
            self.provider_record = None
        if self.conversation.provider_id != getattr(self.provider_record, "pk", None):
            self.conversation.provider = self.provider_record
            self.conversation.save(update_fields=["provider", "updated_at"])

    def _system_prompt(self) -> str:
        language = self.conversation.language
        hotel = settings.HOTEL
        base_prompt = self.settings.prompt_for(language) or ""
        room_types = []
        for room_type in RoomType.objects.filter(is_active=True).order_by("sort_order", "base_price"):
            room_types.append(
                f"{room_type.name} (slug: {room_type.slug}, sleeps {room_type.capacity}, "
                f"from {room_type.base_price} {room_type.currency}/night)"
            )
        context = (
            "\n\nRUNTIME CONTEXT\n"
            f"- Hotel: {hotel['name']}\n"
            f"- Today: {timezone.localdate().isoformat()}\n"
            f"- Currency: {hotel['currency']}\n"
            f"- Check-in from {hotel['check_in_time']}, check-out until {hotel['check_out_time']}\n"
            f"- A booking hold lasts {self.settings.booking_hold_minutes} minutes\n"
            f"- Channel: {self.conversation.channel}\n"
            f"- Room types: {'; '.join(room_types) or 'none configured'}\n"
            "- Use the tools for every availability, price and booking operation. "
            "Never invent availability or prices.\n"
            "- Ask for explicit confirmation before calling confirm_booking.\n"
            f"LANGUAGE: {language}\n"
        )
        if language == "pl":
            context += "Odpowiadaj wyłącznie po polsku.\n"
        else:
            context += "Respond only in English.\n"
        return f"{base_prompt.strip()}{context}"

    def _history(self) -> list[ChatMessage]:
        limit = settings.AI["history_limit"]
        recent = list(self.conversation.messages.order_by("-created_at", "-pk")[:limit])
        recent.reverse()
        messages: list[ChatMessage] = []
        for message in recent:
            if message.role == Message.Role.USER:
                messages.append(ChatMessage(role="user", content=message.content))
            elif message.role == Message.Role.ASSISTANT:
                tool_calls = [
                    ToolCall(
                        id=call.get("id", ""),
                        name=call.get("name", ""),
                        arguments=call.get("arguments") or {},
                    )
                    for call in (message.tool_calls or [])
                ]
                messages.append(
                    ChatMessage(role="assistant", content=message.content, tool_calls=tool_calls)
                )
            elif message.role == Message.Role.TOOL:
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=message.content,
                        tool_call_id=message.tool_call_id,
                        name=message.name,
                    )
                )
        return messages

    # ------------------------------------------------------------------- turn
    def _maybe_switch_language(self, text: str) -> None:
        has_prior_user = self.conversation.messages.filter(role=Message.Role.USER).exists()
        detected = detect_language(text, self.conversation.language, allow_number_switch=not has_prior_user)
        if detected != self.conversation.language:
            logger.info(
                "Conversation %s switched language %s → %s",
                self.conversation.pk,
                self.conversation.language,
                detected,
            )
            self.conversation.language = detected
            self.conversation.save(update_fields=["language", "updated_at"])

    def handle_user_message(self, text: str) -> AgentTurn:
        text = (text or "").strip()
        if not text:
            raise ValueError("The message is empty.")
        self._ensure_provider()
        self._maybe_switch_language(text)

        Message.objects.create(conversation=self.conversation, role=Message.Role.USER, content=text)
        messages = [ChatMessage(role="system", content=self._system_prompt()), *self._history()]

        tool_trace: list[dict[str, Any]] = []
        booking_reference = ""
        started = time.perf_counter()
        last_usage: dict[str, Any] = {}
        last_model = ""

        for _iteration in range(max(1, self.settings.max_tool_iterations)):
            call_started = time.perf_counter()
            try:
                response: LLMResponse = self.provider.chat(messages, TOOL_SPECS)
            except LLMProviderError as exc:
                logger.warning("Provider error during conversation %s: %s", self.conversation.pk, exc)
                content = self.settings.fallback_for(self.conversation.language)
                Message.objects.create(
                    conversation=self.conversation,
                    role=Message.Role.ASSISTANT,
                    content=content,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
                return self._finish(
                    AgentTurn(
                        content=content,
                        provider=getattr(self.provider, "provider_type", ""),
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        error=exc.message,
                    )
                )

            latency_ms = int((time.perf_counter() - call_started) * 1000)
            last_usage = response.usage or {}
            last_model = response.model or self.provider.config.model

            if not response.tool_calls:
                content = (response.content or "").strip()
                if not content:
                    content = self.settings.fallback_for(self.conversation.language)
                Message.objects.create(
                    conversation=self.conversation,
                    role=Message.Role.ASSISTANT,
                    content=content,
                    latency_ms=latency_ms,
                    token_usage=last_usage,
                )
                return self._finish(
                    AgentTurn(
                        content=content,
                        tool_calls=tool_trace,
                        provider=response.provider,
                        model=last_model,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        booking_reference=booking_reference,
                    )
                )

            Message.objects.create(
                conversation=self.conversation,
                role=Message.Role.ASSISTANT,
                content=response.content or "",
                tool_calls=[
                    {"id": call.id, "name": call.name, "arguments": call.arguments}
                    for call in response.tool_calls
                ],
                latency_ms=latency_ms,
                token_usage=last_usage,
            )
            messages.append(
                ChatMessage(role="assistant", content=response.content or "", tool_calls=response.tool_calls)
            )

            for call in response.tool_calls:
                result = self.executor.execute(call.name, call.arguments)
                serialized = json.dumps(result, ensure_ascii=False, default=str)
                Message.objects.create(
                    conversation=self.conversation,
                    role=Message.Role.TOOL,
                    content=serialized,
                    tool_call_id=call.id,
                    name=call.name,
                )
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=serialized,
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )
                tool_trace.append(
                    {
                        "name": call.name,
                        "arguments": call.arguments,
                        "ok": bool(result.get("ok")),
                    }
                )
                if result.get("ok") and isinstance(result.get("booking"), dict):
                    booking_reference = result["booking"].get("reference") or booking_reference

        content = self.settings.fallback_for(self.conversation.language)
        Message.objects.create(
            conversation=self.conversation,
            role=Message.Role.ASSISTANT,
            content=content,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return self._finish(
            AgentTurn(
                content=content,
                tool_calls=tool_trace,
                provider=getattr(self.provider, "provider_type", ""),
                model=last_model,
                latency_ms=int((time.perf_counter() - started) * 1000),
                booking_reference=booking_reference,
                error="tool_iteration_limit",
            )
        )

    def _finish(self, turn: AgentTurn) -> AgentTurn:
        self.conversation.save(update_fields=["updated_at"])
        return turn


def start_conversation(
    *,
    channel: str = Conversation.Channel.CHAT,
    language: str | None = None,
) -> Conversation:
    settings_obj = AgentSettings.load()
    if not settings_obj.is_enabled:
        raise AgentUnavailableError("The AI receptionist is currently disabled.")
    if channel == Conversation.Channel.CHAT and not settings_obj.chat_enabled:
        raise AgentUnavailableError("The chat assistant is currently disabled.")
    if channel == Conversation.Channel.VOICE and not settings_obj.voice_enabled:
        raise AgentUnavailableError("The voice assistant is currently disabled.")

    language = normalize_language(language)
    conversation = Conversation.objects.create(channel=channel, language=language)
    greeting = settings_obj.greeting_for(language, settings.HOTEL["name"])
    Message.objects.create(
        conversation=conversation,
        role=Message.Role.ASSISTANT,
        content=greeting,
    )
    return conversation


def conversation_history(conversation: Conversation) -> list[dict[str, Any]]:
    return [
        {
            "role": message.role,
            "content": message.content,
            "created_at": message.created_at.isoformat(),
            "tool_calls": message.tool_calls,
        }
        for message in conversation.messages.order_by("created_at", "pk")
        if message.role in (Message.Role.USER, Message.Role.ASSISTANT)
    ]
