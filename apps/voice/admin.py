"""Admin for phone calls."""

from __future__ import annotations

from django.contrib import admin

from .models import PhoneCall


@admin.register(PhoneCall)
class PhoneCallAdmin(admin.ModelAdmin):
    list_display = (
        "external_id",
        "direction",
        "status",
        "from_number",
        "to_number",
        "conversation",
        "started_at",
        "answered_at",
        "ended_at",
        "duration_seconds",
        "created_at",
    )
    list_filter = ("provider", "direction", "status")
    search_fields = ("external_id", "from_number", "to_number", "conversation__session_key")
    readonly_fields = (
        "conversation",
        "provider",
        "external_id",
        "direction",
        "from_number",
        "to_number",
        "status",
        "started_at",
        "answered_at",
        "ended_at",
        "duration_seconds",
        "recording_url",
        "transcript",
        "summary",
        "metadata",
        "created_at",
        "updated_at",
    )
    fields = readonly_fields
    date_hierarchy = "created_at"

    def has_add_permission(self, request) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
