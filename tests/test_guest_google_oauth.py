"""Google OAuth guest sign-in/linking flows."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.hotel.booking_service import create_booking
from apps.hotel.guest_access import (
    consume_email_verification_token,
    issue_email_verification_token,
    link_clients_for_guest_account,
)
from apps.hotel.models import GuestAccount

User = get_user_model()


@pytest.fixture(autouse=True)
def clear_throttle_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def client() -> Client:
    return Client()


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


class _FakeHttpxClient:
    def __init__(self, token_payload, userinfo_payload):
        self.token_payload = token_payload
        self.userinfo_payload = userinfo_payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url, data=None):
        assert "oauth2.googleapis.com/token" in url
        assert data["code"] == "good-code"
        return _FakeResponse(self.token_payload)

    def get(self, url, headers=None):
        assert "openidconnect.googleapis.com/v1/userinfo" in url
        assert headers["Authorization"] == "Bearer access-token"
        return _FakeResponse(self.userinfo_payload)


@pytest.mark.django_db
def test_google_login_redirects_and_sets_state(client, settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = "client-id"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "client-secret"
    settings.HOTEL["public_url"] = "http://testserver"

    response = client.get("/my-bookings/google/")

    assert response.status_code == 302
    assert "accounts.google.com/o/oauth2/v2/auth" in response["Location"]
    assert "client_id=client-id" in response["Location"]
    assert "state=" in response["Location"]
    assert "redirect_uri=http%3A%2F%2Ftestserver%2Fmy-bookings%2Fgoogle%2Fcallback%2F" in response["Location"]
    assert client.session.get("google_oauth_state")


@pytest.mark.django_db
def test_google_login_not_configured_shows_error(client, settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = ""
    settings.GOOGLE_OAUTH_CLIENT_SECRET = ""

    response = client.get("/my-bookings/google/", HTTP_ACCEPT_LANGUAGE="en")

    assert response.status_code == 400
    assert "not configured" in response.content.decode().lower()


@pytest.mark.django_db
def test_google_callback_rejects_invalid_state(client, settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = "client-id"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "client-secret"
    session = client.session
    session["google_oauth_state"] = "expected"
    session.save()

    response = client.get("/my-bookings/google/callback/?state=wrong&code=x", HTTP_ACCEPT_LANGUAGE="en")

    assert response.status_code == 400
    assert "could not be verified" in response.content.decode().lower()


@pytest.mark.django_db
def test_google_callback_creates_verified_guest_and_logs_in(client, settings, monkeypatch, room_type, rooms, stay_dates):
    settings.GOOGLE_OAUTH_CLIENT_ID = "client-id"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "client-secret"
    settings.HOTEL["public_url"] = "http://testserver"
    session = client.session
    session["google_oauth_state"] = "good-state"
    session.save()

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
    assert booking.client.account_id is None

    fake = _FakeHttpxClient(
        token_payload={"access_token": "access-token"},
        userinfo_payload={
            "email": "anna@example.com",
            "email_verified": True,
            "given_name": "Anna",
            "family_name": "Kowalska",
        },
    )
    monkeypatch.setattr("apps.web.views.httpx.Client", lambda timeout=10.0: fake)

    response = client.get("/my-bookings/google/callback/?state=good-state&code=good-code")

    assert response.status_code == 302
    assert response["Location"].endswith("/my-bookings/")

    user = User.objects.get(email="anna@example.com")
    assert user.is_active is True
    assert user.is_staff is False
    account = GuestAccount.objects.get(user=user)
    assert account.is_email_verified is True

    booking.client.refresh_from_db()
    assert booking.client.account_id == account.id

    listing = client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert listing.status_code == 200
    assert booking.reference in listing.content.decode()


@pytest.mark.django_db
def test_google_callback_rejects_unverified_email(client, settings, monkeypatch):
    settings.GOOGLE_OAUTH_CLIENT_ID = "client-id"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "client-secret"
    session = client.session
    session["google_oauth_state"] = "good-state"
    session.save()

    fake = _FakeHttpxClient(
        token_payload={"access_token": "access-token"},
        userinfo_payload={"email": "anna@example.com", "email_verified": False},
    )
    monkeypatch.setattr("apps.web.views.httpx.Client", lambda timeout=10.0: fake)

    response = client.get(
        "/my-bookings/google/callback/?state=good-state&code=good-code",
        HTTP_ACCEPT_LANGUAGE="en",
    )

    assert response.status_code == 400
    assert "verified email" in response.content.decode().lower()
    assert not User.objects.filter(email="anna@example.com").exists()


@pytest.mark.django_db
def test_google_callback_rejects_staff_account(client, settings, monkeypatch):
    settings.GOOGLE_OAUTH_CLIENT_ID = "client-id"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "client-secret"
    session = client.session
    session["google_oauth_state"] = "good-state"
    session.save()

    User.objects.create_user(
        username="admin@example.com",
        email="admin@example.com",
        password="x",
        is_active=True,
        is_staff=True,
    )

    fake = _FakeHttpxClient(
        token_payload={"access_token": "access-token"},
        userinfo_payload={"email": "admin@example.com", "email_verified": True},
    )
    monkeypatch.setattr("apps.web.views.httpx.Client", lambda timeout=10.0: fake)

    response = client.get(
        "/my-bookings/google/callback/?state=good-state&code=good-code",
        HTTP_ACCEPT_LANGUAGE="en",
    )

    assert response.status_code == 400
    assert "could not be completed" in response.content.decode().lower()
    assert "cannot be used" not in response.content.decode().lower()
    assert not client.session.get("_auth_user_id")


@pytest.mark.django_db
def test_google_callback_activates_and_links_existing_unverified_guest(
    client, settings, monkeypatch, room_type, rooms, stay_dates
):
    settings.GOOGLE_OAUTH_CLIENT_ID = "client-id"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "client-secret"
    settings.HOTEL["public_url"] = "http://testserver"
    session = client.session
    session["google_oauth_state"] = "good-state"
    session.save()

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
    assert booking.client.account_id is None

    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        first_name="",
        last_name="",
        is_active=False,
        is_staff=False,
    )
    GuestAccount.objects.get_or_create(user=user)

    fake = _FakeHttpxClient(
        token_payload={"access_token": "access-token"},
        userinfo_payload={
            "email": "anna@example.com",
            "email_verified": True,
            "given_name": "Anna",
            "family_name": "Kowalska",
        },
    )
    monkeypatch.setattr("apps.web.views.httpx.Client", lambda timeout=10.0: fake)

    response = client.get("/my-bookings/google/callback/?state=good-state&code=good-code")

    assert response.status_code == 302
    assert response["Location"].endswith("/my-bookings/")
    user.refresh_from_db()
    assert user.is_active is True
    assert user.first_name == "Anna"
    assert user.last_name == "Kowalska"
    account = GuestAccount.objects.get(user=user)
    assert account.is_email_verified is True
    assert account.email_verified_at is not None
    assert account.email_verified_at <= timezone.now()

    booking.client.refresh_from_db()
    assert booking.client.account_id == account.id

@pytest.mark.django_db
def test_google_login_is_rate_limited_per_ip(client, settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = "client-id"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "client-secret"
    settings.HOTEL["public_url"] = "http://testserver"

    first = client.get("/my-bookings/google/", REMOTE_ADDR="10.0.0.5")
    second = client.get("/my-bookings/google/", REMOTE_ADDR="10.0.0.5", HTTP_ACCEPT_LANGUAGE="en")

    assert first.status_code == 302
    assert "accounts.google.com/o/oauth2/v2/auth" in first["Location"]
    assert second.status_code == 400
    assert "could not be completed" in second.content.decode().lower()


@pytest.mark.django_db
def test_google_callback_is_rate_limited_per_ip(client, settings, monkeypatch):
    settings.GOOGLE_OAUTH_CLIENT_ID = "client-id"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "client-secret"
    session = client.session
    session["google_oauth_state"] = "good-state"
    session.save()

    called = {"count": 0}

    def _unexpected_httpx(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("external exchange should not run when throttled")

    monkeypatch.setattr("apps.web.views.httpx.Client", _unexpected_httpx)

    first = client.get(
        "/my-bookings/google/callback/?state=wrong&code=x",
        HTTP_ACCEPT_LANGUAGE="en",
        REMOTE_ADDR="10.0.0.6",
    )
    # First request consumed IP cooldown but failed on state (no external call).
    assert first.status_code == 400
    assert called["count"] == 0

    second = client.get(
        "/my-bookings/google/callback/?state=good-state&code=good-code",
        HTTP_ACCEPT_LANGUAGE="en",
        REMOTE_ADDR="10.0.0.6",
    )
    assert second.status_code == 400
    assert "could not be completed" in second.content.decode().lower()
    assert called["count"] == 0

@pytest.mark.django_db
def test_google_callback_logs_in_existing_verified_guest_and_keeps_bookings_accessible(
    client, settings, monkeypatch, room_type, rooms, stay_dates
):
    settings.GOOGLE_OAUTH_CLIENT_ID = "client-id"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "client-secret"
    settings.HOTEL["public_url"] = "http://testserver"
    session = client.session
    session["google_oauth_state"] = "good-state"
    session.save()

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

    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        first_name="Anna",
        last_name="Kowalska",
        is_active=True,
        is_staff=False,
    )
    account, _ = GuestAccount.objects.get_or_create(user=user)
    account.email_verified_at = timezone.now()
    account.save(update_fields=["email_verified_at", "updated_at"])
    booking.client.account = account
    booking.client.save(update_fields=["account", "updated_at"])

    fake = _FakeHttpxClient(
        token_payload={"access_token": "access-token"},
        userinfo_payload={
            "email": "anna@example.com",
            "email_verified": True,
            "given_name": "Anna",
            "family_name": "Kowalska",
        },
    )
    monkeypatch.setattr("apps.web.views.httpx.Client", lambda timeout=10.0: fake)

    response = client.get("/my-bookings/google/callback/?state=good-state&code=good-code")

    assert response.status_code == 302
    assert response["Location"].endswith("/my-bookings/")
    user.refresh_from_db()
    account.refresh_from_db()
    assert user.is_active is True
    assert user.is_staff is False
    assert account.is_email_verified is True
    assert User.objects.filter(email="anna@example.com").count() == 1

    listing = client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert listing.status_code == 200
    assert booking.reference in listing.content.decode()

    detail = client.get(f"/booking/{booking.reference}/", HTTP_ACCEPT_LANGUAGE="en")
    assert detail.status_code == 200
    assert booking.reference in detail.content.decode()


@pytest.mark.django_db
def test_google_callback_rejects_preserved_historical_client_email_linked_to_other_verified_account(
    settings, monkeypatch, room_type, rooms, stay_dates
):
    settings.GOOGLE_OAUTH_CLIENT_ID = "client-id"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "client-secret"
    settings.HOTEL["public_url"] = "http://testserver"

    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="old@example.com",
        adults=2,
    )

    owner = User.objects.create_user(
        username="old@example.com",
        email="old@example.com",
        password="pw",
        first_name="Anna",
        last_name="Kowalska",
        is_active=True,
    )
    owner_account, _ = GuestAccount.objects.get_or_create(user=owner)
    owner_account.email_verified_at = timezone.now()
    owner_account.save(update_fields=["email_verified_at", "updated_at"])
    link_clients_for_guest_account(owner_account)
    _rec, raw = issue_email_verification_token(owner, email="new@example.com")
    assert consume_email_verification_token(raw) == "new@example.com"
    owner.refresh_from_db()
    booking.client.refresh_from_db()
    assert owner.email == "new@example.com"
    assert booking.client.email == "old@example.com"
    assert booking.client.account_id == owner_account.id

    client = Client()
    session = client.session
    session["google_oauth_state"] = "good-state"
    session.save()

    fake = _FakeHttpxClient(
        token_payload={"access_token": "access-token"},
        userinfo_payload={
            "email": "old@example.com",
            "email_verified": True,
            "given_name": "Attacker",
            "family_name": "User",
        },
    )
    monkeypatch.setattr("apps.web.views.httpx.Client", lambda timeout=10.0: fake)

    response = client.get("/my-bookings/google/callback/?state=good-state&code=good-code", HTTP_ACCEPT_LANGUAGE="en")
    assert response.status_code == 400
    assert User.objects.filter(email__iexact="old@example.com").count() == 0
    booking.client.refresh_from_db()
    assert booking.client.account_id == owner_account.id
    # No guest session should be established.
    assert client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en").status_code == 200
    assert booking.reference not in client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en").content.decode()

@pytest.mark.django_db
def test_google_callback_rejects_conflicting_email_when_unverified_user_exists(
    settings, monkeypatch, room_type, rooms, stay_dates
):
    settings.GOOGLE_OAUTH_CLIENT_ID = "client-id"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "client-secret"
    settings.HOTEL["public_url"] = "http://testserver"

    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="old@example.com",
        adults=2,
    )

    owner = User.objects.create_user(
        username="old-owner@example.com",
        email="old@example.com",
        password="pw",
        is_active=True,
    )
    owner_account, _ = GuestAccount.objects.get_or_create(user=owner)
    owner_account.email_verified_at = timezone.now()
    owner_account.save(update_fields=["email_verified_at", "updated_at"])
    link_clients_for_guest_account(owner_account)
    _rec, raw = issue_email_verification_token(owner, email="new@example.com")
    assert consume_email_verification_token(raw) == "new@example.com"
    owner.refresh_from_db()
    booking.client.refresh_from_db()
    assert owner.email == "new@example.com"
    assert booking.client.account_id == owner_account.id

    unverified = User.objects.create_user(
        username="old@example.com",
        email="old@example.com",
        password="pw2",
        is_active=False,
    )
    unverified_account, _ = GuestAccount.objects.get_or_create(user=unverified)

    client = Client()
    session = client.session
    session["google_oauth_state"] = "good-state"
    session.save()

    fake = _FakeHttpxClient(
        token_payload={"access_token": "access-token"},
        userinfo_payload={
            "email": "old@example.com",
            "email_verified": True,
            "given_name": "Attacker",
            "family_name": "User",
        },
    )
    monkeypatch.setattr("apps.web.views.httpx.Client", lambda timeout=10.0: fake)

    response = client.get("/my-bookings/google/callback/?state=good-state&code=good-code", HTTP_ACCEPT_LANGUAGE="en")
    assert response.status_code == 400

    unverified.refresh_from_db()
    unverified_account.refresh_from_db()
    booking.client.refresh_from_db()
    assert unverified.is_active is False
    assert unverified_account.is_email_verified is False
    assert booking.client.account_id == owner_account.id
