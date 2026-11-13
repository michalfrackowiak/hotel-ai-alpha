"""Calendar integration admin."""

from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _

from .models import BookingCalendarEvent, CalendarConnection, RoomCalendar
from .providers import CalendarError, get_calendar_provider
from .services import sync_booking


class CalendarConnectionForm(forms.ModelForm):
    class Meta:
        model = CalendarConnection
        fields = (
            "name",
            "provider",
            "calendar_id",
            "credentials_json",
            "sync_enabled",
            "is_active",
            "notes",
        )
        widgets = {"credentials_json": forms.Textarea(attrs={"rows": 8, "class": "vLargeTextField"})}


@admin.register(CalendarConnection)
class CalendarConnectionAdmin(admin.ModelAdmin):
    form = CalendarConnectionForm
    list_display = ("name", "provider", "calendar_id", "room_count", "sync_enabled", "is_active", "credentials")
    list_filter = ("provider", "sync_enabled", "is_active")
    search_fields = ("name", "calendar_id")
    actions = ("test_connection",)
    fieldsets = (
        (None, {"fields": ("name", "provider", "is_active", "sync_enabled")}),
        (_("Calendars"), {"fields": ("calendar_id", "notes")}),
        (
            _("Credentials"),
            {
                "fields": ("credentials_json",),
                "description": _(
                    "Service account key JSON or authorised-user token JSON. "
                    "Alternatively set GOOGLE_CALENDAR_CREDENTIALS_FILE in the environment."
                ),
            },
        ),
    )

    @admin.display(description=_("Rooms"))
    def room_count(self, obj: CalendarConnection) -> int:
        return obj.room_mappings.count()

    @admin.display(description=_("Credentials"), boolean=True)
    def credentials(self, obj: CalendarConnection) -> bool:
        return obj.has_credentials

    @admin.action(description=_("Test calendar connection"))
    def test_connection(self, request, queryset):
        for connection in queryset:
            try:
                ok, detail = get_calendar_provider(connection).health_check()
            except CalendarError as exc:
                ok, detail = False, str(exc)
            if ok:
                self.message_user(request, f"{connection.name}: {detail}", messages.SUCCESS)
            else:
                self.message_user(request, f"{connection.name}: {detail}", messages.ERROR)


@admin.register(RoomCalendar)
class RoomCalendarAdmin(admin.ModelAdmin):
    list_display = ("room", "connection", "calendar_id", "is_active", "updated_at")
    list_filter = ("connection", "is_active")
    search_fields = ("room__number", "room__name", "calendar_id")
    autocomplete_fields = ("room", "connection")
    fieldsets = (
        (None, {"fields": ("room", "connection", "is_active")}),
        (_("Calendar"), {"fields": ("calendar_id",)}),
    )


@admin.register(BookingCalendarEvent)
class BookingCalendarEventAdmin(admin.ModelAdmin):
    list_display = ("booking", "status", "event_id", "last_synced_at", "created_at")
    list_filter = ("status", "mapping__connection")
    search_fields = ("booking__reference", "event_id")
    readonly_fields = (
        "booking",
        "mapping",
        "event_id",
        "status",
        "last_synced_at",
        "error_message",
        "payload",
        "created_at",
        "updated_at",
    )
    actions = ("retry",)

    @admin.action(description=_("Retry calendar sync"))
    def retry(self, request, queryset):
        count = 0
        for record in queryset.select_related("booking"):
            sync_booking(record.booking)
            count += 1
        self.message_user(request, _("%(count)s event(s) retried.") % {"count": count}, messages.SUCCESS)

    def has_add_permission(self, request) -> bool:
        return False
