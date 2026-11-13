"""Shared fixtures for the hotel alpha test suite."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.hotel.models import Room, RoomType


def make_room_type(slug: str, **overrides) -> RoomType:
    defaults = {
        "slug": slug,
        "name": slug.replace("-", " ").title(),
        "short_description": "A calm room for the test suite.",
        "capacity": 2,
        "base_price": Decimal("400.00"),
        "currency": "PLN",
    }
    defaults.update(overrides)
    return RoomType.objects.create(**defaults)


@pytest.fixture
def room_type(db) -> RoomType:
    return make_room_type("standard-double", name="Standard Double")


@pytest.fixture
def deluxe_suite(db) -> RoomType:
    return make_room_type(
        "deluxe-suite",
        name="Deluxe Suite",
        capacity=4,
        base_price=Decimal("800.00"),
    )


@pytest.fixture
def rooms(room_type) -> list[Room]:
    return [
        Room.objects.create(number="101", room_type=room_type, floor=1),
        Room.objects.create(number="102", room_type=room_type, floor=1),
    ]


@pytest.fixture
def deluxe_room(deluxe_suite) -> Room:
    return Room.objects.create(number="201", room_type=deluxe_suite, floor=2)


@pytest.fixture
def stay_dates():
    """A future three-night stay."""
    start = timezone.localdate() + timedelta(days=30)
    return start, start + timedelta(days=3)


@pytest.fixture
def next_friday():
    today = timezone.localdate()
    offset = (4 - today.weekday()) % 7 or 7
    return today + timedelta(days=offset)
