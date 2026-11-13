"""Idempotent demo data: two room types, rooms, prices, providers and settings."""

from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.hotel.booking_service import confirm_booking, create_booking
from apps.hotel.models import Booking, Client, PricingRule, Room, RoomType


class Command(BaseCommand):
    help = "Seed demo hotel data. Safe to run repeatedly."

    @transaction.atomic
    def handle(self, *args, **options):
        room_types = self._seed_room_types()
        self._seed_rooms(room_types)
        self._seed_pricing()
        self._seed_clients()
        provider = self._seed_providers()
        self._seed_agent_settings(provider)
        self._seed_calendar()
        self._seed_sample_booking()

        self.stdout.write(self.style.SUCCESS("Demo data ready."))

    def _seed_room_types(self) -> dict[str, RoomType]:
        data = [
            {
                "slug": "standard-double",
                "name": "Standard Double",
                "short_description": "Comfortable double room with a queen bed and city or garden view.",
                "description": (
                    "Our Standard Double rooms are a calm base for a city break: a queen bed, "
                    "a rain shower, fast Wi-Fi and a Nespresso machine. Breakfast can be added "
                    "at reception."
                ),
                "capacity": 2,
                "base_price": Decimal("420.00"),
                "currency": "PLN",
                "size_sqm": 22,
                "bed_configuration": "Queen bed",
                "amenities": [
                    "Free Wi-Fi",
                    "Air conditioning",
                    "Smart TV",
                    "Nespresso machine",
                    "Rain shower",
                    "Work desk",
                ],
                "sort_order": 10,
            },
            {
                "slug": "deluxe-suite",
                "name": "Deluxe Suite",
                "short_description": "Spacious suite with a separate lounge area, king bed and city panorama.",
                "description": (
                    "The Deluxe Suite offers a separate living area, a king-size bed and a sofa bed, "
                    "making it ideal for families or longer stays. Enjoy the evening panorama from "
                    "the corner windows and a complimentary minibar on arrival."
                ),
                "capacity": 4,
                "base_price": Decimal("780.00"),
                "currency": "PLN",
                "size_sqm": 45,
                "bed_configuration": "King bed + sofa bed",
                "amenities": [
                    "Free Wi-Fi",
                    "Air conditioning",
                    "55\" Smart TV",
                    "Nespresso machine",
                    "Bathtub and rain shower",
                    "Lounge area",
                    "Complimentary minibar",
                    "City panorama",
                ],
                "sort_order": 20,
            },
        ]
        result: dict[str, RoomType] = {}
        for item in data:
            slug = item["slug"]
            room_type, _ = RoomType.objects.get_or_create(slug=slug, defaults=item)
            result[slug] = room_type
        return result

    def _seed_rooms(self, room_types: dict[str, RoomType]) -> None:
        rooms = [
            ("101", "Garden view", "standard-double", 1),
            ("102", "Garden view", "standard-double", 1),
            ("103", "City view", "standard-double", 1),
            ("201", "Corner suite", "deluxe-suite", 2),
            ("202", "Panorama suite", "deluxe-suite", 2),
        ]
        for number, name, slug, floor in rooms:
            Room.objects.get_or_create(
                number=number,
                defaults={"name": name, "room_type": room_types[slug], "floor": floor},
            )

    def _seed_pricing(self) -> None:
        year = timezone.localdate().year
        rules = [
            {
                "name": "Weekend uplift",
                "kind": PricingRule.Kind.WEEKEND,
                "days_of_week": "4,5",
                "adjustment_type": PricingRule.AdjustmentType.PERCENT,
                "amount": Decimal("15.00"),
                "priority": 10,
            },
            {
                "name": "Long stay discount",
                "kind": PricingRule.Kind.LONG_STAY,
                "min_nights": 7,
                "adjustment_type": PricingRule.AdjustmentType.PERCENT,
                "amount": Decimal("-10.00"),
                "priority": 20,
            },
            {
                "name": "Summer season",
                "kind": PricingRule.Kind.SEASONAL,
                "start_date": date(year, 6, 1),
                "end_date": date(year, 9, 30),
                "adjustment_type": PricingRule.AdjustmentType.PERCENT,
                "amount": Decimal("20.00"),
                "priority": 5,
            },
        ]
        for item in rules:
            PricingRule.objects.get_or_create(name=item["name"], defaults=item)

    def _seed_clients(self) -> None:
        clients = [
            {
                "first_name": "Anna",
                "last_name": "Kowalska",
                "email": "anna.kowalska@example.com",
                "phone": "+48 600 100 200",
                "language": "pl",
                "country": "PL",
            },
            {
                "first_name": "John",
                "last_name": "Smith",
                "email": "john.smith@example.com",
                "phone": "+44 7700 900123",
                "language": "en",
                "country": "GB",
            },
        ]
        for item in clients:
            Client.objects.get_or_create(email=item["email"], defaults=item)

    def _seed_providers(self):
        from apps.agents.models import AIProvider

        openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        ollama_enabled = os.getenv("OLLAMA_ENABLED", "false").lower() in {"1", "true", "yes"}

        definitions = [
            {
                "slug": "demo",
                "name": "Demo policy (offline)",
                "provider_type": AIProvider.Type.DEMO,
                "model": "demo-policy-v1",
                "base_url": "",
                "api_key": "",
                "is_active": True,
                "temperature": 0.2,
                "max_tokens": 800,
                "notes": "Deterministic bilingual receptionist. No API key or network required.",
            },
            {
                "slug": "openai",
                "name": "OpenAI",
                "provider_type": AIProvider.Type.OPENAI,
                "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                "api_key": openai_key,
                "is_active": bool(openai_key),
                "temperature": 0.3,
                "max_tokens": 800,
            },
            {
                "slug": "anthropic",
                "name": "Anthropic",
                "provider_type": AIProvider.Type.ANTHROPIC,
                "model": os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-latest"),
                "base_url": os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
                "api_key": anthropic_key,
                "is_active": bool(anthropic_key),
                "temperature": 0.3,
                "max_tokens": 800,
            },
            {
                "slug": "ollama",
                "name": "Ollama (self-hosted)",
                "provider_type": AIProvider.Type.OLLAMA,
                "model": os.getenv("OLLAMA_MODEL", "llama3.1"),
                "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
                "api_key": "",
                "is_active": ollama_enabled,
                "temperature": 0.3,
                "max_tokens": 800,
                "notes": "Point OLLAMA_BASE_URL at your Ollama instance and enable the provider.",
            },
        ]

        for item in definitions:
            AIProvider.objects.get_or_create(slug=item["slug"], defaults=item)

        if not AIProvider.objects.filter(is_default=True, is_active=True).exists():
            preferred = "openai" if openai_key else "anthropic" if anthropic_key else "demo"
            provider = AIProvider.objects.filter(slug=preferred).first() or AIProvider.objects.filter(
                slug="demo"
            ).first()
            if provider:
                provider.is_default = True
                provider.save(update_fields=["is_default", "updated_at"])
                return provider
        return AIProvider.objects.filter(is_default=True).first()

    def _seed_agent_settings(self, provider) -> None:
        from apps.agents.defaults import build_default_agent_settings
        from apps.agents.models import AgentSettings

        if AgentSettings.objects.exists():
            return
        AgentSettings.objects.create(**build_default_agent_settings(provider))

    def _seed_calendar(self) -> None:
        from apps.calendar_sync.models import CalendarConnection, RoomCalendar

        if CalendarConnection.objects.exists():
            return
        connection = CalendarConnection.objects.create(
            name="Demo calendar",
            provider=CalendarConnection.Provider.FAKE,
            calendar_id="demo@hotel.local",
            sync_enabled=True,
            notes="Fake provider used by the alpha demo. Replace with a Google connection in production.",
        )
        for room in Room.objects.all():
            RoomCalendar.objects.get_or_create(
                room=room,
                defaults={"connection": connection, "calendar_id": f"demo-{room.number}@hotel.local"},
            )

    def _seed_sample_booking(self) -> None:
        if Booking.objects.exists():
            return
        room_type = RoomType.objects.filter(slug="standard-double").first()
        if not room_type:
            return
        start = timezone.localdate() + timedelta(days=14)
        try:
            booking = create_booking(
                room_type=room_type,
                check_in=start,
                check_out=start + timedelta(days=3),
                first_name="Anna",
                last_name="Kowalska",
                email="anna.kowalska@example.com",
                phone="+48 600 100 200",
                adults=2,
                source=Booking.Source.ADMIN,
                language="pl",
                special_requests="High floor please.",
            )
            confirm_booking(booking.reference)
        except Exception as exc:  # noqa: BLE001 - demo data must never block boot
            self.stdout.write(self.style.WARNING(f"Sample booking skipped: {exc}"))
