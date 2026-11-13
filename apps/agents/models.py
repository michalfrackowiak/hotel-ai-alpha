"""Configuration models for AI providers, agent behaviour and conversations."""

from __future__ import annotations

import uuid

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.hotel.models import Booking, Client, TimeStampedModel

from .fields import EncryptedCharField


def new_session_key() -> str:
    return uuid.uuid4().hex


class AIProvider(TimeStampedModel):
    class Type(models.TextChoices):
        OPENAI = "openai", _("OpenAI")
        ANTHROPIC = "anthropic", _("Anthropic")
        OLLAMA = "ollama", _("Ollama / self-hosted")
        DEMO = "demo", _("Demo policy (offline)")

    name = models.CharField(_("name"), max_length=120)
    slug = models.SlugField(_("slug"), max_length=120, unique=True, blank=True)
    provider_type = models.CharField(_("provider type"), max_length=20, choices=Type.choices)
    model = models.CharField(_("model"), max_length=120)
    base_url = models.URLField(_("API base URL"), blank=True)
    api_key = EncryptedCharField(_("API key"), max_length=1024, blank=True)
    is_active = models.BooleanField(_("active"), default=True)
    is_default = models.BooleanField(_("default provider"), default=False)
    temperature = models.FloatField(
        _("temperature"), default=0.3, validators=[MinValueValidator(0.0), MaxValueValidator(2.0)]
    )
    max_tokens = models.PositiveIntegerField(
        _("max output tokens"), default=800, validators=[MinValueValidator(64), MaxValueValidator(8192)]
    )
    timeout_seconds = models.PositiveSmallIntegerField(
        _("timeout (seconds)"), default=45, validators=[MinValueValidator(5), MaxValueValidator(300)]
    )
    extra = models.JSONField(_("extra settings"), default=dict, blank=True)
    notes = models.TextField(_("notes"), blank=True)

    class Meta:
        ordering = ["-is_default", "name"]
        verbose_name = _("AI provider")
        verbose_name_plural = _("AI providers")

    def __str__(self) -> str:
        return f"{self.name} ({self.get_provider_type_display()})"

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or self.provider_type
            slug = base
            counter = 2
            while AIProvider.objects.exclude(pk=self.pk).filter(slug=slug).exists():
                slug = f"{base}-{counter}"
                counter += 1
            self.slug = slug
        if self.is_default:
            AIProvider.objects.exclude(pk=self.pk).filter(is_default=True).update(is_default=False)
        super().save(*args, **kwargs)

    @property
    def masked_api_key(self) -> str:
        if not self.api_key:
            return "—"
        if len(self.api_key) <= 8:
            return "•" * len(self.api_key)
        return f"{self.api_key[:4]}…{self.api_key[-4:]}"

    @property
    def is_ready(self) -> bool:
        if self.provider_type == self.Type.DEMO:
            return True
        if self.provider_type == self.Type.OLLAMA:
            return bool(self.base_url)
        return bool(self.api_key and self.base_url and self.model)


class AgentSettings(TimeStampedModel):
    """Singleton with the receptionist's behaviour, prompts and voice options."""

    name = models.CharField(_("assistant name"), max_length=120, default="Aurora")
    is_enabled = models.BooleanField(_("enabled"), default=True)
    chat_enabled = models.BooleanField(_("chat enabled"), default=True)
    voice_enabled = models.BooleanField(_("voice enabled"), default=True)
    default_provider = models.ForeignKey(
        AIProvider,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_settings",
        verbose_name=_("default provider"),
    )
    system_prompt_en = models.TextField(_("system prompt (English)"))
    system_prompt_pl = models.TextField(_("system prompt (Polish)"))
    greeting_en = models.TextField(_("greeting (English)"))
    greeting_pl = models.TextField(_("greeting (Polish)"))
    fallback_message_en = models.TextField(_("fallback message (English)"))
    fallback_message_pl = models.TextField(_("fallback message (Polish)"))
    enabled_languages = models.JSONField(_("enabled languages"), default=list, blank=True)
    max_tool_iterations = models.PositiveSmallIntegerField(
        _("max tool iterations"), default=6, validators=[MinValueValidator(1), MaxValueValidator(20)]
    )
    booking_hold_minutes = models.PositiveSmallIntegerField(
        _("booking hold (minutes)"), default=30, validators=[MinValueValidator(5), MaxValueValidator(1440)]
    )
    voice_stt_provider = models.CharField(_("speech-to-text provider"), max_length=40, default="browser")
    voice_tts_provider = models.CharField(_("text-to-speech provider"), max_length=40, default="browser")
    voice_name_en = models.CharField(_("voice name (English)"), max_length=80, blank=True)
    voice_name_pl = models.CharField(_("voice name (Polish)"), max_length=80, blank=True)

    class Meta:
        verbose_name = _("agent settings")
        verbose_name_plural = _("agent settings")

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> AgentSettings:
        settings_obj, _created = cls.objects.get_or_create(pk=1, defaults=_bootstrap_defaults())
        return settings_obj

    def greeting_for(self, language: str, hotel_name: str) -> str:
        template = self.greeting_pl if language == "pl" else self.greeting_en
        return (template or "").format(hotel=hotel_name)

    def fallback_for(self, language: str) -> str:
        return self.fallback_message_pl if language == "pl" else self.fallback_message_en

    def prompt_for(self, language: str) -> str:
        return self.system_prompt_pl if language == "pl" else self.system_prompt_en


def _bootstrap_defaults() -> dict:
    from .defaults import build_default_agent_settings

    return build_default_agent_settings(None)


class Conversation(TimeStampedModel):
    class Channel(models.TextChoices):
        CHAT = "chat", _("Chat")
        VOICE = "voice", _("Voice")

    session_key = models.CharField(max_length=64, unique=True, default=new_session_key, editable=False)
    channel = models.CharField(_("channel"), max_length=10, choices=Channel.choices, default=Channel.CHAT)
    language = models.CharField(_("language"), max_length=2, default="en")
    client = models.ForeignKey(
        Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="conversations"
    )
    booking = models.ForeignKey(
        Booking, on_delete=models.SET_NULL, null=True, blank=True, related_name="conversations"
    )
    provider = models.ForeignKey(
        AIProvider, on_delete=models.SET_NULL, null=True, blank=True, related_name="conversations"
    )
    state = models.JSONField(_("state"), default=dict, blank=True)
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = _("conversation")
        verbose_name_plural = _("conversations")

    def __str__(self) -> str:
        return f"{self.get_channel_display()} {self.session_key[:8]} ({self.language})"

    @property
    def session_id(self) -> str:
        return str(self.session_key)


class Message(models.Model):
    class Role(models.TextChoices):
        SYSTEM = "system", _("System")
        USER = "user", _("Guest")
        ASSISTANT = "assistant", _("Assistant")
        TOOL = "tool", _("Tool")

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(_("role"), max_length=10, choices=Role.choices)
    content = models.TextField(_("content"), blank=True)
    tool_calls = models.JSONField(_("tool calls"), default=list, blank=True)
    tool_call_id = models.CharField(_("tool call id"), max_length=120, blank=True)
    name = models.CharField(_("tool name"), max_length=80, blank=True)
    latency_ms = models.PositiveIntegerField(_("latency (ms)"), null=True, blank=True)
    token_usage = models.JSONField(_("token usage"), default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        verbose_name = _("message")
        verbose_name_plural = _("messages")

    def __str__(self) -> str:
        preview = (self.content or "")[:40]
        return f"{self.role}: {preview}"
