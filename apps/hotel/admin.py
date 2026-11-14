"""Reception admin for rooms, clients, pricing and bookings."""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from .booking_service import BookingError, cancel_booking, confirm_booking
from .models import Booking, BookingEvent, Client, DailyRate, PricingRule, RatePlan, Room, RoomType


class RoomInline(admin.TabularInline):
    model = Room
    extra = 1
    fields = ("number", "name", "floor", "is_active", "housekeeping_status")


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
    list_display = ("number", "name", "room_type", "floor", "is_active", "housekeeping_status", "calendar")
    list_filter = ("room_type", "is_active", "floor", "housekeeping_status")
    list_editable = ("housekeeping_status", "is_active")
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


class BookingEventInline(admin.TabularInline):
    model = BookingEvent
    extra = 0
    can_delete = False
    fields = ("created_at", "event_type", "actor", "channel", "summary")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


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
        "cancellation_fee",
        "hold_state",
        "confirmation_email_status",
        "created_at",
    )
    list_filter = ("status", "source", "language", "cancelled_by", "room__room_type", "check_in")
    search_fields = ("reference", "client__first_name", "client__last_name", "client__email", "client__phone")
    date_hierarchy = "check_in"
    autocomplete_fields = ("client", "room")
    inlines = [BookingEventInline]
    readonly_fields = (
        "reference",
        "price_breakdown",
        "external_event_id",
        "created_at",
        "updated_at",
        "confirmed_at",
        "cancelled_at",
        "cancellation_fee",
    )
    actions = ("confirm_selected", "cancel_selected", "sync_calendar", "resend_confirmation")
    fieldsets = (
        (None, {"fields": ("reference", "status", "source", "language")}),
        (_("Guest"), {"fields": ("client",)}),
        (_("Stay"), {"fields": ("room", "check_in", "check_out", "adults", "children")}),
        (_("Price"), {"fields": ("currency", "total_price", "price_breakdown")}),
        (_("Extras"), {"fields": ("special_requests", "hold_expires_at", "external_event_id")}),
        (
            _("Cancellation"),
            {"fields": ("cancelled_at", "cancelled_by", "cancellation_reason", "cancellation_note", "cancellation_fee")},
        ),
        (_("Metadata"), {"fields": ("confirmed_at", "created_at", "updated_at")}),
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
                confirm_booking(booking.reference, actor="staff", channel="admin")
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
                cancel_booking(
                    booking.reference,
                    reason=Booking.CancellationReason.GUEST_REQUEST,
                    note=_("Cancelled from the reception admin."),
                    actor="staff",
                    channel="admin",
                )
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

    @admin.action(description=_("Resend confirmation email"))
    def resend_confirmation(self, request, queryset):
        from apps.hotel.notifications import send_booking_confirmation

        sent = 0
        for booking in queryset:
            try:
                if send_booking_confirmation(booking, force=True):
                    sent += 1
                else:
                    self.message_user(
                        request,
                        _("%(reference)s: no confirmation email was sent.")
                        % {"reference": booking.reference},
                        messages.WARNING,
                    )
            except Exception as exc:  # noqa: BLE001
                self.message_user(request, f"{booking.reference}: {exc}", messages.ERROR)
        if sent:
            self.message_user(
                request,
                _("%(count)s confirmation email(s) sent.") % {"count": sent},
                messages.SUCCESS,
            )


@admin.register(BookingEvent)
class BookingEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "booking", "event_type", "actor", "channel", "summary")
    list_filter = ("event_type", "actor", "channel")
    search_fields = ("booking__reference", "summary")
    date_hierarchy = "created_at"
    readonly_fields = ("booking", "event_type", "actor", "channel", "summary", "data", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RatePlan)
class RatePlanAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "room_type", "kind", "adjustment", "is_refundable", "is_active", "sort_order")
    list_filter = ("is_active", "is_refundable", "kind", "room_type")
    list_editable = ("is_active", "sort_order")
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("sort_order", "name")
    fields = ("name", "slug", "room_type", "description", "kind", "adjustment", "is_refundable", "is_active", "sort_order")


