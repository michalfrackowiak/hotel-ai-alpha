"""Demo seeding is complete and idempotent."""

from __future__ import annotations

import pytest
from django.core.management import call_command

from apps.agents.models import AgentSettings, AIProvider
from apps.calendar_sync.models import CalendarConnection, RoomCalendar
from apps.hotel.models import Booking, Client, PricingRule, Room, RoomType


@pytest.mark.django_db
def test_seed_demo_creates_the_expected_data():
    call_command("seed_demo")

    assert RoomType.objects.count() == 2
    assert Room.objects.count() == 5
    assert PricingRule.objects.count() == 3
    assert Client.objects.count() == 2
    assert AIProvider.objects.count() == 4
    assert AgentSettings.objects.count() == 1
    assert CalendarConnection.objects.count() == 1
    assert RoomCalendar.objects.count() == 5
    assert Booking.objects.count() == 1
    assert Booking.objects.get().status == Booking.Status.CONFIRMED


@pytest.mark.django_db
def test_seed_demo_is_idempotent():
    call_command("seed_demo")
    first = {
        "room_types": RoomType.objects.count(),
        "rooms": Room.objects.count(),
        "rules": PricingRule.objects.count(),
        "clients": Client.objects.count(),
        "providers": AIProvider.objects.count(),
        "settings": AgentSettings.objects.count(),
        "connections": CalendarConnection.objects.count(),
        "mappings": RoomCalendar.objects.count(),
        "bookings": Booking.objects.count(),
    }

    call_command("seed_demo")

    second = {
        "room_types": RoomType.objects.count(),
        "rooms": Room.objects.count(),
        "rules": PricingRule.objects.count(),
        "clients": Client.objects.count(),
        "providers": AIProvider.objects.count(),
        "settings": AgentSettings.objects.count(),
        "connections": CalendarConnection.objects.count(),
        "mappings": RoomCalendar.objects.count(),
        "bookings": Booking.objects.count(),
    }
    assert first == second


@pytest.mark.django_db
def test_seed_marks_one_provider_as_default():
    call_command("seed_demo")

    assert AIProvider.objects.filter(is_default=True).count() == 1
    assert AgentSettings.load().default_provider is not None
