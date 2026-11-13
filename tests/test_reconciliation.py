"""Calendar reconciliation: detect drift and repair it on demand."""

from __future__ import annotations

import pytest
from django.core.management import call_command

from apps.calendar_sync.models import BookingCalendarEvent, CalendarConnection, RoomCalendar
from apps.calendar_sync.providers import FakeCalendarProvider, get_calendar_provider
from apps.calendar_sync.services import build_event_payload, reconcile_calendars
from apps.hotel.booking_service import confirm_booking, create_booking
from apps.hotel.models import Booking


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
def confirmed_booking(room_type, rooms, stay_dates, calendar, django_capture_on_commit_callbacks):
    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        adults=2,
    )
    with django_capture_on_commit_callbacks(execute=True):
        confirm_booking(booking.reference)
    booking.refresh_from_db()
    return booking


def _record(booking: Booking) -> BookingCalendarEvent:
    return BookingCalendarEvent.objects.get(booking=booking)


def _provider(booking: Booking):
    return get_calendar_provider(_record(booking).mapping.connection)


@pytest.mark.django_db
def test_a_healthy_event_is_reported_as_ok(confirmed_booking):
    result = reconcile_calendars()

    assert result["apply"] is False
    assert result["summary"]["checked"] == 1
    assert result["summary"]["ok"] == 1
    assert result["issues"] == []


@pytest.mark.django_db
def test_missing_event_is_detected_and_recreated(confirmed_booking):
    record = _record(confirmed_booking)
    mapping = record.mapping
    FakeCalendarProvider.reset()

    dry_run = reconcile_calendars()
    assert dry_run["summary"]["missing"] == 1
    assert dry_run["summary"]["repaired"] == 0
    assert dry_run["issues"][0]["action"] == "recreate_event"

    applied = reconcile_calendars(apply=True)
    assert applied["summary"]["missing"] == 1
    assert applied["summary"]["repaired"] == 1

    record.refresh_from_db()
    assert record.status == BookingCalendarEvent.Status.SYNCED
    assert _provider(confirmed_booking).get_event(mapping.calendar_id, record.event_id) is not None


@pytest.mark.django_db
def test_modified_event_is_restored(confirmed_booking):
    record = _record(confirmed_booking)
    provider = _provider(confirmed_booking)
    tampered = dict(record.payload)
    tampered["summary"] = "Someone edited this"
    provider.update_event(record.mapping.calendar_id, record.event_id, tampered)

    dry_run = reconcile_calendars()
    assert dry_run["summary"]["modified"] == 1
    assert dry_run["issues"][0]["action"] == "update_event"

    reconcile_calendars(apply=True)

    actual = provider.get_event(record.mapping.calendar_id, record.event_id)
    assert actual["summary"] == build_event_payload(confirmed_booking)["summary"]


@pytest.mark.django_db
def test_stale_event_for_a_cancelled_booking_is_deleted(confirmed_booking):
    record = _record(confirmed_booking)
    Booking.objects.filter(pk=confirmed_booking.pk).update(status=Booking.Status.CANCELLED)
    confirmed_booking.status = Booking.Status.CANCELLED

    dry_run = reconcile_calendars()
    assert dry_run["summary"]["stale"] == 1
    assert dry_run["issues"][0]["action"] == "delete_event"

    reconcile_calendars(apply=True)

    record.refresh_from_db()
    assert record.status == BookingCalendarEvent.Status.DELETED
    assert record.event_id == ""
    assert _provider(confirmed_booking).get_event(record.mapping.calendar_id, record.event_id) is None


@pytest.mark.django_db
def test_confirmed_booking_without_a_record_is_created(confirmed_booking):
    BookingCalendarEvent.objects.filter(booking=confirmed_booking).delete()

    dry_run = reconcile_calendars()
    assert dry_run["summary"]["not_synced"] == 1

    reconcile_calendars(apply=True)

    record = _record(confirmed_booking)
    assert record.status == BookingCalendarEvent.Status.SYNCED
    assert record.event_id.startswith("fake-")


@pytest.mark.django_db
def test_reference_filter_limits_the_scope(confirmed_booking):
    BookingCalendarEvent.objects.filter(booking=confirmed_booking).delete()

    result = reconcile_calendars(reference="HA-NOPE00")

    assert result["summary"]["checked"] == 0
    assert result["issues"] == []


@pytest.mark.django_db
def test_command_defaults_to_a_dry_run(confirmed_booking, capsys):
    FakeCalendarProvider.reset()

    call_command("reconcile_calendars")

    output = capsys.readouterr().out
    assert "dry run" in output
    assert _record(confirmed_booking).event_id
    assert "missing 1" in output