@admin.register(DailyRate)
class DailyRateAdmin(admin.ModelAdmin):
    list_display = ("room_type", "date", "rate", "min_nights", "max_nights", "closed", "closed_to_arrival")
    fields = ("room_type", "date", "rate", "min_nights", "max_nights", "closed", "closed_to_arrival")
    list_filter = ("room_type",)
    date_hierarchy = "date"
    search_fields = ("room_type__name",)
    ordering = ("-date", "room_type")
    change_list_template = "admin/hotel/dailyrate/change_list.html"

    def get_urls(self):
        custom = [
            path(
                "calendar/",
                self.admin_site.admin_view(self.rate_calendar_view),
                name="hotel_dailyrate_calendar",
            ),
        ]
        return custom + super().get_urls()

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["rate_calendar_url"] = reverse("admin:hotel_dailyrate_calendar")
        return super().changelist_view(request, extra_context=extra_context)

    def rate_calendar_view(self, request):
        today = date.today()
        try:
            year = int(request.GET.get("year", today.year))
            month = int(request.GET.get("month", today.month))
            if not 1 <= month <= 12:
                raise ValueError
        except (TypeError, ValueError):
            year, month = today.year, today.month

        if request.method == "POST":
            try:
                year = int(request.POST.get("year", year))
                month = int(request.POST.get("month", month))
                if not 1 <= month <= 12:
                    raise ValueError
            except (TypeError, ValueError):
                year, month = today.year, today.month
            self._save_rate_grid(request, year, month)
            return HttpResponseRedirect(f"{request.path}?year={year}&month={month}")

        days_in_month = calendar.monthrange(year, month)[1]
        days = [date(year, month, day) for day in range(1, days_in_month + 1)]
        room_types = list(RoomType.objects.filter(is_active=True).order_by("sort_order", "name"))
        existing = {
            (row.room_type_id, row.date): row
            for row in DailyRate.objects.filter(date__in=days)
        }
        rows = []
        for room_type in room_types:
            cells = []
            for day in days:
                record = existing.get((room_type.pk, day))
                cells.append(
                    {
                        "iso": day.isoformat(),
                        "rate": record.rate if record else None,
                        "min_nights": record.min_nights if record else None,
                    }
                )
            rows.append({"room_type": room_type, "cells": cells})

        context = {
            **self.admin_site.each_context(request),
            "title": _("Rate calendar"),
            "opts": self.model._meta,
            "year": year,
            "month": month,
            "month_label": date(year, month, 1).strftime("%B %Y"),
            "prev_year": year - 1 if month == 1 else year,
            "prev_month": 12 if month == 1 else month - 1,
            "next_year": year + 1 if month == 12 else year,
            "next_month": 1 if month == 12 else month + 1,
            "days": days,
            "rows": rows,
        }
        return render(request, "admin/hotel/dailyrate/rate_calendar.html", context)

    def _save_rate_grid(self, request, year: int, month: int) -> None:
        days_in_month = calendar.monthrange(year, month)[1]
        dates = {date(year, month, day).isoformat() for day in range(1, days_in_month + 1)}
        room_types = {str(room_type.pk): room_type for room_type in RoomType.objects.filter(is_active=True)}
        saved = 0
        for room_type_id, room_type in room_types.items():
            for iso in dates:
                rate_raw = (request.POST.get(f"rate_{room_type_id}_{iso}") or "").strip()
                min_raw = (request.POST.get(f"min_{room_type_id}_{iso}") or "").strip()
                existing = DailyRate.objects.filter(room_type=room_type, date=iso).first()

                if not rate_raw:
                    if existing and min_raw:
                        try:
                            existing.min_nights = max(int(min_raw), 0)
                        except ValueError:
                            continue
                        existing.save(update_fields=["min_nights", "updated_at"])
                        saved += 1
                    elif existing:
                        existing.delete()
                    continue

                try:
                    rate = Decimal(rate_raw)
                except (InvalidOperation, ValueError):
                    self.message_user(
                        request,
                        _("Ignored an invalid rate for %(room)s on %(date)s.")
                        % {"room": room_type.name, "date": iso},
                        messages.ERROR,
                    )
                    continue
                try:
                    min_nights = max(int(min_raw or 0), 0)
                except ValueError:
                    min_nights = 0
                DailyRate.objects.update_or_create(
                    room_type=room_type,
                    date=iso,
                    defaults={"rate": rate, "min_nights": min_nights},
                )
                saved += 1

        if saved:
            self.message_user(
                request,
                _("%(count)s daily rate(s) saved.") % {"count": saved},
                messages.SUCCESS,
            )
