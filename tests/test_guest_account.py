"""Passwordless guest accounts and magic-link sign-in."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.hotel.booking_service import create_booking
from apps.hotel.guest_access import GUEST_SESSION_KEY, issue_login_token
from apps.hotel.models import GuestLoginToken


@pytest.fixture(autouse=True)
def clear_login_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def anna(room_type, rooms, stay_dates):
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


def _login_path(message) -> str:
    match = re.search(r"/my-bookings/login/[^\s]+", message.body)
    assert match, message.body
    return match.group(0)


@pytest.mark.django_db
def test_requesting_a_link_sends_one_email(client, anna):
    response = client.post(
        "/my-bookings/", {"email": "Anna@Example.com"}, HTTP_ACCEPT_LANGUAGE="en"
    )

    assert response.status_code == 200
    content = response.content.decode()
    assert "sign-in link is on its way" in content
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["anna@example.com"]
    assert "/my-bookings/login/" in mail.outbox[0].body


@pytest.mark.django_db
def test_unknown_email_gets_the_same_neutral_response(client, anna):
    response = client.post(
        "/my-bookings/", {"email": "nobody@example.com"}, HTTP_ACCEPT_LANGUAGE="en"
    )

    assert response.status_code == 200
    assert "sign-in link is on its way" in response.content.decode()
    assert mail.outbox == []


@pytest.mark.django_db
def test_magic_link_signs_in_and_lists_every_booking(client, room_type, rooms, anna):
    check_in = timezone.localdate() + timedelta(days=60)
    second = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_in + timedelta(days=2),
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        adults=1,
    )

    client.post("/my-bookings/", {"email": "anna@example.com"})
    path = _login_path(mail.outbox[0])

    response = client.get(path)
    assert response.status_code == 302
    assert response["Location"] == "/my-bookings/"

    listing = client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    content = listing.content.decode()
    assert listing.status_code == 200
    assert "anna@example.com" in content
    assert anna.reference in content
    assert second.reference in content
    assert client.session[GUEST_SESSION_KEY] == "anna@example.com"
    # Unified My bookings shows rate/payment summary and a clear path to details.
    assert "View reservation" in content
    assert "Standard rate" in content
    assert "Payment" in content
    assert "Balance due" in content
    assert f'/booking/{anna.reference}/' in content


@pytest.mark.django_db
def test_a_magic_link_can_only_be_used_once(client, anna):
    _record, raw_token = issue_login_token("anna@example.com")
    path = f"/my-bookings/login/{raw_token}/"

    first = client.get(path)
    assert first.status_code == 302

    second = Client().get(path, HTTP_ACCEPT_LANGUAGE="en")
    assert second.status_code == 400
    assert "invalid, already used or expired" in second.content.decode()


@pytest.mark.django_db
def test_magic_link_ip_cooldown_does_not_consume_email_cooldown(client, anna):
    from apps.hotel.guest_access import _throttle_key

    ip_key = _throttle_key("ip", "10.0.0.1")
    cache.set(ip_key, 1, timeout=60)

    response = client.post(
        "/my-bookings/",
        {"email": "anna@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
        REMOTE_ADDR="10.0.0.1",
    )
    assert response.status_code == 200
    assert "sign-in link is on its way" in response.content.decode()
    assert mail.outbox == []

    # A different IP must not be blocked by an email cooldown that was never
    # legitimately reserved for this request.
    response = client.post(
        "/my-bookings/",
        {"email": "anna@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
        REMOTE_ADDR="10.0.0.2",
    )
    assert response.status_code == 200
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["anna@example.com"]


@pytest.mark.django_db
def test_expired_magic_link_is_rejected(client, anna):
    record, raw_token = issue_login_token("anna@example.com")
    record.expires_at = timezone.now() - timedelta(minutes=1)
    record.save(update_fields=["expires_at"])

    response = client.get(f"/my-bookings/login/{raw_token}/")

    assert response.status_code == 400
    assert GUEST_SESSION_KEY not in client.session


@pytest.mark.django_db
def test_tokens_are_stored_hashed(client, anna):
    _record, raw_token = issue_login_token("anna@example.com")

    record = GuestLoginToken.objects.get()
    assert record.token_hash != raw_token
    assert len(record.token_hash) == 64


@pytest.mark.django_db
def test_sign_out_clears_the_guest_session(client, anna):
    _record, raw_token = issue_login_token("anna@example.com")
    client.get(f"/my-bookings/login/{raw_token}/")

    response = client.post("/my-bookings/logout/")

    assert response.status_code == 302
    assert GUEST_SESSION_KEY not in client.session


@pytest.mark.django_db
def test_signed_in_guest_can_open_a_booking_without_a_token(client, anna):
    _record, raw_token = issue_login_token("anna@example.com")
    client.get(f"/my-bookings/login/{raw_token}/")

    response = client.get(f"/booking/{anna.reference}/")

    assert response.status_code == 200
    assert anna.reference in response.content.decode()


@pytest.mark.django_db
def test_signed_in_guest_cannot_open_someone_elses_booking(client, room_type, rooms, anna, stay_dates):
    check_in, check_out = stay_dates
    other = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Jan",
        last_name="Nowak",
        email="jan@example.com",
        adults=1,
    )
    _record, raw_token = issue_login_token("anna@example.com")
    client.get(f"/my-bookings/login/{raw_token}/")

    response = client.get(f"/booking/{other.reference}/")

    assert response.status_code == 403


@pytest.mark.django_db
def test_login_requests_are_rate_limited_per_email(client, anna):
    first = client.post(
        "/my-bookings/", {"email": "anna@example.com"}, HTTP_ACCEPT_LANGUAGE="en"
    )
    second = client.post(
        "/my-bookings/", {"email": "anna@example.com"}, HTTP_ACCEPT_LANGUAGE="en"
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert "sign-in link is on its way" in second.content.decode()
    assert len(mail.outbox) == 1

@pytest.mark.django_db
def test_magic_link_email_cooldown_is_neutral_across_ips(client, anna, settings):
    settings.GUEST_LOGIN_COOLDOWN_SECONDS = 300
    settings.GUEST_LOGIN_IP_COOLDOWN_SECONDS = 60

    first = client.post(
        "/my-bookings/",
        {"email": "anna@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
        REMOTE_ADDR="10.0.0.1",
    )
    second = client.post(
        "/my-bookings/",
        {"email": "anna@example.com"},
        HTTP_ACCEPT_LANGUAGE="en",
        REMOTE_ADDR="10.0.0.2",
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert "sign-in link is on its way" in first.content.decode()
    assert "sign-in link is on its way" in second.content.decode()
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["anna@example.com"]
