"""Google Calendar integration models: one calendar per room."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.hotel.models import Booking, Room, TimeStampedModel


class CalendarConnection(TimeStampedModel):
    class Provider(models.TextChoices):
        GOOGLE = "google", _("Google Calendar")
        FAKE = "fake", _("Fake (demo/testing)")

    name = models.CharField(_("name"), max_length=120)
    provider = models.CharField(_("provider"), max_length=20, choices=Provider.choices, default=Provider.GOOGLE)
    calendar_id = models.CharField(
        _("default calendar id"),
        max_length=255,
        blank=True,
        help_text=_("Fallback calendar id when a room mapping does not specify one."),
    )
    credentials_json = models.TextField(
        _("credentials JSON"),
        blank=True,
        help_text=_(
            "Service account key JSON or OAuth authorised-user JSON. "
            "The file path from GOOGLE_CALENDAR_CREDENTIALS_FILE is used when empty."
        ),
    )
    sync_enabled = models.BooleanField(_("sync enabled"), default=True)
    is_active = models.BooleanField(_("active"), default=True)
    notes = models.TextField(_("notes"), blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = _("calendar connection")
        verbose_name_plural = _("calendar connections")

    def __str__(self) -> str:
        return f"{self.name} ({self.get_provider_display()})"

    @property
    def has_credentials(self) -> bool:
        import os

        return bool(self.credentials_json.strip() or os.getenv("GOOGLE_CALENDAR_CREDENTIALS_FILE", "").strip())


class RoomCalendar(TimeStampedModel):
    room = models.OneToOneField(Room, on_delete=models.CASCADE, related_name="calendar_mapping")
    connection = models.ForeignKey(
        CalendarConnection, on_delete=models.CASCADE, related_name="room_mappings"
    )
    calendar_id = models.CharField(
        _("calendar id"),
        max_length=255,
        blank=True,
        help_text=_("The Google Calendar id dedicated to this room."),
    )
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        ordering = ["room__number"]
        verbose_name = _("room calendar")
        verbose_name_plural = _("room calendars")

    def __str__(self) -> str:
        return f"{self.room.number} → {self.calendar_id or self.connection.calendar_id}"

    def save(self, *args, **kwargs):
        if not self.calendar_id:
            self.calendar_id = self.connection.calendar_id
        super().save(*args, **kwargs)


class BookingCalendarEvent(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        SYNCED = "synced", _("Synced")
        FAILED = "failed", _("Failed")
        DELETED = "deleted", _("Deleted")

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="calendar_events")
    mapping = models.ForeignKey(
        RoomCalendar, on_delete=models.SET_NULL, null=True, blank=True, related_name="events"
    )
    event_id = models.CharField(_("event id"), max_length=255, blank=True)
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.PENDING)
    last_synced_at = models.DateTimeField(_("last synced at"), null=True, blank=True)
    error_message = models.TextField(_("error"), blank=True)
    payload = models.JSONField(_("event payload"), default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("calendar event")
        verbose_name_plural = _("calendar events")
        constraints = [
            models.UniqueConstraint(fields=["booking", "mapping"], name="calendar_event_booking_mapping_uniq")
        ]

    def __str__(self) -> str:
        return f"{self.booking.reference} · {self.get_status_display()}"
