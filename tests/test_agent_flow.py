"""End-to-end demo agent flow in English and Polish."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.agents.engine import AgentEngine, start_conversation
from apps.agents.models import Conversation
from apps.hotel.models import Booking, Room, RoomType
from apps.hotel.sms_providers import ConsoleSmsProvider


def _dates():
    start = timezone.localdate() + timedelta(days=30)
    return start, start + timedelta(days=3)


def _run(conversation: Conversation, message: str):
    return AgentEngine(conversation).handle_user_message(message)


def _start_stay(conversation: Conversation, check_in, check_out, guests: int = 2):
    """Share the whole stay in one message and return the offer turn."""
    return _run(
        conversation,
        f"I would like to book a room from {check_in.isoformat()} to {check_out.isoformat()} "
        f"for {guests} guests.",
    )


@pytest.mark.django_db
def test_demo_agent_collects_stay_then_books_and_emails(room_type, rooms):
    check_in, check_out = _dates()
    conversation = start_conversation(language="en", channel=Conversation.Channel.CHAT)

    offer = _start_stay(conversation, check_in, check_out)
    assert offer.error == ""
    assert any(call["name"] == "check_availability" and call["ok"] for call in offer.tool_calls)
    assert "Which option would you like to select?" in offer.content

    confirm = _run(conversation, "The Standard Double, please.")
    assert "Shall I confirm your reservation?" in confirm.content
    assert not Booking.objects.exists()

    delivery = _run(conversation, "Yes, please confirm.")
    assert "email address or by SMS" in delivery.content
    assert not Booking.objects.exists()

    contact = _run(conversation, "Email, please.")
    assert "email address" in contact.content.lower()

    done = _run(conversation, "Anna Kowalska, anna@example.com")
    assert any(
        call["name"] == "send_confirmation_email" and call["ok"] for call in done.tool_calls
    )
    assert "sent the details to anna@example.com" in done.content

    booking = Booking.objects.get()
    assert booking.status == Booking.Status.CONFIRMED
    assert booking.client.first_name == "Anna"
    assert booking.client.last_name == "Kowalska"
    assert booking.client.email == "anna@example.com"
    assert booking.confirmation_email_sent_at is not None
    assert booking.confirmation_email_status == "sent"


@pytest.mark.django_db
def test_demo_agent_sends_confirmation_sms(room_type, rooms):
    check_in, check_out = _dates()
    conversation = start_conversation(language="en", channel=Conversation.Channel.VOICE)

    _start_stay(conversation, check_in, check_out)
    _run(conversation, "The Standard Double, please.")
    _run(conversation, "Yes, please confirm.")
    ask_phone = _run(conversation, "SMS, please.")
    assert "phone number" in ask_phone.content.lower()

    done = _run(conversation, "Anna Kowalska, +48 510 378 422")
    assert any(
        call["name"] == "send_confirmation_sms" and call["ok"] for call in done.tool_calls
    )
    assert "confirmation text message to +48 510 378 422" in done.content

    booking = Booking.objects.get()
    assert booking.status == Booking.Status.CONFIRMED
    assert booking.client.phone == "+48 510 378 422"
    assert booking.confirmation_sms_sent_at is not None
    assert booking.confirmation_sms_status == "sent"

    assert len(ConsoleSmsProvider.outbox) == 1
    sms = ConsoleSmsProvider.outbox[0]
    assert sms.to == "+48 510 378 422"
    assert booking.reference in sms.body


@pytest.mark.django_db
def test_demo_agent_asks_for_departure_and_guest_count_in_order(room_type, rooms):
    check_in, check_out = _dates()
    conversation = start_conversation(language="en")

    arrival = _run(conversation, f"I would like to book a room. My arrival is {check_in.isoformat()}.")
    assert "departure date" in arrival.content.lower()

    departure = _run(conversation, f"My departure is {check_out.isoformat()}.")
    assert "total number of guests" in departure.content.lower()

    guests = _run(conversation, "2 guests")
    assert any(call["name"] == "check_availability" and call["ok"] for call in guests.tool_calls)
    assert "Which option would you like to select?" in guests.content


@pytest.mark.django_db
def test_demo_agent_books_deluxe_suite_by_name(room_type, rooms):
    """Picking a room by name (as a voice guest would) must not re-run search."""
    deluxe = RoomType.objects.create(
        slug="deluxe-suite",
        name="Deluxe Suite",
        capacity=4,
        base_price=Decimal("800.00"),
    )
    Room.objects.create(number="201", room_type=deluxe, floor=2)
    check_in, check_out = _dates()
    conversation = start_conversation(language="en", channel=Conversation.Channel.VOICE)

    offer = _start_stay(conversation, check_in, check_out)
    assert "Deluxe Suite" in offer.content

    second = _run(conversation, "I'm picking the Deluxe Suite")
    assert "Shall I confirm your reservation?" in second.content
    assert not any(call["name"] == "create_booking" for call in second.tool_calls)

    _run(conversation, "Yes, please confirm.")
    _run(conversation, "Email, please.")
    done = _run(conversation, "Anna Kowalska, anna@example.com")
    assert any(call["name"] == "create_booking" and call["ok"] for call in done.tool_calls)

    booking = Booking.objects.get()
    assert booking.room.room_type.slug == "deluxe-suite"
    assert booking.status == Booking.Status.CONFIRMED


@pytest.mark.django_db
def test_demo_agent_books_a_room_in_polish(room_type, rooms):
    conversation = start_conversation(language="pl")
    assert conversation.language == "pl"

    offer = _run(
        conversation,
        "Chcę zarezerwować pokój od 12 grudnia 2026 do 15 grudnia 2026 dla 2 gości.",
    )
    assert offer.error == ""
    assert any(call["name"] == "check_availability" for call in offer.tool_calls)
    assert "Którą opcję chciałbyś wybrać?" in offer.content

    confirm = _run(conversation, "Standard Double, proszę.")
    assert "Czy potwierdzić rezerwację?" in confirm.content

    delivery = _run(conversation, "Tak, potwierdzam.")
    assert "e-mail" in delivery.content.lower()
    assert "sms" in delivery.content.lower()

    _run(conversation, "E-mail, proszę.")
    done = _run(conversation, "Anna Kowalska, anna@example.com")
    assert any(call["name"] == "send_confirmation_email" and call["ok"] for call in done.tool_calls)
    assert "Wysłaliśmy szczegóły" in done.content

    booking = Booking.objects.get()
    assert booking.language == "pl"
    assert booking.status == Booking.Status.CONFIRMED


@pytest.mark.django_db
def test_demo_agent_switches_language_mid_conversation(room_type, rooms):
    conversation = start_conversation(language="pl")
    assert conversation.language == "pl"

    reply = _run(conversation, "English, please.")
    conversation.refresh_from_db()
    assert conversation.language == "en"
    assert "arrival date" in reply.content.lower()


@pytest.mark.django_db
def test_demo_agent_hands_over_non_booking_request(room_type, rooms):
    conversation = start_conversation(language="en")

    reply = _run(conversation, "I have a complaint about my invoice.")
    assert any(call["name"] == "transfer_to_human" and call["ok"] for call in reply.tool_calls)
    assert "Redirecting you to a reception employee" in reply.content

    conversation.refresh_from_db()
    assert conversation.state["handover_requested"]["reason"]


@pytest.mark.django_db
def test_demo_agent_asks_for_arrival_date_when_missing(room_type, rooms):
    conversation = start_conversation(language="en")
    turn = _run(conversation, "Hello, I would like a room.")

    assert turn.error == ""
    assert "arrival date" in turn.content.lower()


@pytest.mark.django_db
def test_conversation_history_excludes_tool_messages(room_type, rooms):
    from apps.agents.engine import conversation_history

    conversation = start_conversation(language="en")
    _run(conversation, "Hi")

    history = conversation_history(conversation)
    assert history
    assert {item["role"] for item in history} <= {"user", "assistant"}
