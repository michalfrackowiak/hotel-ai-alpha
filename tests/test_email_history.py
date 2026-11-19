"""Per-message confirmation email history: rows, webhook sync and ops view."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib import admin
from django.utils import timezone

from apps.hotel import notifications
from apps.hotel.admin import EmailDeliveryAdmin
from apps.hotel.booking_service import create_booking
from apps.hotel.email_providers import DeliveryResult
from apps.hotel.models import Booking, EmailDelivery
from apps.hotel.notifications import send_booking_confirmation


class FailingProvider:
    name = "failing"

    def send(self, message):
        return DeliveryResult(sent=False, error="smtp rejected the message")


@pytest.fixture
def booking(room_type, rooms, stay_dates):
    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        adults=2,
        language="en",
    )
    booking.status = Booking.Status.CONFIRMED
    booking.save(update_fields=["status"])
    return booking


def _delivery(booking, *, attempt: int, status: str, message_id: str = "", created_at=None) -> EmailDelivery:
    delivery = EmailDelivery.objects.create(
        booking=booking,
        attempt=attempt,
        status=status,
        provider="postmark",
        provider_message_id=message_id,
        recipient="anna@example.com",
        subject=f"Booking confirmed · {booking.reference}",
    )
    if created_at is not None:
        EmailDelivery.objects.filter(pk=delivery.pk).update(created_at=created_at)
        delivery.refresh_from_db()
    return delivery


@pytest.mark.django_db
def test_send_and_retry_append_one_row_per_attempt(booking, monkeypatch):
    monkeypatch.setattr(notifications, "get_email_provider", lambda: FailingProvider())
    assert send_booking_confirmation(booking) is False

    monkeypatch.undo()
    assert send_booking_confirmation(booking, force=True) is True

    deliveries = list(EmailDelivery.objects.filter(booking=booking).order_by("attempt"))
    assert [d.attempt for d in deliveries] == [1, 2]
    assert [d.status for d in deliveries] == [
        EmailDelivery.Status.FAILED,
        EmailDelivery.Status.SENT,
    ]
    assert [d.provider for d in deliveries] == ["failing", "smtp"]
    assert all(d.recipient == "anna@example.com" for d in deliveries)
    assert all(d.subject == f"Booking confirmed · {booking.reference}" for d in deliveries)
    assert deliveries[0].error_message == "smtp rejected the message"
    assert deliveries[1].error_message == ""


@pytest.mark.django_db
def test_webhook_updates_the_delivery_matching_its_message_id(booking, client):
    older = _delivery(
        booking,
        attempt=1,
        status=EmailDelivery.Status.SENT,
        message_id="pm-old",
        created_at=timezone.now() - timedelta(minutes=1),
    )
    newer = _delivery(
        booking,
        attempt=2,
        status=EmailDelivery.Status.SENT,
        message_id="pm-new",
    )
    booking.confirmation_email_provider_id = "pm-old"
    booking.save(update_fields=["confirmation_email_provider_id"])

    response = client.post(
        "/api/v1/email/webhooks/postmark/",
        {"RecordType": "Delivery", "MessageID": "pm-old"},
        content_type="application/json",
    )

    assert response.status_code == 200
    older.refresh_from_db()
    newer.refresh_from_db()
    assert older.status == EmailDelivery.Status.DELIVERED
    assert newer.status == EmailDelivery.Status.SENT


@pytest.mark.django_db
def test_webhook_without_message_id_updates_the_latest_delivery(booking, client):
    _delivery(
        booking,
        attempt=1,
        status=EmailDelivery.Status.SENT,
        created_at=timezone.now() - timedelta(minutes=1),
    )
    latest = _delivery(booking, attempt=2, status=EmailDelivery.Status.SENT)

    response = client.post(
        "/api/v1/email/webhooks/ses/",
        {"reference": booking.reference, "event": "bounced", "reason": "mailbox full"},
        content_type="application/json",
    )

    assert response.status_code == 200
    latest.refresh_from_db()
    assert latest.status == EmailDelivery.Status.BOUNCED
    assert latest.error_message == "mailbox full"


@pytest.mark.django_db
def test_webhook_with_no_delivery_rows_is_safe(booking, client):
    response = client.post(
        "/api/v1/email/webhooks/ses/",
        {"reference": booking.reference, "event": "delivered"},
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert EmailDelivery.objects.filter(booking=booking).count() == 0


@pytest.mark.django_db
def test_ops_dashboard_lists_email_history_for_staff(booking, client):
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(username="reception", password="pw", is_staff=True)
    client.force_login(user)
    delivery = _delivery(booking, attempt=2, status=EmailDelivery.Status.DELIVERED)

    response = client.get("/ops/", HTTP_ACCEPT_LANGUAGE="en")

    assert response.status_code == 200
    content = response.content.decode()
    assert "Email history" in content
    assert "Sent at" in content
    assert booking.reference in content
    assert delivery.subject in content
    assert "Delivered" in content


@pytest.mark.django_db
def test_ops_email_history_is_translated(booking, client):
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(username="reception", password="pw", is_staff=True)
    client.force_login(user)

    response = client.get("/ops/", HTTP_ACCEPT_LANGUAGE="pl")

    assert response.status_code == 200
    assert "Historia e-maili" in response.content.decode()


@pytest.mark.django_db
def test_email_delivery_admin_is_read_only():
    model_admin = EmailDeliveryAdmin(EmailDelivery, admin.site)

    assert model_admin.has_add_permission(None) is False
    assert model_admin.has_change_permission(None) is False
    assert model_admin.has_delete_permission(None) is False


@pytest.mark.django_db
def test_email_delivery_admin_lists_history(booking, client):
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_superuser(
        username="boss", password="pw", email="boss@example.com"
    )
    client.force_login(user)
    delivery = _delivery(booking, attempt=1, status=EmailDelivery.Status.SENT)

    response = client.get("/admin/hotel/emaildelivery/")

    assert response.status_code == 200
    content = response.content.decode()
    assert delivery.subject in content
    assert "/admin/hotel/emaildelivery/add/" not in content
