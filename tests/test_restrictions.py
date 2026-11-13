"""Rate-calendar restrictions: closed nights, closed-to-arrival and max stay."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from apps.hotel.availability import search_availability
from apps.hotel.models import DailyRate
from apps.hotel.pricing import StayError, quote


def test_closed_night_rejects_any_stay(room_type, stay_dates):
    check_in, check_out = stay_dates
    DailyRate.objects.create(
        room_type=room_type, date=check_in, rate=Decimal("400.00"), closed=True
    )

    with pytest.raises(StayError) as exc:
        quote(room_type, check_in, check_out, adults=2)

    assert exc.value.code == "date_closed"


def test_closed_to_arrival_rejects_checkin_on_that_date(room_type, stay_dates):
    check_in, check_out = stay_dates
    DailyRate.objects.create(
        room_type=room_type, date=check_in, rate=Decimal("400.00"), closed_to_arrival=True
    )

    with pytest.raises(StayError) as exc:
        quote(room_type, check_in, check_out, adults=2)

    assert exc.value.code == "closed_to_arrival"

    # A stay that starts a day later is allowed.
    priced = quote(room_type, check_in + timedelta(days=1), check_out, adults=2)
    assert priced.nights == 2


def test_max_nights_is_enforced(room_type, stay_dates):
    check_in, check_out = stay_dates
    DailyRate.objects.create(
        room_type=room_type, date=check_in, rate=Decimal("400.00"), max_nights=2
    )

    with pytest.raises(StayError) as exc:
        quote(room_type, check_in, check_out, adults=2)  # 3 nights

    assert exc.value.code == "max_nights"

    priced = quote(room_type, check_in, check_in + timedelta(days=2), adults=2)
    assert priced.nights == 2


def test_availability_skips_a_room_type_on_a_closed_night(
    room_type, rooms, deluxe_suite, deluxe_room, stay_dates
):
    check_in, check_out = stay_dates
    DailyRate.objects.create(
        room_type=room_type, date=check_in, rate=Decimal("400.00"), closed=True
    )

    results = search_availability(check_in, check_out, guests=2)

    assert [item["room_type"]["slug"] for item in results] == ["deluxe-suite"]


def test_availability_skips_a_room_type_closed_to_arrival(
    room_type, rooms, deluxe_suite, deluxe_room, stay_dates
):
    check_in, check_out = stay_dates
    DailyRate.objects.create(
        room_type=room_type, date=check_in, rate=Decimal("400.00"), closed_to_arrival=True
    )

    results = search_availability(check_in, check_out, guests=2)

    assert [item["room_type"]["slug"] for item in results] == ["deluxe-suite"]