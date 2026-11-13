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


def _event_matches(expected: dict[str, Any], actual: dict[str, Any] | None) -> bool:
    if actual is None:
        return False
    for key in ("summary", "location"):
        if (actual.get(key) or "") != (expected.get(key) or ""):
            return False
    for key in ("start", "end"):
        if (actual.get(key) or {}).get("date") != (expected.get(key) or {}).get("date"):
            return False
    if actual.get("status") and actual.get("status") != expected.get("status"):
        return False
    return True


def _active_mapping(booking: Booking) -> RoomCalendar | None:
    return (
        RoomCalendar.objects.filter(room_id=booking.room_id, is_active=True)
        .select_related("connection")
        .first()
    )


def _mapping_is_active(mapping: RoomCalendar) -> bool:
    connection = mapping.connection
    return bool(connection and connection.is_active and connection.sync_enabled)


def _issue(
    *,
    booking: Booking,
    mapping: RoomCalendar,
    event_id: str,
    issue: str,
    action: str,
    repaired: bool,
    error: str = "",
) -> dict[str, Any]:
    return {
        "reference": booking.reference,
        "calendar_id": mapping.calendar_id,
        "event_id": event_id,
        "issue": issue,
        "action": action,
        "repaired": repaired,
        "error": error,
    }


def reconcile_calendars(*, apply: bool = False, reference: str | None = None) -> dict[str, Any]:
    """Detect drift between booking records and the room calendars.

    ``apply=False`` is a read-only dry run. ``apply=True`` recreates missing
    events, restores edited payloads, retries failed syncs and deletes events
    that belong to cancelled bookings.
    """
    summary = {
        "checked": 0,
        "ok": 0,
        "missing": 0,
        "modified": 0,
        "not_synced": 0,
        "failed": 0,
        "stale": 0,
        "repaired": 0,
    }
    issues: list[dict[str, Any]] = []
    reference_filter = (reference or "").strip().upper()

    records = BookingCalendarEvent.objects.select_related(
        "booking", "booking__room", "booking__room__room_type", "booking__client", "mapping", "mapping__connection"
    ).filter(mapping__isnull=False)
    if reference_filter:
        records = records.filter(booking__reference=reference_filter)

    for record in records:
        booking = record.booking
        mapping = record.mapping
        if not _mapping_is_active(mapping):
            continue
        summary["checked"] += 1
        provider = get_calendar_provider(mapping.connection)
        expected = build_event_payload(booking)

        if booking.status in (Booking.Status.CANCELLED, Booking.Status.EXPIRED, Booking.Status.NO_SHOW):
            if record.status == BookingCalendarEvent.Status.SYNCED and record.event_id:
                summary["stale"] += 1
                repaired = False
                error = ""
                if apply:
                    try:
                        sync_booking(booking)
                        repaired = True
                        summary["repaired"] += 1
                    except Exception as exc:  # noqa: BLE001
                        error = str(exc)
                issues.append(
                    _issue(
                        booking=booking,
                        mapping=mapping,
                        event_id=record.event_id,
                        issue="stale_event",
                        action="delete_event",
                        repaired=repaired,
                        error=error,
                    )
                )
            continue

        if booking.status not in (Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN, Booking.Status.CHECKED_OUT):
            continue

        if record.status == BookingCalendarEvent.Status.FAILED:
            summary["failed"] += 1
            repaired = False
            error = record.error_message
            if apply:
                try:
                    sync_booking(booking)
                    repaired = True
                    summary["repaired"] += 1
                    error = ""
                except Exception as exc:  # noqa: BLE001
                    error = str(exc)
            issues.append(
                _issue(
                    booking=booking,
                    mapping=mapping,
                    event_id=record.event_id,
                    issue="failed",
                    action="retry_sync",
                    repaired=repaired,
                    error=error,
                )
            )
            continue

        if not record.event_id:
            summary["not_synced"] += 1
            repaired = False
            error = ""
            if apply:
                try:
                    sync_booking(booking)
                    repaired = True
                    summary["repaired"] += 1
                except Exception as exc:  # noqa: BLE001
                    error = str(exc)
            issues.append(
                _issue(
                    booking=booking,
                    mapping=mapping,
                    event_id="",
                    issue="not_synced",
                    action="create_event",
                    repaired=repaired,
                    error=error,
                )
            )
            continue

        try:
            actual = provider.get_event(mapping.calendar_id, record.event_id)
        except Exception as exc:  # noqa: BLE001
            issues.append(
                _issue(
                    booking=booking,
                    mapping=mapping,
                    event_id=record.event_id,
                    issue="unreachable",
                    action="none",
                    repaired=False,
                    error=str(exc),
                )
            )
            continue

        if actual is None:
            summary["missing"] += 1
            repaired = False
            error = ""
            if apply:
                try:
                    event_id = provider.create_event(mapping.calendar_id, expected)
                    record.event_id = event_id
                    record.status = BookingCalendarEvent.Status.SYNCED
                    record.payload = expected
                    record.error_message = ""
                    record.last_synced_at = timezone.now()
                    record.save()
                    Booking.objects.filter(pk=booking.pk).update(external_event_id=event_id)
                    booking.external_event_id = event_id
                    repaired = True
                    summary["repaired"] += 1
                except Exception as exc:  # noqa: BLE001
                    error = str(exc)
            issues.append(
                _issue(
                    booking=booking,
                    mapping=mapping,
                    event_id=record.event_id,
                    issue="missing",
                    action="recreate_event",
                    repaired=repaired,
                    error=error,
                )
            )
            continue

        if not _event_matches(expected, actual):
            summary["modified"] += 1
            repaired = False
            error = ""
            if apply:
                try:
                    provider.update_event(mapping.calendar_id, record.event_id, expected)
                    record.status = BookingCalendarEvent.Status.SYNCED
                    record.payload = expected
                    record.error_message = ""
                    record.last_synced_at = timezone.now()
                    record.save()
                    repaired = True
                    summary["repaired"] += 1
                except Exception as exc:  # noqa: BLE001
                    error = str(exc)
            issues.append(
                _issue(
                    booking=booking,
                    mapping=mapping,
                    event_id=record.event_id,
                    issue="modified",
                    action="update_event",
                    repaired=repaired,
                    error=error,
                )
            )
            continue

        summary["ok"] += 1

    # Confirmed bookings that never got a calendar record at all.
    booked_with_records = set(
        BookingCalendarEvent.objects.values_list("booking_id", flat=True)
    )
    bookings = Booking.objects.filter(
        status__in=(Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN, Booking.Status.CHECKED_OUT)
    ).select_related("room", "room__room_type", "client")
    if reference_filter:
        bookings = bookings.filter(reference=reference_filter)

    for booking in bookings:
        if booking.pk in booked_with_records:
            continue
        mapping = _active_mapping(booking)
        if mapping is None or not _mapping_is_active(mapping):
            continue
        summary["checked"] += 1
        summary["not_synced"] += 1
        repaired = False
        error = ""
        if apply:
            try:
                sync_booking(booking)
                repaired = True
                summary["repaired"] += 1
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
        issues.append(
            _issue(
                booking=booking,
                mapping=mapping,
                event_id="",
                issue="not_synced",
                action="create_event",
                repaired=repaired,
                error=error,
            )
        )

    return {"apply": apply, "summary": summary, "issues": issues}
