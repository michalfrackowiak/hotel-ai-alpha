"""Per-room calendar synchronisation with the fake provider."""

from __future__ import annotations

import pytest

from apps.calendar_sync.models import BookingCalendarEvent, CalendarConnection, RoomCalendar
from apps.calendar_sync.services import build_event_payload, sync_booking
from apps.hotel.booking_service import cancel_booking, confirm_booking, create_booking


@pytest.fixture
def calendar(rooms):
    connection = CalendarConnection.objects.create(
        name="Test calendar",
        provider=CalendarConnection.Provider.FAKE,
        calendar_id="test@hotel.local",
        sync_enabled=True,
    )
    for room in rooms:
        RoomCalendar.objects.create(
            room=room,
            connection=connection,
            calendar_id=f"room-{room.number}@hotel.local",
        )
    return connection


@pytest.fixture
def booking(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    return create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        adults=2,
    )


@pytest.mark.django_db
def test_event_payload_contains_booking_details(booking):
    payload = build_event_payload(booking)

    assert booking.reference in payload["summary"]
    assert payload["start"]["date"] == booking.check_in.isoformat()
    assert payload["end"]["date"] == booking.check_out.isoformat()
    assert payload["extendedProperties"]["private"]["booking_reference"] == booking.reference
    assert payload["status"] == "tentative"


@pytest.mark.django_db
def test_pending_booking_is_not_synced(booking, calendar):
    assert sync_booking(booking) is None
    assert BookingCalendarEvent.objects.count() == 0


@pytest.mark.django_db
def test_confirmation_syncs_event(booking, calendar, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        confirm_booking(booking.reference)

    record = BookingCalendarEvent.objects.get(booking=booking)
    assert record.status == BookingCalendarEvent.Status.SYNCED
    assert record.event_id.startswith("fake-")
    assert record.mapping.calendar_id.startswith("room-")

    booking.refresh_from_db()
    assert booking.external_event_id == record.event_id
    assert record.payload["status"] == "confirmed"


@pytest.mark.django_db
def test_cancellation_deletes_event(booking, calendar, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        confirm_booking(booking.reference)
    event_id = BookingCalendarEvent.objects.get(booking=booking).event_id

    with django_capture_on_commit_callbacks(execute=True):
        cancel_booking(booking.reference)

    record = BookingCalendarEvent.objects.get(booking=booking)
    assert record.status == BookingCalendarEvent.Status.DELETED
    assert record.event_id == ""
    assert event_id


@pytest.mark.django_db
def test_each_room_has_its_own_mapping(rooms, calendar):
    mappings = RoomCalendar.objects.filter(connection=calendar)

    assert mappings.count() == len(rooms)
    assert {mapping.calendar_id for mapping in mappings} == {
        f"room-{room.number}@hotel.local" for room in rooms
    }


@pytest.mark.django_db
def test_sync_is_skipped_without_mapping(booking):
    assert sync_booking(booking) is None
