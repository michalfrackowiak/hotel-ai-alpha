"""Channel-manager iCal import, room blocks and the signed export feed."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.hotel.availability import available_rooms
from apps.hotel.booking_service import BookingError, confirm_booking, create_booking
from apps.hotel.channel_feeds import (
    make_feed_token,
    parse_ical,
    resolve_feed_token,
    sync_feed,
)
from apps.hotel.models import ChannelFeed, RoomBlock


def _ical(*events: str) -> str:
    return "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Test//Channel feed//EN",
            *events,
            "END:VCALENDAR",
            "",
        ]
    )


def _event(uid: str, start: str, end: str, *, summary: str = "OTA booking", status: str = "CONFIRMED") -> str:
    return "\r\n".join(
        [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTART;VALUE=DATE:{start}",
            f"DTEND;VALUE=DATE:{end}",
            f"SUMMARY:{summary}",
            f"STATUS:{status}",
            "END:VEVENT",
        ]
    )


def _feed(room, name: str = "Booking.com") -> ChannelFeed:
    return ChannelFeed.objects.create(room=room, name=name, url="https://example.com/feed.ics")


def _booking(room_type, check_in, check_out, **overrides):
    data = {
        "room_type": room_type,
        "check_in": check_in,
        "check_out": check_out,
        "first_name": "Test",
        "last_name": "Guest",
        "email": "guest@example.com",
        "adults": 2,
    }
    data.update(overrides)
    return create_booking(**data)


def test_parse_ical_unfolds_lines_and_skips_cancelled_events():
    payload = _ical(
        "\r\n".join(
            [
                "BEGIN:VEVENT",
                "UID:folded@example.com",
                "DTSTART;VALUE=DATE:20261201",
                "DTEND;VALUE=DATE:20261204",
                "SUMMARY:A long OTA summary that is fol",
                " ded across two physical lines",
                "END:VEVENT",
            ]
        ),
        _event("cancelled@example.com", "20261205", "20261206", status="CANCELLED"),
    )

    events = parse_ical(payload)

    assert len(events) == 1
    assert events[0].uid == "folded@example.com"
    assert events[0].summary == "A long OTA summary that is folded across two physical lines"
    assert events[0].check_in.isoformat() == "2026-12-01"
    assert events[0].check_out.isoformat() == "2026-12-04"


def test_parse_ical_handles_utc_date_times_in_local_time():
    payload = _ical(
        "\r\n".join(
            [
                "BEGIN:VEVENT",
                "UID:timed@example.com",
                "DTSTART:20261201T230000Z",
                "DTEND:20261204T100000Z",
                "SUMMARY:Timed booking",
                "END:VEVENT",
            ]
        )
    )

    event = parse_ical(payload)[0]

    # 23:00 UTC on 1 Dec is already 2 Dec in Europe/Warsaw.
    assert event.check_in.isoformat() == "2026-12-02"
    assert event.check_out.isoformat() == "2026-12-04"


def test_sync_feed_creates_blocks_and_is_idempotent(room_type, rooms):
    feed = _feed(rooms[0])
    payload = _ical(_event("ota-1", "20261201", "20261204"))

    first = sync_feed(feed, text=payload)
    second = sync_feed(feed, text=payload)

    assert first.created == 1
    assert first.event_count == 1
    assert second.created == 0
    assert second.updated == 0
    assert second.deleted == 0
    assert feed.blocks.count() == 1
    feed.refresh_from_db()
    assert feed.last_synced_at is not None
    assert feed.last_error == ""
    assert feed.last_event_count == 1


def test_sync_feed_updates_and_removes_stale_blocks(rooms):
    feed = _feed(rooms[0])
    sync_feed(
        feed,
        text=_ical(
            _event("ota-1", "20261201", "20261204"),
            _event("ota-2", "20261205", "20261207"),
        ),
    )

    result = sync_feed(feed, text=_ical(_event("ota-1", "20261202", "20261205")))

    assert result.updated == 1
    assert result.deleted == 1
    assert result.event_count == 1
    block = feed.blocks.get()
    assert block.external_uid == "ota-1"
    assert block.check_in.isoformat() == "2026-12-02"
    assert block.check_out.isoformat() == "2026-12-05"


def test_dry_run_reports_changes_without_writing(rooms):
    feed = _feed(rooms[0])

    result = sync_feed(feed, text=_ical(_event("ota-1", "20261201", "20261204")), dry_run=True)

    assert result.created == 1
    assert RoomBlock.objects.count() == 0
    feed.refresh_from_db()
    assert feed.last_synced_at is None


def test_fetch_failure_keeps_existing_blocks(rooms, monkeypatch):
    feed = _feed(rooms[0])
    sync_feed(feed, text=_ical(_event("ota-1", "20261201", "20261204")))

    def explode(url):
        raise OSError("feed is down")

    monkeypatch.setattr("apps.hotel.channel_feeds.fetch_feed", explode)

    result = sync_feed(feed)

    assert result.ok is False
    assert "feed is down" in result.error
    assert feed.blocks.count() == 1
    feed.refresh_from_db()
    assert "feed is down" in feed.last_error


def test_channel_block_removes_room_from_availability(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    RoomBlock.objects.create(
        room=rooms[0],
        check_in=check_in,
        check_out=check_out,
        source=RoomBlock.Source.MANUAL,
        summary="Maintenance",
    )

    available = available_rooms(room_type, check_in, check_out)

    assert [room.number for room in available] == ["102"]


def test_fully_blocked_room_type_cannot_be_booked(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    for room in rooms:
        RoomBlock.objects.create(
            room=room,
            check_in=check_in,
            check_out=check_out,
            source=RoomBlock.Source.MANUAL,
        )

    with pytest.raises(BookingError) as excinfo:
        _booking(room_type, check_in, check_out)

    assert excinfo.value.code == "no_availability"


def test_booking_clean_rejects_a_room_block(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    booking = _booking(room_type, check_in, check_out)
    RoomBlock.objects.create(
        room=booking.room,
        check_in=check_in,
        check_out=check_out,
        source=RoomBlock.Source.MANUAL,
    )

    with pytest.raises(ValidationError) as excinfo:
        booking.full_clean()

    assert "blocked" in str(excinfo.value)


def test_sync_counts_conflicts_with_local_bookings(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    booking = _booking(room_type, check_in, check_out)
    feed = _feed(booking.room)
    payload = _ical(
        _event(
            "ota-conflict",
            f"{check_in:%Y%m%d}",
            f"{check_out:%Y%m%d}",
        )
    )

    result = sync_feed(feed, text=payload)

    assert result.conflicts == 1
    feed.refresh_from_db()
    assert feed.conflict_count == 1


def test_room_feed_token_round_trip(rooms):
    token = make_feed_token(rooms[0])

    assert resolve_feed_token(token) == rooms[0]
    assert resolve_feed_token("not-a-token") is None


def test_room_calendar_endpoint_exports_bookings_and_blocks(client, room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    booking = _booking(room_type, check_in, check_out)
    confirm_booking(booking.reference)
    RoomBlock.objects.create(
        room=rooms[0],
        check_in=check_in + timedelta(days=10),
        check_out=check_in + timedelta(days=12),
        source=RoomBlock.Source.MANUAL,
        summary="Maintenance",
    )

    response = client.get(reverse("web:room-calendar", kwargs={"token": make_feed_token(rooms[0])}))

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/calendar")
    body = response.content.decode()
    assert f"UID:{booking.reference}@" in body
    assert "SUMMARY:Maintenance" in body
    assert "STATUS:CONFIRMED" in body


def test_room_calendar_endpoint_rejects_bad_tokens(client, rooms):
    response = client.get("/calendar/bogus.ics")

    assert response.status_code == 404
