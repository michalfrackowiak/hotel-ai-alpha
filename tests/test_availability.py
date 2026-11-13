"""Availability search and room blocking."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from apps.hotel.availability import available_rooms, search_availability
from apps.hotel.booking_service import cancel_booking, confirm_booking, create_booking
from apps.hotel.models import Booking


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


def test_search_lists_available_room_types(room_type, deluxe_suite, rooms, deluxe_room, stay_dates):
    check_in, check_out = stay_dates
    results = search_availability(check_in, check_out, guests=2)

    slugs = {item["room_type"]["slug"] for item in results}
    assert slugs == {"standard-double", "deluxe-suite"}
    assert all(item["available_rooms"] >= 1 for item in results)
    assert all(item["quote"]["total"] for item in results)


def test_capacity_filters_room_types(room_type, deluxe_suite, rooms, deluxe_room, stay_dates):
    check_in, check_out = stay_dates
    results = search_availability(check_in, check_out, guests=3)

    assert {item["room_type"]["slug"] for item in results} == {"deluxe-suite"}


def test_confirmed_booking_blocks_one_room(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    booking = _booking(room_type, check_in, check_out)
    confirm_booking(booking.reference)

    available = available_rooms(room_type, check_in, check_out)
    assert len(available) == 1
    assert available[0].pk != booking.room_id


def test_pending_hold_blocks_room(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    _booking(room_type, check_in, check_out)

    assert len(available_rooms(room_type, check_in, check_out)) == 1


def test_cancelled_booking_releases_room(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    booking = _booking(room_type, check_in, check_out)
    confirm_booking(booking.reference)
    cancel_booking(booking.reference)

    assert len(available_rooms(room_type, check_in, check_out)) == 2


def test_expired_hold_releases_room(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    booking = _booking(room_type, check_in, check_out, hold_minutes=5)
    Booking.objects.filter(pk=booking.pk).update(
        hold_expires_at=booking.hold_expires_at - timedelta(days=1)
    )

    assert len(available_rooms(room_type, check_in, check_out)) == 2


def test_no_availability_when_all_rooms_taken(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    for _ in range(2):
        _booking(room_type, check_in, check_out)

    results = search_availability(check_in, check_out, guests=2)
    assert results == []


def test_search_can_filter_to_one_room_type(room_type, deluxe_suite, rooms, deluxe_room, stay_dates):
    check_in, check_out = stay_dates
    results = search_availability(check_in, check_out, guests=2, room_type_slug="deluxe-suite")

    assert [item["room_type"]["slug"] for item in results] == ["deluxe-suite"]


def test_quote_total_is_gross(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    results = search_availability(check_in, check_out, guests=2)
    quote_data = results[0]["quote"]

    assert Decimal(quote_data["total"]) == Decimal(quote_data["subtotal"])
    assert quote_data["nights"] == 3
    assert quote_data["tax_rate"] == "0.08"
