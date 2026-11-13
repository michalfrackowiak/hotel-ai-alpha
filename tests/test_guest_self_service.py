"""Guest self-service: cancellation policy, modifications, audit and rates."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone
from rest_framework.test import APIClient

from apps.hotel.access import make_booking_token
from apps.hotel.availability import search_availability
from apps.hotel.booking_service import (
    BookingError,
    cancel_booking,
    confirm_booking,
    create_booking,
    expire_stale_holds,
    modify_booking,
)
from apps.hotel.cancellation import cancellation_preview
from apps.hotel.models import Booking, BookingEvent, DailyRate
from apps.hotel.pricing import StayError, quote


def _booking(room_type, check_in, check_out, **overrides):
    data = {
        "room_type": room_type,
        "check_in": check_in,
        "check_out": check_out,
        "first_name": "Anna",
        "last_name": "Kowalska",
        "email": "anna@example.com",
        "phone": "+48 600 100 200",
        "adults": 2,
    }
    data.update(overrides)
    return create_booking(**data)


@pytest.fixture
def booking(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    return _booking(room_type, check_in, check_out)


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def staff_client(db) -> Client:
    client = Client()
    user = get_user_model().objects.create_user(
        username="reception",
        password="not-a-real-password",
        is_staff=True,
    )
    client.force_login(user)
    return client


# --------------------------------------------------------------- cancellation


def test_pending_hold_is_always_free_to_release(booking):
    preview = cancellation_preview(booking)

    assert booking.status == Booking.Status.PENDING
    assert preview.is_cancellable is True
    assert preview.is_free is True
    assert preview.fee == Decimal("0.00")
    assert preview.reason == "pending_hold"


def test_confirmed_booking_inside_free_window_has_no_fee(booking):
    booking = confirm_booking(booking.reference, actor="guest", channel="web")

    preview = cancellation_preview(booking)

    assert preview.is_cancellable is True
    assert preview.is_free is True
    assert preview.fee == Decimal("0.00")
    assert preview.reason == "free_window"
    assert preview.free_until == booking.check_in - timedelta(days=3)


def test_late_cancellation_charges_the_first_night(room_type, rooms):
    check_in = timezone.localdate() + timedelta(days=1)
    booking = _booking(room_type, check_in, check_in + timedelta(days=1))
    booking = confirm_booking(booking.reference, actor="guest", channel="web")

    preview = cancellation_preview(booking)

    assert preview.reason == "late_cancellation"
    assert preview.is_free is False
    assert preview.fee == Decimal("400.00")
    assert preview.fee_nights == 1


def test_cancellation_fee_is_capped_at_the_booking_total(room_type, rooms, monkeypatch, settings):
    monkeypatch.setitem(settings.HOTEL, "cancellation_fee_nights", 5)
    check_in = timezone.localdate() + timedelta(days=1)
    booking = _booking(room_type, check_in, check_in + timedelta(days=3))
    booking = confirm_booking(booking.reference, actor="guest", channel="web")

    preview = cancellation_preview(booking)

    assert preview.fee == booking.total_price == Decimal("1200.00")


def test_cancel_booking_records_policy_fields_and_audit_event(room_type, rooms):
    check_in = timezone.localdate() + timedelta(days=1)
    booking = _booking(room_type, check_in, check_in + timedelta(days=1))
    confirm_booking(booking.reference, actor="guest", channel="web")

    cancelled = cancel_booking(
        booking.reference,
        reason=Booking.CancellationReason.PLANS_CHANGED,
        note="  Flights moved.  ",
        actor=BookingEvent.Actor.GUEST,
        channel="web",
    )

    assert cancelled.status == Booking.Status.CANCELLED
    assert cancelled.cancellation_reason == Booking.CancellationReason.PLANS_CHANGED
    assert cancelled.cancellation_note == "Flights moved."
    assert cancelled.cancellation_fee == Decimal("400.00")
    assert cancelled.cancelled_by == BookingEvent.Actor.GUEST
    assert cancelled.cancelled_at is not None

    event = cancelled.events.get(event_type=BookingEvent.Type.CANCELLED)
    assert event.actor == BookingEvent.Actor.GUEST
    assert event.data["fee"] == "400.00"
    assert event.data["reason"] == Booking.CancellationReason.PLANS_CHANGED


def test_cancel_rejects_an_unknown_reason(booking):
    with pytest.raises(BookingError) as exc:
        cancel_booking(booking.reference, reason="because")

    assert exc.value.code == "invalid_reason"


def test_cancelling_twice_is_idempotent(booking):
    first = cancel_booking(booking.reference, actor="guest")
    second = cancel_booking(booking.reference, actor="guest")

    assert second.status == Booking.Status.CANCELLED
    assert second.cancellation_fee == first.cancellation_fee
    assert booking.events.filter(event_type=BookingEvent.Type.CANCELLED).count() == 1


def test_preview_api_requires_a_token(api_client, booking):
    url = f"/api/v1/bookings/{booking.reference}/cancellation-preview/"

    assert api_client.get(url).status_code == 403

    response = api_client.get(url, HTTP_X_BOOKING_TOKEN=make_booking_token(booking))
    assert response.status_code == 200
    assert response.json()["preview"]["reason"] == "pending_hold"


def test_cancel_api_records_the_reason(api_client, booking):
    response = api_client.post(
        f"/api/v1/bookings/{booking.reference}/cancel/",
        {"reason": "price", "note": "Found a cheaper stay."},
        format="json",
        HTTP_X_BOOKING_TOKEN=make_booking_token(booking),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == Booking.Status.CANCELLED
    assert payload["cancellation_reason"] == "price"
    assert payload["cancellation_note"] == "Found a cheaper stay."
    assert payload["cancelled_by"] == "guest"


# --------------------------------------------------------------- modification


def test_modify_dates_recalculates_the_price_and_audits(booking):
    new_check_out = booking.check_in + timedelta(days=2)

    modified = modify_booking(
        booking.reference,
        check_out=new_check_out,
        actor=BookingEvent.Actor.GUEST,
        channel="web",
    )

    assert modified.check_out == new_check_out
    assert modified.total_price == Decimal("800.00")
    event = modified.events.get(event_type=BookingEvent.Type.MODIFIED)
    assert event.data["before"]["check_out"] == booking.check_out.isoformat()
    assert event.data["after"]["check_out"] == new_check_out.isoformat()


def test_modify_room_type_assigns_an_available_room(booking, deluxe_suite, deluxe_room):
    modified = modify_booking(booking.reference, room_type=deluxe_suite)

    assert modified.room == deluxe_room
    assert modified.room.room_type == deluxe_suite
    assert modified.total_price == Decimal("2400.00")


def test_modify_rejects_a_cancelled_booking(booking):
    cancel_booking(booking.reference, actor="guest")

    with pytest.raises(BookingError) as exc:
        modify_booking(booking.reference, check_out=booking.check_in + timedelta(days=2))

    assert exc.value.code == "not_modifiable"


def test_modify_api_updates_the_reservation(api_client, booking):
    new_check_out = booking.check_in + timedelta(days=2)

    response = api_client.patch(
        f"/api/v1/bookings/{booking.reference}/",
        {"check_out": new_check_out.isoformat()},
        format="json",
        HTTP_X_BOOKING_TOKEN=make_booking_token(booking),
    )

    assert response.status_code == 200
    assert response.json()["check_out"] == new_check_out.isoformat()
    assert response.json()["total_price"] == "800.00"


def test_modify_api_requires_a_token(api_client, booking):
    response = api_client.patch(
        f"/api/v1/bookings/{booking.reference}/",
        {"check_out": (booking.check_in + timedelta(days=2)).isoformat()},
        format="json",
    )

    assert response.status_code == 403


# ---------------------------------------------------------------------- audit


def test_creation_and_confirmation_append_events(booking):
    created = booking.events.get(event_type=BookingEvent.Type.CREATED)
    assert created.actor == BookingEvent.Actor.GUEST
    assert created.channel == Booking.Source.WEB

    confirm_booking(booking.reference, actor=BookingEvent.Actor.STAFF, channel="admin")

    confirmed = booking.events.get(event_type=BookingEvent.Type.CONFIRMED)
    assert confirmed.actor == BookingEvent.Actor.STAFF
    assert confirmed.channel == "admin"


def test_expiring_a_stale_hold_appends_an_event(room_type, rooms):
    check_in = timezone.localdate() + timedelta(days=1)
    booking = _booking(room_type, check_in, check_in + timedelta(days=1), hold_minutes=-1)

    assert expire_stale_holds() == 1

    booking.refresh_from_db()
    assert booking.status == Booking.Status.EXPIRED
    event = booking.events.get(event_type=BookingEvent.Type.EXPIRED)
    assert event.actor == BookingEvent.Actor.SYSTEM


def test_booking_events_are_append_only(booking):
    event = booking.events.first()

    event.summary = "rewritten history"
    with pytest.raises(ValueError):
        event.save()


# ---------------------------------------------------------------- rate calendar


def test_daily_rate_overrides_the_calculated_price(room_type, stay_dates):
    check_in, check_out = stay_dates
    DailyRate.objects.create(room_type=room_type, date=check_in, rate=Decimal("500.00"))

    priced = quote(room_type, check_in, check_out, adults=2)

    assert priced.nightly[0].rate == Decimal("500.00")
    assert priced.total == Decimal("1300.00")


def test_minimum_stay_is_enforced(room_type, stay_dates):
    check_in, _ = stay_dates
    DailyRate.objects.create(
        room_type=room_type,
        date=check_in,
        rate=Decimal("500.00"),
        min_nights=2,
    )

    with pytest.raises(StayError) as exc:
        quote(room_type, check_in, check_in + timedelta(days=1), adults=2)

    assert exc.value.code == "min_nights"


def test_availability_skips_a_room_type_that_violates_minimum_stay(
    room_type, rooms, deluxe_suite, deluxe_room, stay_dates
):
    check_in, check_out = stay_dates
    DailyRate.objects.create(
        room_type=room_type,
        date=check_in,
        rate=Decimal("500.00"),
        min_nights=5,
    )

    results = search_availability(check_in, check_out, guests=2)

    assert [item["room_type"]["slug"] for item in results] == ["deluxe-suite"]


def test_rate_calendar_page_renders_and_saves(staff_client, room_type, rooms):
    url = "/admin/hotel/dailyrate/calendar/?year=2026&month=12"
    response = staff_client.get(url)

    assert response.status_code == 200
    assert "Rate calendar" in response.content.decode()

    response = staff_client.post(
        "/admin/hotel/dailyrate/calendar/",
        {
            "year": 2026,
            "month": 12,
            f"rate_{room_type.pk}_2026-12-01": "550.00",
            f"min_{room_type.pk}_2026-12-01": "2",
        },
    )

    assert response.status_code == 302
    record = DailyRate.objects.get(room_type=room_type, date="2026-12-01")
    assert record.rate == Decimal("550.00")
    assert record.min_nights == 2


# --------------------------------------------------------------- ops dashboard


@pytest.mark.django_db
def test_ops_dashboard_requires_staff():
    response = Client().get("/ops/")

    assert response.status_code == 302
    assert "next=/ops/" in response["Location"]


def test_ops_dashboard_lists_today_arrivals(staff_client, room_type, rooms):
    today = timezone.localdate()
    booking = _booking(room_type, today, today + timedelta(days=1))
    confirm_booking(booking.reference, actor="staff", channel="admin")

    response = staff_client.get("/ops/")

    assert response.status_code == 200
    content = response.content.decode()
    assert "Operations" in content
    assert booking.reference in content


def test_ops_dashboard_renders_in_polish(staff_client):
    response = staff_client.get("/ops/", HTTP_ACCEPT_LANGUAGE="pl")

    assert response.status_code == 200
    content = response.content.decode()
    assert "Operacje" in content
    assert "Przyjazdy" in content


def test_booking_page_shows_the_cancellation_policy_in_polish(booking):
    from apps.hotel.access import booking_manage_path

    booking = confirm_booking(booking.reference, actor="guest", channel="web")
    response = Client().get(booking_manage_path(booking), HTTP_ACCEPT_LANGUAGE="pl")

    assert response.status_code == 200
    content = response.content.decode()
    assert "Bezpłatne anulowanie" in content
    assert "Zmień rezerwację" in content
