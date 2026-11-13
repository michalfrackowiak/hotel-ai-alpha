"""Synchronise bookings with the per-room calendars."""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.hotel.models import Booking

from .models import BookingCalendarEvent, RoomCalendar
from .providers import CalendarError, get_calendar_provider

logger = logging.getLogger(__name__)


def build_event_payload(booking: Booking) -> dict[str, Any]:
    hotel = settings.HOTEL
    client = booking.client
    description_lines = [
        f"Booking {booking.reference}",
        f"Guest: {client.full_name}",
        f"Email: {client.email or '—'}",
        f"Phone: {client.phone or '—'}",
        f"Guests: {booking.adults} adult(s), {booking.children} child(ren)",
        f"Source: {booking.get_source_display()}",
        f"Status: {booking.get_status_display()}",
        f"Total: {booking.total_price} {booking.currency}",
    ]
    if booking.special_requests:
        description_lines.append(f"Requests: {booking.special_requests}")

    confirmed = booking.status in (Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN)
    return {
        "summary": f"{booking.room.number} · {client.full_name} · {booking.reference}",
        "description": "\n".join(description_lines),
        "location": hotel["address"],
        "start": {"date": booking.check_in.isoformat()},
        "end": {"date": booking.check_out.isoformat()},
        "status": "confirmed" if confirmed else "tentative",
        "transparency": "opaque",
        "extendedProperties": {
            "private": {
                "booking_reference": booking.reference,
                "room": booking.room.number,
                "source": booking.source,
            }
        },
    }


def sync_booking(booking: Booking) -> BookingCalendarEvent | None:
    """Create, update or delete the calendar event for one booking.

    Never raises: a calendar outage must not roll back a reservation.
    """
    mapping = (
        RoomCalendar.objects.filter(room_id=booking.room_id, is_active=True)
        .select_related("connection")
        .first()
    )
    if mapping is None:
        return None
    connection = mapping.connection
    if not connection.is_active or not connection.sync_enabled:
        return None

    if booking.status not in (
        Booking.Status.CONFIRMED,
        Booking.Status.CHECKED_IN,
        Booking.Status.CHECKED_OUT,
        Booking.Status.CANCELLED,
        Booking.Status.EXPIRED,
        Booking.Status.NO_SHOW,
    ):
        return None

    record, _created = BookingCalendarEvent.objects.get_or_create(booking=booking, mapping=mapping)
    now = timezone.now()

    if booking.status in (Booking.Status.CANCELLED, Booking.Status.EXPIRED, Booking.Status.NO_SHOW):
        if record.event_id:
            try:
                get_calendar_provider(connection).delete_event(mapping.calendar_id, record.event_id)
            except CalendarError as exc:
                record.status = BookingCalendarEvent.Status.FAILED
                record.error_message = str(exc)
                record.last_synced_at = now
                record.save()
                logger.warning("Calendar delete failed for %s: %s", booking.reference, exc)
                return record
            except Exception as exc:  # noqa: BLE001
                record.status = BookingCalendarEvent.Status.FAILED
                record.error_message = f"Unexpected error: {exc}"
                record.last_synced_at = now
                record.save()
                return record
        record.event_id = ""
        record.status = BookingCalendarEvent.Status.DELETED
        record.error_message = ""
        record.last_synced_at = now
        record.save()
        return record

    if booking.status not in (Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN, Booking.Status.CHECKED_OUT):
        return None

    payload = build_event_payload(booking)
    try:
        provider = get_calendar_provider(connection)
        if record.event_id:
            event_id = provider.update_event(mapping.calendar_id, record.event_id, payload)
        else:
            event_id = provider.create_event(mapping.calendar_id, payload)
    except CalendarError as exc:
        record.status = BookingCalendarEvent.Status.FAILED
        record.error_message = str(exc)
        record.payload = payload
        record.last_synced_at = now
        record.save()
        logger.warning("Calendar sync failed for %s: %s", booking.reference, exc)
        return record
    except Exception as exc:  # noqa: BLE001
        record.status = BookingCalendarEvent.Status.FAILED
        record.error_message = f"Unexpected error: {exc}"
        record.payload = payload
        record.last_synced_at = now
        record.save()
        logger.exception("Unexpected calendar sync error for %s", booking.reference)
        return record

    record.event_id = event_id
    record.status = BookingCalendarEvent.Status.SYNCED
    record.error_message = ""
    record.payload = payload
    record.last_synced_at = now
    record.save()

    if booking.external_event_id != event_id:
        Booking.objects.filter(pk=booking.pk).update(external_event_id=event_id)
        booking.external_event_id = event_id

    return record


def sync_all_bookings() -> dict[str, int]:
    """Sync every active/confirmed booking. Returns counters for the CLI."""
    summary = {"synced": 0, "failed": 0, "skipped": 0}
    bookings = Booking.objects.filter(
        status__in=(Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN, Booking.Status.CHECKED_OUT)
    ).select_related("room", "client")
    for booking in bookings:
        record = sync_booking(booking)
        if record is None:
            summary["skipped"] += 1
        elif record.status == BookingCalendarEvent.Status.SYNCED:
            summary["synced"] += 1
        elif record.status == BookingCalendarEvent.Status.FAILED:
            summary["failed"] += 1
        else:
            summary["skipped"] += 1
    return summary


def retry_pending_events() -> int:
    """Retry failed calendar events (called by the sync_calendars command)."""
    retried = 0
    for record in BookingCalendarEvent.objects.filter(
        status=BookingCalendarEvent.Status.FAILED
    ).select_related("booking"):
        with transaction.atomic():
            sync_booking(record.booking)
        retried += 1
    return retried
