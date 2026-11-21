"""Verified guest account linkage to existing and future bookings/clients."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client
from django.utils import timezone

from apps.hotel.booking_service import create_booking
from apps.hotel.guest_access import (
    consume_email_verification_token,
    issue_email_verification_token,
    link_clients_for_guest_account,
)
from apps.hotel.models import Client as GuestClient
from apps.hotel.models import GuestAccount

User = get_user_model()


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.mark.django_db
def test_verifying_email_links_existing_clients(room_type, rooms, stay_dates):
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
        is_active=False,
        is_staff=False,
    )
    GuestAccount.objects.get_or_create(user=user)
    _record, raw = issue_email_verification_token(user)

    verified_email = consume_email_verification_token(raw)
    assert verified_email == "anna@example.com"

    booking.client.refresh_from_db()
    account = GuestAccount.objects.get(user=user)
    assert booking.client.account_id == account.id
    assert account.is_email_verified is True


@pytest.mark.django_db
def test_future_booking_links_to_verified_guest_account(room_type, rooms, stay_dates):
    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    account, _ = GuestAccount.objects.get_or_create(user=user)
    account.email_verified_at = timezone.now()
    account.save(update_fields=["email_verified_at", "updated_at"])

    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="ANNA@example.com",
        adults=2,
    )

    assert booking.client.account_id == account.id


@pytest.mark.django_db
def test_verified_authenticated_guest_sees_and_can_open_linked_booking(client, room_type, rooms, stay_dates):
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
    # simulate pre-existing unlinked booking, then verify/link
    booking.client.account = None
    booking.client.save(update_fields=["account", "updated_at"])

    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=False,
        is_staff=False,
    )
    GuestAccount.objects.get_or_create(user=user)
    _record, raw = issue_email_verification_token(user)
    consume_email_verification_token(raw)

    user.refresh_from_db()
    assert client.login(username="anna@example.com", password="correct horse battery")

    listing = client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert listing.status_code == 200
    assert booking.reference in listing.content.decode()

    detail = client.get(f"/booking/{booking.reference}/", HTTP_ACCEPT_LANGUAGE="en")
    assert detail.status_code == 200
    assert booking.reference in detail.content.decode()


@pytest.mark.django_db
def test_linking_rejects_email_owned_by_different_user():
    # existing client linked to account A and its user already owns the email
    user_a = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    account_a, _ = GuestAccount.objects.get_or_create(user=user_a)
    account_a.email_verified_at = timezone.now()
    account_a.save(update_fields=["email_verified_at", "updated_at"])

    existing = GuestClient.objects.create(
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        phone="+48111222333",
        account=account_a,
    )

    # another account must not be able to verify/claim an email owned by user A
    user_b = User.objects.create_user(
        username="b@example.com",
        email="b@example.com",
        password="correct horse battery",
        is_active=False,
        is_staff=False,
    )
    account_b, _ = GuestAccount.objects.get_or_create(user=user_b)
    _record, raw = issue_email_verification_token(user_b, email="anna@example.com")
    verified_email = consume_email_verification_token(raw)

    assert verified_email is None
    existing.refresh_from_db()
    assert existing.account_id == account_a.id
    account_b.refresh_from_db()
    assert account_b.is_email_verified is False
    user_b.refresh_from_db()
    assert user_b.email == "b@example.com"


@pytest.mark.django_db
def test_pre_account_booking_becomes_visible_after_register_verify_and_magic_link_login(
    client, room_type, rooms, stay_dates, settings
):
    """A pre-account booking is linked on verification and visible via magic-link sign-in."""
    settings.BOOKING_EMAILS_ENABLED = True
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
        is_active=False,
        is_staff=False,
    )
    GuestAccount.objects.get_or_create(user=user)
    _record, raw = issue_email_verification_token(user)
    assert consume_email_verification_token(raw) == "anna@example.com"

    booking.client.refresh_from_db()
    assert booking.client.account_id is not None

    response = client.post("/my-bookings/", {"email": "anna@example.com"}, HTTP_ACCEPT_LANGUAGE="en")
    assert response.status_code == 200
    assert len(mail.outbox) == 1
    match = __import__("re").search(r"/my-bookings/login/[^\s]+", mail.outbox[0].body)
    assert match, mail.outbox[0].body

    login = client.get(match.group(0))
    assert login.status_code == 302

    listing = client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert listing.status_code == 200
    assert booking.reference in listing.content.decode()

    detail = client.get(f"/booking/{booking.reference}/", HTTP_ACCEPT_LANGUAGE="en")
    assert detail.status_code == 200
    assert booking.reference in detail.content.decode()



@pytest.mark.django_db
def test_link_clients_for_guest_account_does_not_override_different_account():
    """Safety: matching clients already linked to another account must not be stolen."""
    user_a = User.objects.create_user(
        username="a@example.com",
        email="a@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    account_a, _ = GuestAccount.objects.get_or_create(user=user_a)
    account_a.email_verified_at = timezone.now()
    account_a.save(update_fields=["email_verified_at", "updated_at"])

    shared_client = GuestClient.objects.create(
        first_name="Anna",
        last_name="Kowalska",
        email="shared@example.com",
        phone="+48111222333",
        account=account_a,
    )

    # Account B is verified but its contact resolves to the same client email in this
    # data-integrity edge case; linking must not override account A.
    user_b = User.objects.create_user(
        username="b@example.com",
        email="shared@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    account_b, _ = GuestAccount.objects.get_or_create(user=user_b)
    account_b.email_verified_at = timezone.now()
    account_b.save(update_fields=["email_verified_at", "updated_at"])

    linked = link_clients_for_guest_account(account_b)

    assert linked == 0
    shared_client.refresh_from_db()
    assert shared_client.account_id == account_a.id



@pytest.mark.django_db
def test_future_booking_after_verification_is_visible_to_authenticated_guest(client, room_type, rooms, stay_dates):
    """Bookings created after account verification are linked and visible in My bookings."""
    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    account, _ = GuestAccount.objects.get_or_create(user=user)
    account.email_verified_at = timezone.now()
    account.save(update_fields=["email_verified_at", "updated_at"])

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
    assert booking.client.account_id == account.id

    assert client.login(username="anna@example.com", password="correct horse battery")

    listing = client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert listing.status_code == 200
    assert booking.reference in listing.content.decode()

    detail = client.get(f"/booking/{booking.reference}/", HTTP_ACCEPT_LANGUAGE="en")
    assert detail.status_code == 200
    assert booking.reference in detail.content.decode()

@pytest.mark.django_db
def test_phone_only_client_is_linked_when_later_booking_provides_verified_email(
    client, room_type, rooms, stay_dates
):
    """A phone-only client/booking becomes account-linked when a later booking provides
    the same phone and a verified account email."""
    from datetime import timedelta

    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    account, _ = GuestAccount.objects.get_or_create(user=user)
    account.email_verified_at = timezone.now()
    account.save(update_fields=["email_verified_at", "updated_at"])

    check_in, check_out = stay_dates
    phone_only = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="",
        phone="+48 501 222 333",
        adults=1,
    )
    phone_only.client.refresh_from_db()
    assert phone_only.client.account_id is None
    assert phone_only.client.email == ""

    # Later booking with same phone provides the verified account email.
    later = create_booking(
        room_type=room_type,
        check_in=check_in + timedelta(days=30),
        check_out=check_out + timedelta(days=30),
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        phone="+48 501 222 333",
        adults=1,
    )
    phone_only.client.refresh_from_db()
    later.client.refresh_from_db()
    assert later.client.pk == phone_only.client.pk
    assert phone_only.client.account_id == account.id
    assert phone_only.client.email == "anna@example.com"

    assert client.login(username="anna@example.com", password="correct horse battery")
    listing = client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert listing.status_code == 200
    content = listing.content.decode()
    assert phone_only.reference in content
    assert later.reference in content

@pytest.mark.django_db
def test_phone_only_booking_linked_to_verified_account_is_visible_and_manageable(
    client, room_type, rooms, stay_dates
):
    """A phone-only booking linked to a verified account is visible in My bookings
    and accessible via the authenticated guest session."""
    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    account, _ = GuestAccount.objects.get_or_create(user=user)
    account.email_verified_at = timezone.now()
    account.save(update_fields=["email_verified_at", "updated_at"])

    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="",
        phone="+48 501 222 333",
        adults=1,
    )
    # Link the phone-only client to the verified account explicitly.
    booking.client.account = account
    booking.client.save(update_fields=["account", "updated_at"])

    assert client.login(username="anna@example.com", password="correct horse battery")

    listing = client.get("/my-bookings/", HTTP_ACCEPT_LANGUAGE="en")
    assert listing.status_code == 200
    assert booking.reference in listing.content.decode()

    detail = client.get(f"/booking/{booking.reference}/", HTTP_ACCEPT_LANGUAGE="en")
    assert detail.status_code == 200
    assert booking.reference in detail.content.decode()

    # Reference+phone fallback remains available.
    client.logout()
    lookup = client.get(
        "/booking/",
        {"reference": booking.reference, "contact": "+48 501 222 333"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert lookup.status_code == 302
    assert "token=" in lookup["Location"]

@pytest.mark.django_db
def test_link_clients_for_guest_account_links_unlinked_clients_with_preserved_account_associated_email(
    client, room_type, rooms, stay_dates
):
    """Unlinked clients whose email is already associated with the verified account
    through another linked client are linked, without overriding different-account links."""
    from datetime import timedelta

    from apps.hotel.models import Booking, Client

    user = User.objects.create_user(
        username="anna@example.com",
        email="anna@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    account, _ = GuestAccount.objects.get_or_create(user=user)
    account.email_verified_at = timezone.now()
    account.save(update_fields=["email_verified_at", "updated_at"])

    check_in, check_out = stay_dates
    linked_booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="old@example.com",
        adults=1,
    )
    linked_booking.client.account = account
    linked_booking.client.save(update_fields=["account", "updated_at"])

    unlinked_client = Client.objects.create(
        first_name="Anna",
        last_name="Kowalska",
        email="old@example.com",
        phone="+48555666777",
    )
    unlinked_booking = Booking.objects.create(
        client=unlinked_client,
        room=rooms[1],
        check_in=check_in + timedelta(days=30),
        check_out=check_out + timedelta(days=30),
        adults=1,
        children=0,
        status=Booking.Status.CONFIRMED,
        source=Booking.Source.WEB,
    )

    other_user = User.objects.create_user(
        username="other@example.com",
        email="other@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    other_account, _ = GuestAccount.objects.get_or_create(user=other_user)
    other_account.email_verified_at = timezone.now()
    other_account.save(update_fields=["email_verified_at", "updated_at"])
    different_account_client = Client.objects.create(
        first_name="Other",
        last_name="Person",
        email="old@example.com",
        phone="+48111222333",
        account=other_account,
    )

    linked = link_clients_for_guest_account(account)

    assert linked >= 1
    unlinked_client.refresh_from_db()
    different_account_client.refresh_from_db()
    assert unlinked_client.account_id == account.id
    assert different_account_client.account_id == other_account.id
    assert unlinked_booking.client_id == unlinked_client.id


@pytest.mark.django_db
def test_email_has_bookings_and_magic_link_issuance_for_account_associated_unlinked_booking(
    room_type, rooms, stay_dates
):
    from datetime import timedelta

    from apps.hotel.guest_access import email_has_bookings, issue_login_token
    from apps.hotel.models import Booking, Client

    account_email = "account@example.com"
    associated_email = "old-client@example.com"
    user = User.objects.create_user(
        username=account_email,
        email=account_email,
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    account, _ = GuestAccount.objects.get_or_create(user=user)
    account.email_verified_at = timezone.now()
    account.save(update_fields=["email_verified_at", "updated_at"])

    # Linked client carries an associated email different from account email.
    Client.objects.create(
        first_name="Anna",
        last_name="Kowalska",
        email=associated_email,
        phone="+48555666777",
        account=account,
    )

    # Booking exists under that associated email, but client is unlinked and
    # no booking is directly on the account email/account.
    check_in, check_out = stay_dates
    unlinked_client = Client.objects.create(
        first_name="Anna",
        last_name="Kowalska",
        email=associated_email,
        phone="+48555666777",
    )
    Booking.objects.create(
        client=unlinked_client,
        room=rooms[0],
        check_in=check_in + timedelta(days=45),
        check_out=check_out + timedelta(days=45),
        adults=1,
        children=0,
        status=Booking.Status.CONFIRMED,
        source=Booking.Source.WEB,
    )

    assert email_has_bookings(account_email) is True
    issued = issue_login_token(account_email)
    assert issued is not None
    _record, raw_token = issued
    assert raw_token


@pytest.mark.django_db
def test_find_or_create_client_does_not_overwrite_email_for_different_verified_account(
    room_type, rooms, stay_dates
):
    from apps.hotel.models import Client

    owner_user = User.objects.create_user(
        username="owner@example.com",
        email="owner@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    owner_account, _ = GuestAccount.objects.get_or_create(user=owner_user)
    owner_account.email_verified_at = timezone.now()
    owner_account.save(update_fields=["email_verified_at", "updated_at"])

    other_user = User.objects.create_user(
        username="other@example.com",
        email="other@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    other_account, _ = GuestAccount.objects.get_or_create(user=other_user)
    other_account.email_verified_at = timezone.now()
    other_account.save(update_fields=["email_verified_at", "updated_at"])

    client_record = Client.objects.create(
        first_name="Anna",
        last_name="Kowalska",
        email="owner-old@example.com",
        phone="+48555111222",
        account=owner_account,
    )

    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="other@example.com",
        phone="+48555111222",
        adults=1,
    )

    client_record.refresh_from_db()
    assert booking.client_id == client_record.id
    assert client_record.account_id == owner_account.id
    assert client_record.email == "owner-old@example.com"


@pytest.mark.django_db
def test_find_or_create_client_does_not_overwrite_unlinked_client_email_with_other_verified_account(
    room_type, rooms, stay_dates
):
    from apps.hotel.models import Client

    other_user = User.objects.create_user(
        username="other@example.com",
        email="other@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    other_account, _ = GuestAccount.objects.get_or_create(user=other_user)
    other_account.email_verified_at = timezone.now()
    other_account.save(update_fields=["email_verified_at", "updated_at"])

    client_record = Client.objects.create(
        first_name="Anna",
        last_name="Kowalska",
        email="legacy@example.com",
        phone="+48555111999",
    )

    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="other@example.com",
        phone="+48555111999",
        adults=1,
    )

    client_record.refresh_from_db()
    assert booking.client_id == client_record.id
    assert client_record.account_id is None
    assert client_record.email == "legacy@example.com"


@pytest.mark.django_db
def test_bookings_for_email_includes_account_current_email_when_queried_by_associated_email(
    room_type, rooms, stay_dates
):
    from apps.hotel.guest_access import bookings_for_email, email_has_bookings
    from apps.hotel.models import Client

    account_user = User.objects.create_user(
        username="new@example.com",
        email="new@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    account, _ = GuestAccount.objects.get_or_create(user=account_user)
    account.email_verified_at = timezone.now()
    account.save(update_fields=["email_verified_at", "updated_at"])

    # Linked client with preserved historical email (associated with account).
    Client.objects.create(
        first_name="Anna",
        last_name="Kowalska",
        email="old@example.com",
        phone="+48555111001",
        account=account,
    )

    # Unlinked booking/client whose email matches account's current email.
    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="new@example.com",
        phone="+48555111002",
        adults=1,
    )
    # Ensure it stays unlinked for this scenario (create_booking may link it; force unlinked).
    booking.client.account = None
    booking.client.save(update_fields=["account", "updated_at"])

    assert email_has_bookings("old@example.com") is True
    refs = {b.reference for b in bookings_for_email("old@example.com")}
    assert booking.reference in refs


@pytest.mark.django_db
def test_bookings_for_email_excludes_bookings_linked_to_different_verified_account(
    room_type, rooms, stay_dates
):
    from django.test import RequestFactory

    from apps.hotel.guest_access import bookings_for_email, guest_session_matches

    owner_user = User.objects.create_user(
        username="owner@example.com",
        email="owner@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    owner_account, _ = GuestAccount.objects.get_or_create(user=owner_user)
    owner_account.email_verified_at = timezone.now()
    owner_account.save(update_fields=["email_verified_at", "updated_at"])

    other_user = User.objects.create_user(
        username="other@example.com",
        email="other@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    other_account, _ = GuestAccount.objects.get_or_create(user=other_user)
    other_account.email_verified_at = timezone.now()
    other_account.save(update_fields=["email_verified_at", "updated_at"])

    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Owner",
        last_name="User",
        email="owner@example.com",
        phone="+48555111003",
        adults=1,
    )
    booking.client.account = owner_account
    booking.client.save(update_fields=["account", "updated_at"])

    # Another verified account with same email in a linked client must not see owner's booking.
    GuestClient.objects.create(
        first_name="Other",
        last_name="User",
        email="owner@example.com",
        phone="+48555111004",
        account=other_account,
    )

    refs = {b.reference for b in bookings_for_email("other@example.com")}
    assert booking.reference not in refs

    rf = RequestFactory()
    request = rf.get("/")
    request.user = other_user
    request.session = {}
    assert guest_session_matches(request, booking) is False

    # Magic-link session for other verified account must also be denied.
    from django.contrib.auth.models import AnonymousUser

    from apps.hotel.guest_access import GUEST_SESSION_KEY

    anon_request = rf.get("/")
    anon_request.user = AnonymousUser()
    anon_request.session = {GUEST_SESSION_KEY: "other@example.com"}
    assert guest_session_matches(anon_request, booking) is False


@pytest.mark.django_db
def test_bookings_for_email_skips_unlinked_email_owned_by_different_verified_account(
    room_type, rooms, stay_dates
):
    from apps.hotel.guest_access import bookings_for_email

    owner_user = User.objects.create_user(
        username="owner2@example.com",
        email="owner2@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    owner_account, _ = GuestAccount.objects.get_or_create(user=owner_user)
    owner_account.email_verified_at = timezone.now()
    owner_account.save(update_fields=["email_verified_at", "updated_at"])

    other_user = User.objects.create_user(
        username="shared2@example.com",
        email="shared2@example.com",
        password="correct horse battery",
        is_active=True,
        is_staff=False,
    )
    other_account, _ = GuestAccount.objects.get_or_create(user=other_user)
    other_account.email_verified_at = timezone.now()
    other_account.save(update_fields=["email_verified_at", "updated_at"])

    # owner_account has shared2@example.com as a linked-client email (conflict state),
    # but the verified owner of shared2@example.com is other_account (user email).
    GuestClient.objects.create(
        first_name="Owner",
        last_name="Linked",
        email="shared2@example.com",
        phone="+48555112001",
        account=owner_account,
    )

    check_in, check_out = stay_dates
    booking = create_booking(
        room_type=room_type,
        check_in=check_in,
        check_out=check_out,
        first_name="Other",
        last_name="User",
        email="shared2@example.com",
        phone="+48555112002",
        adults=1,
    )
    # Unlinked booking; its client email is owned by other_account.
    booking.client.account = None
    booking.client.save(update_fields=["account", "updated_at"])

    refs = {b.reference for b in bookings_for_email("owner2@example.com")}
    assert booking.reference not in refs
