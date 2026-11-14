"""End-to-end demo agent flow in English and Polish."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.agents.engine import AgentEngine, start_conversation
from apps.agents.models import Conversation, Message
from apps.hotel.models import Booking, Room, RoomType


def _dates():
    start = timezone.localdate() + timedelta(days=30)
    return start, start + timedelta(days=3)


def _run(conversation: Conversation, message: str):
    return AgentEngine(conversation).handle_user_message(message)


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

    first = _run(
        conversation,
        f"Do you have a room for 2 guests from {check_in.isoformat()} to {check_out.isoformat()}?",
    )
    assert first.tool_calls and first.tool_calls[0]["name"] == "check_availability"

    second = _run(conversation, "I'm picking the deluxe suit")
    assert "full name" in second.content.lower() or "email" in second.content.lower()
    assert not any(call["name"] == "create_booking" for call in second.tool_calls)

    third = _run(conversation, "Anna Kowalska, anna@example.com")
    assert any(call["name"] == "create_booking" and call["ok"] for call in third.tool_calls)

    booking = Booking.objects.get()
    assert booking.room.room_type.slug == "deluxe-suite"

    fourth = _run(conversation, "Yes, please confirm.")
    assert fourth.booking_reference == booking.reference
    booking.refresh_from_db()
    assert booking.status == Booking.Status.CONFIRMED


@pytest.mark.django_db
def test_demo_agent_books_a_room_in_english(room_type, rooms):
    check_in, check_out = _dates()
    conversation = start_conversation(language="en")
    assert conversation.language == "en"
    assert conversation.messages.filter(role=Message.Role.ASSISTANT).exists()

    first = _run(
        conversation,
        f"Do you have a room for 2 guests from {check_in.isoformat()} to {check_out.isoformat()}?",
    )
    assert first.error == ""
    assert "Which room" in first.content or "offer" in first.content.lower()
    assert first.tool_calls[0]["name"] == "check_availability"

    second = _run(conversation, "My name is Anna Kowalska, anna@example.com")
    assert "confirm" in second.content.lower()
    assert any(call["name"] == "create_booking" and call["ok"] for call in second.tool_calls)

    booking = Booking.objects.get()
    assert booking.status == Booking.Status.PENDING
    assert booking.client.first_name == "Anna"

    third = _run(conversation, "Yes, please confirm.")
    assert "confirmed" in third.content.lower()
    assert third.booking_reference == booking.reference

    booking.refresh_from_db()
    assert booking.status == Booking.Status.CONFIRMED
    assert booking.source == Booking.Source.CHAT


@pytest.mark.django_db
def test_demo_agent_books_a_room_in_polish(room_type, rooms):
    conversation = start_conversation(language="pl")
    assert conversation.language == "pl"

    first = _run(
        conversation,
        "Czy macie pokój dla 2 gości od 12 grudnia 2026 do 15 grudnia 2026?",
    )
    assert first.error == ""
    assert any(call["name"] == "check_availability" for call in first.tool_calls)

    second = _run(conversation, "Nazywam się Anna Kowalska, anna@example.com")
    assert "potwierdz" in second.content.lower()
    assert any(call["name"] == "create_booking" and call["ok"] for call in second.tool_calls)

    third = _run(conversation, "Tak, potwierdzam.")
    assert "potwierdzona" in third.content.lower()
    assert third.booking_reference

    booking = Booking.objects.get()
    assert booking.language == "pl"
    assert booking.status == Booking.Status.CONFIRMED


@pytest.mark.django_db
def test_bare_name_reply_is_understood(room_type, rooms):
    check_in, check_out = _dates()
    conversation = start_conversation(language="en")

    _run(
        conversation,
        f"I need a room for 2 guests from {check_in.isoformat()} to {check_out.isoformat()}.",
    )
    reply = _run(conversation, "Anna Kowalska, anna@example.com")

    assert any(call["name"] == "create_booking" and call["ok"] for call in reply.tool_calls)
    booking = Booking.objects.get()
    assert booking.client.first_name == "Anna"
    assert booking.client.last_name == "Kowalska"


@pytest.mark.django_db
def test_demo_agent_asks_for_dates_when_missing(room_type, rooms):
    conversation = start_conversation(language="en")
    turn = _run(conversation, "Hello, I would like a room.")

    assert turn.error == ""
    assert "dates" in turn.content.lower()


@pytest.mark.django_db
def test_conversation_history_excludes_tool_messages(room_type, rooms):
    from apps.agents.engine import conversation_history

    conversation = start_conversation(language="en")
    _run(conversation, "Hi")

    history = conversation_history(conversation)
    assert history
    assert {item["role"] for item in history} <= {"user", "assistant"}
