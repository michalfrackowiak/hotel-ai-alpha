"""Reception admin for rooms, clients, pricing and bookings."""

from __future__ import annotations

from django.contrib import admin, messages
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from .booking_service import BookingError, cancel_booking, confirm_booking
from .models import Booking, Client, PricingRule, Room, RoomType


class RoomInline(admin.TabularInline):
    model = Room
    extra = 1
    fields = ("number", "name", "floor", "is_active")


@admin.register(RoomType)
class RoomTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "capacity", "base_price", "currency", "room_count", "is_active", "sort_order")
    list_editable = ("is_active", "sort_order")
    list_filter = ("is_active", "currency")
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [RoomInline]
    fieldsets = (
        (None, {"fields": ("name", "slug", "is_active", "sort_order")}),
        (_("Presentation"), {"fields": ("short_description", "description", "amenities")}),
        (_("Capacity & price"), {"fields": ("capacity", "base_price", "currency", "size_sqm", "bed_configuration")}),
    )

    @admin.display(description=_("Rooms"))
    def room_count(self, obj: RoomType) -> int:
        return obj.rooms.count()


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ("number", "name", "room_type", "floor", "is_active", "calendar")
    list_filter = ("room_type", "is_active", "floor")
    search_fields = ("number", "name", "room_type__name")
    autocomplete_fields = ("room_type",)

    @admin.display(description=_("Calendar"))
    def calendar(self, obj: Room) -> str:
        mapping = getattr(obj, "calendar_mapping", None)
        if mapping and mapping.is_active:
            return format_html('<span style="color:#0a7">●</span> {}', mapping.calendar_id)
        return format_html('<span style="color:#999">—</span>')


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "phone", "language", "country", "created_at")
    list_filter = ("language", "country", "marketing_consent")
    search_fields = ("first_name", "last_name", "email", "phone")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("first_name", "last_name", "language", "country")}),
        (_("Contact"), {"fields": ("email", "phone")}),
        (_("Preferences"), {"fields": ("marketing_consent", "notes")}),
        (_("Metadata"), {"fields": ("created_at", "updated_at")}),
    )


@admin.register(PricingRule)
class PricingRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "room_type", "adjustment", "priority", "window", "is_active")
    list_filter = ("kind", "adjustment_type", "is_active", "room_type")
    search_fields = ("name",)
    fieldsets = (
        (None, {"fields": ("name", "kind", "room_type", "is_active", "priority")}),
        (_("Validity"), {"fields": ("start_date", "end_date", "days_of_week", "min_nights")}),
        (_("Adjustment"), {"fields": ("adjustment_type", "amount")}),
    )

    @admin.display(description=_("Adjustment"))
    def adjustment(self, obj: PricingRule) -> str:
        suffix = "%" if obj.adjustment_type == PricingRule.AdjustmentType.PERCENT else obj.room_type.currency if obj.room_type else "PLN"
        sign = "+" if obj.amount >= 0 else ""
        return f"{sign}{obj.amount}{suffix}"

    @admin.display(description=_("Window"))
    def window(self, obj: PricingRule) -> str:
        if obj.start_date or obj.end_date:
            return f"{obj.start_date or '…'} → {obj.end_date or '…'}"
        if obj.days_of_week:
            return _("weekdays %(days)s") % {"days": obj.days_of_week}
        return _("always")


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "client",
        "room",
        "stay",
        "status",
        "source",
        "total_price",
        "hold_state",
        "created_at",
    )
    list_filter = ("status", "source", "language", "room__room_type", "check_in")
    search_fields = ("reference", "client__first_name", "client__last_name", "client__email", "client__phone")
    date_hierarchy = "check_in"
    autocomplete_fields = ("client", "room")
    readonly_fields = (
        "reference",
        "price_breakdown",
        "external_event_id",
        "created_at",
        "updated_at",
        "confirmed_at",
        "cancelled_at",
    )
    actions = ("confirm_selected", "cancel_selected", "sync_calendar")
    fieldsets = (
        (None, {"fields": ("reference", "status", "source", "language")}),
        (_("Guest"), {"fields": ("client",)}),
        (_("Stay"), {"fields": ("room", "check_in", "check_out", "adults", "children")}),
        (_("Price"), {"fields": ("currency", "total_price", "price_breakdown")}),
        (_("Extras"), {"fields": ("special_requests", "hold_expires_at", "external_event_id")}),
        (_("Metadata"), {"fields": ("confirmed_at", "cancelled_at", "created_at", "updated_at")}),
    )

    @admin.display(description=_("Stay"))
    def stay(self, obj: Booking) -> str:
        return f"{obj.check_in:%d %b} → {obj.check_out:%d %b} · {obj.nights}n"

    @admin.display(description=_("Hold"))
    def hold_state(self, obj: Booking) -> str:
        if obj.status != Booking.Status.PENDING:
            return "—"
        if obj.is_hold_expired:
            return _("expired")
        if obj.hold_expires_at:
            return obj.hold_expires_at.strftime("%H:%M")
        return "—"

    @admin.action(description=_("Confirm selected bookings"))
    def confirm_selected(self, request, queryset):
        confirmed = 0
        for booking in queryset:
            try:
                confirm_booking(booking.reference)
                confirmed += 1
            except BookingError as exc:
                self.message_user(request, f"{booking.reference}: {exc.message}", messages.ERROR)
        if confirmed:
            self.message_user(request, _("%(count)s booking(s) confirmed.") % {"count": confirmed}, messages.SUCCESS)

    @admin.action(description=_("Cancel selected bookings"))
    def cancel_selected(self, request, queryset):
        cancelled = 0
        for booking in queryset:
            try:
                cancel_booking(booking.reference)
                cancelled += 1
            except BookingError as exc:
                self.message_user(request, f"{booking.reference}: {exc.message}", messages.ERROR)
        if cancelled:
            self.message_user(request, _("%(count)s booking(s) cancelled.") % {"count": cancelled}, messages.SUCCESS)

    @admin.action(description=_("Sync selected bookings to calendars"))
    def sync_calendar(self, request, queryset):
        from apps.calendar_sync.services import sync_booking

        synced = 0
        for booking in queryset:
            try:
                sync_booking(booking)
                synced += 1
            except Exception as exc:  # noqa: BLE001
                self.message_user(request, f"{booking.reference}: {exc}", messages.ERROR)
        if synced:
            self.message_user(request, _("%(count)s booking(s) synced.") % {"count": synced}, messages.SUCCESS)
