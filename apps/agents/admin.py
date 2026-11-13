"""Admin for providers, agent settings and conversation logs."""

from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from apps.providers.registry import test_provider

from .models import AgentSettings, AIProvider, Conversation, Message


class AIProviderAdminForm(forms.ModelForm):
    api_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text=_("Encrypted in the database. Leave empty to keep the current key."),
    )

    class Meta:
        model = AIProvider
        fields = (
            "name",
            "slug",
            "provider_type",
            "model",
            "base_url",
            "api_key",
            "is_active",
            "is_default",
            "temperature",
            "max_tokens",
            "timeout_seconds",
            "extra",
            "notes",
        )

    def clean_api_key(self):
        value = (self.cleaned_data.get("api_key") or "").strip()
        if not value and self.instance.pk:
            return self.instance.api_key
        return value


@admin.register(AIProvider)
class AIProviderAdmin(admin.ModelAdmin):
    form = AIProviderAdminForm
    list_display = (
        "name",
        "provider_type",
        "model",
        "is_active",
        "is_default",
        "ready",
        "masked_key",
        "updated_at",
    )
    list_filter = ("provider_type", "is_active", "is_default")
    search_fields = ("name", "slug", "model", "base_url")
    readonly_fields = ("slug", "created_at", "updated_at", "masked_key")
    actions = ("test_connection", "make_default", "enable", "disable")
    fieldsets = (
        (None, {"fields": ("name", "slug", "provider_type", "is_active", "is_default")}),
        (_("Model"), {"fields": ("model", "base_url", "api_key", "masked_key")}),
        (_("Generation"), {"fields": ("temperature", "max_tokens", "timeout_seconds", "extra")}),
        (_("Notes"), {"fields": ("notes", "created_at", "updated_at")}),
    )

    @admin.display(description=_("Ready"), boolean=True)
    def ready(self, obj: AIProvider) -> bool:
        return obj.is_ready

    @admin.display(description=_("API key"))
    def masked_key(self, obj: AIProvider) -> str:
        return obj.masked_api_key

    @admin.action(description=_("Test connection"))
    def test_connection(self, request, queryset):
        for provider in queryset:
            health = test_provider(provider)
            if health.ok:
                suffix = f" ({health.latency_ms} ms)" if health.latency_ms is not None else ""
                self.message_user(
                    request, f"{provider.name}: {health.message}{suffix}", messages.SUCCESS
                )
            else:
                self.message_user(request, f"{provider.name}: {health.message}", messages.ERROR)

    @admin.action(description=_("Make default"))
    def make_default(self, request, queryset):
        provider = queryset.first()
        if not provider:
            return
        provider.is_default = True
        provider.is_active = True
        provider.save(update_fields=["is_default", "is_active", "updated_at"])
        self.message_user(
            request,
            _("%(name)s is now the default provider.") % {"name": provider.name},
            messages.SUCCESS,
        )

    @admin.action(description=_("Enable selected providers"))
    def enable(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, _("%(count)s provider(s) enabled.") % {"count": updated}, messages.SUCCESS)

    @admin.action(description=_("Disable selected providers"))
    def disable(self, request, queryset):
        updated = queryset.update(is_active=False, is_default=False)
        self.message_user(request, _("%(count)s provider(s) disabled.") % {"count": updated}, messages.SUCCESS)


@admin.register(AgentSettings)
class AgentSettingsAdmin(admin.ModelAdmin):
    list_display = ("name", "is_enabled", "chat_enabled", "voice_enabled", "default_provider", "updated_at")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("name", "is_enabled", "chat_enabled", "voice_enabled", "default_provider")}),
        (
            _("English prompts"),
            {"fields": ("system_prompt_en", "greeting_en", "fallback_message_en"), "classes": ("collapse",)},
        ),
        (
            _("Polish prompts"),
            {"fields": ("system_prompt_pl", "greeting_pl", "fallback_message_pl"), "classes": ("collapse",)},
        ),
        (
            _("Behaviour"),
            {"fields": ("enabled_languages", "max_tool_iterations", "booking_hold_minutes")},
        ),
        (
            _("Voice"),
            {
                "fields": (
                    "voice_stt_provider",
                    "voice_tts_provider",
                    "voice_name_en",
                    "voice_name_pl",
                )
            },
        ),
        (_("Metadata"), {"fields": ("created_at", "updated_at")}),
    )

    def has_add_permission(self, request) -> bool:
        return not AgentSettings.objects.exists()

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def changelist_view(self, request, extra_context=None):
        settings_obj = AgentSettings.objects.first()
        if settings_obj is None:
            return super().changelist_view(request, extra_context)
        from django.shortcuts import redirect
        from django.urls import reverse

        return redirect(reverse("admin:agents_agentsettings_change", args=[settings_obj.pk]))


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    can_delete = False
    fields = ("created_at", "role", "content", "name", "tool_calls", "latency_ms")
    readonly_fields = fields
    ordering = ("created_at", "pk")

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("session_key", "channel", "language", "client", "booking", "message_count", "updated_at")
    list_filter = ("channel", "language", "is_active")
    search_fields = ("session_key", "client__email", "client__last_name", "booking__reference")
    readonly_fields = ("session_key", "state", "created_at", "updated_at")
    inlines = [MessageInline]
    date_hierarchy = "created_at"

    @admin.display(description=_("Messages"))
    def message_count(self, obj: Conversation) -> int:
        return obj.messages.count()

    def has_add_permission(self, request) -> bool:
        return False


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("created_at", "conversation", "role", "short_content", "name", "latency_ms")
    list_filter = ("role", "name")
    search_fields = ("content", "conversation__session_key")
    readonly_fields = (
        "conversation",
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "name",
        "latency_ms",
        "token_usage",
        "created_at",
    )

    @admin.display(description=_("Content"))
    def short_content(self, obj: Message) -> str:
        return format_html("{}", (obj.content or "")[:80])

    def has_add_permission(self, request) -> bool:
        return False
