"""Load tests for the scheduler helpers."""

from __future__ import annotations

import time
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.hotel.booking_service import confirm_booking, create_booking, expire_stale_holds
from apps.hotel.models import Booking, Room, RoomType
from apps.hotel.notifications import send_pending_confirmation_emails


def _make_room_type_and_rooms(count):
    rt = RoomType.objects.create(
        slug="load-room",
        name="Load Room",
        capacity=2,
        base_price=Decimal("400.00"),
        currency="PLN",
    )
    rooms = [Room.objects.create(number=f"L{i:03d}", room_type=rt, floor=1) for i in range(count)]
    return rt, rooms


@pytest.mark.django_db
def test_expire_stale_holds_handles_a_backlog():
    count = 100
    rt, _ = _make_room_type_and_rooms(count)
    now = timezone.now()
    for _i in range(count):
        booking = create_booking(
            room_type=rt,
            check_in=timezone.localdate() + timedelta(days=30),
            check_out=timezone.localdate() + timedelta(days=33),
            first_name="Load",
            last_name="Test",
            email="load@example.com",
            adults=2,
            hold_minutes=1,
            room=None,
        )
        Booking.objects.filter(pk=booking.pk).update(hold_expires_at=now - timedelta(minutes=5))

    start = time.perf_counter()
    expired = expire_stale_holds()
    elapsed = time.perf_counter() - start

    assert expired == count
    assert Booking.objects.filter(status=Booking.Status.EXPIRED).count() == count
    assert elapsed < 10, f"expire_stale_holds took {elapsed:.2f}s for {count} rows"


@pytest.mark.django_db
def test_send_pending_confirmation_emails_handles_a_backlog():
    count = 50
    rt, _ = _make_room_type_and_rooms(count)
    check_in = timezone.localdate() + timedelta(days=30)
    check_out = check_in + timedelta(days=3)
    for _ in range(count):
        booking = create_booking(
            room_type=rt,
            check_in=check_in,
            check_out=check_out,
            first_name="Load",
            last_name="Test",
            email="load@example.com",
            adults=2,
        )
        confirm_booking(booking.reference)

    start = time.perf_counter()
    sent = send_pending_confirmation_emails()
    elapsed = time.perf_counter() - start

    assert sent == count
    assert elapsed < 10, f"send_pending_confirmation_emails took {elapsed:.2f}s for {count} rows"
