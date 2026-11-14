"""Tool definitions and execution for the receptionist agent.

Tools are the only way the model can read availability, prices or mutate
bookings. Every handler is defensive: it returns ``{"ok": false, "error": ...}``
instead of raising into the conversation loop.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.core.exceptions import ValidationError

from apps.hotel.availability import availability_for_agent, search_availability
from apps.hotel.booking_service import (
    BookingError,
    cancel_booking,
    confirm_booking,
    create_booking,
    get_booking,
    resolve_room_type,
)
from apps.hotel.cancellation import cancellation_preview
from apps.hotel.pricing import StayError, quote

from .models import AgentSettings, Conversation

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "check_availability",
        "description": (
            "Search real room availability for a date range. Always call this before quoting a room, "
            "a price or availability. Returns room types, remaining rooms, nightly rate and total price."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "check_in": {"type": "string", "description": "Check-in date, ISO format YYYY-MM-DD."},
                "check_out": {"type": "string", "description": "Check-out date, ISO format YYYY-MM-DD."},
                "guests": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8,
                    "description": "Total number of guests.",
                },
                "room_type": {
                    "type": "string",
                    "description": "Optional room type slug to restrict the search.",
                },
            },
            "required": ["check_in", "check_out", "guests"],
        },
    },
    {
        "name": "get_pricing",
        "description": (
            "Get a detailed nightly price breakdown for one room type and date range, including "
            "applied pricing rules, subtotal, VAT included and total."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "room_type": {"type": "string", "description": "Room type slug, e.g. standard-double."},
                "check_in": {"type": "string", "description": "Check-in date, ISO format YYYY-MM-DD."},
                "check_out": {"type": "string", "description": "Check-out date, ISO format YYYY-MM-DD."},
                "adults": {"type": "integer", "minimum": 1, "maximum": 8},
                "children": {"type": "integer", "minimum": 0, "maximum": 6},
            },
            "required": ["room_type", "check_in", "check_out"],
        },
    },
    {
        "name": "create_booking",
        "description": (
            "Create a temporary booking hold (status pending) for a room type. Requires the guest's "
            "first name, last name and an email address or phone number. Does not confirm the booking."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "room_type": {"type": "string", "description": "Room type slug."},
                "check_in": {"type": "string", "description": "Check-in date, ISO format YYYY-MM-DD."},
                "check_out": {"type": "string", "description": "Check-out date, ISO format YYYY-MM-DD."},
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "email": {"type": "string", "description": "Guest email, or empty when only a phone is given."},
                "phone": {"type": "string", "description": "Guest phone, or empty when only an email is given."},
                "adults": {"type": "integer", "minimum": 1, "maximum": 8},
                "children": {"type": "integer", "minimum": 0, "maximum": 6},
                "special_requests": {"type": "string"},
                "rate_plan": {
                    "type": "string",
                    "description": "Optional rate plan slug chosen from the availability results' rate_plans list.",
                },
            },
            "required": ["room_type", "check_in", "check_out", "first_name"],
        },
    },
    {
        "name": "confirm_booking",
        "description": (
            "Confirm a pending booking hold. Only call after the guest explicitly agreed to the "
            "summary and total price."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reference": {"type": "string", "description": "Booking reference, e.g. HA-7KQ2M4."}
            },
            "required": ["reference"],
        },
    },
    {
        "name": "get_booking",
        "description": "Look up an existing booking by its reference.",
        "parameters": {
            "type": "object",
            "properties": {"reference": {"type": "string"}},
            "required": ["reference"],
        },
    },
    {
        "name": "cancel_booking",
        "description": (
            "Cancel a booking by reference after the guest explicitly asks to cancel it. "
            "Returns the cancellation fee calculated by the hotel policy."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reference": {"type": "string"},
                "reason": {
                    "type": "string",
                    "enum": ["guest_request", "plans_changed", "found_alternative", "price", "emergency", "other"],
                    "description": "Optional cancellation reason supplied by the guest.",
                },
                "note": {"type": "string", "description": "Optional free-text note."},
            },
            "required": ["reference"],
        },
    },
]

TOOL_NAMES = [spec["name"] for spec in TOOL_SPECS]


class ToolArgumentError(Exception):
    pass


def error_payload(code: str, message: str, details: dict | None = None) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "details": details or {}}}


def booking_payload(booking) -> dict:
    preview = cancellation_preview(booking)
    return {
        "reference": booking.reference,
        "status": booking.status,
        "room": booking.room.number,
        "room_type": booking.room_type.slug,
        "room_type_name": booking.room_type.name,
        "rate_plan": booking.rate_plan.slug if booking.rate_plan else None,
        "check_in": booking.check_in.isoformat(),
        "check_out": booking.check_out.isoformat(),
        "nights": booking.nights,
        "adults": booking.adults,
        "children": booking.children,
        "total": str(booking.total_price),
        "currency": booking.currency,
        "hold_expires_at": booking.hold_expires_at.isoformat() if booking.hold_expires_at else None,
        "guest_name": booking.client.full_name,
        "guest_email": booking.client.email,
        "guest_phone": booking.client.phone,
        "source": booking.source,
        "can_confirm": booking.can_confirm,
        "can_cancel": booking.can_cancel,
        "can_modify": booking.can_modify,
        "cancellation": preview.as_dict(),
        "cancellation_fee": str(booking.cancellation_fee),
        "cancellation_reason": booking.cancellation_reason,
        "cancelled_by": booking.cancelled_by,
    }


class ToolExecutor:
    def __init__(self, conversation: Conversation):
        self.conversation = conversation
        self.settings = AgentSettings.load()

    def execute(self, name: str, arguments: dict[str, Any]) -> dict:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return error_payload("unknown_tool", f"Unknown tool {name!r}.")
        try:
            return handler(**arguments)
        except ToolArgumentError as exc:
            return error_payload("invalid_arguments", str(exc))
        except BookingError as exc:
            return error_payload(exc.code, exc.message, exc.details)
        except (StayError, ValidationError) as exc:
            message = str(exc.messages[0]) if getattr(exc, "messages", None) else str(exc)
            return error_payload("validation_error", message)
        except TypeError as exc:
            return error_payload("invalid_arguments", f"Invalid arguments: {exc}")
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the model
            return error_payload("tool_error", f"The tool failed: {exc}")

    # ------------------------------------------------------------------ tools
    def _tool_check_availability(
        self,
        check_in: str,
        check_out: str,
        guests: int = 2,
        room_type: str | None = None,
    ) -> dict:
        start = self._date(check_in, "check_in")
        end = self._date(check_out, "check_out")
        guests = self._int(guests, "guests", minimum=1, maximum=8)
        try:
            if room_type:
                options = search_availability(start, end, guests=guests, room_type_slug=room_type)
                data = {
                    "check_in": start.isoformat(),
                    "check_out": end.isoformat(),
                    "guests": guests,
                    "options": [
                        {
                            "room_type": item["room_type"]["slug"],
                            "name": item["room_type"]["name"],
                            "capacity": item["room_type"]["capacity"],
                            "available_rooms": item["available_rooms"],
                            "nights": item["quote"]["nights"],
                            "nightly_from": item["quote"]["nightly"][0]["rate"]
                            if item["quote"]["nightly"]
                            else None,
                            "total": item["quote"]["total"],
                            "currency": item["quote"]["currency"],
                            "rate_plans": item.get("rate_plans", []),
                        }
                        for item in options
                    ],
                }
            else:
                data = availability_for_agent(start, end, guests)
        except ValidationError as exc:
            message = str(exc.messages[0]) if getattr(exc, "messages", None) else str(exc)
            return error_payload("invalid_dates", message)

        self.conversation.state["last_search"] = {
            "check_in": start.isoformat(),
            "check_out": end.isoformat(),
            "guests": guests,
        }
        self.conversation.save(update_fields=["state", "updated_at"])
        return {"ok": True, "availability": data}

    def _tool_get_pricing(
        self,
        room_type: str,
        check_in: str,
        check_out: str,
        adults: int = 2,
        children: int = 0,
    ) -> dict:
        start = self._date(check_in, "check_in")
        end = self._date(check_out, "check_out")
        adults = self._int(adults, "adults", minimum=1, maximum=8)
        children = self._int(children, "children", minimum=0, maximum=6)
        record = resolve_room_type(room_type)
        priced = quote(record, start, end, adults=adults, children=children)
        return {"ok": True, "quote": priced.as_dict()}

    def _tool_create_booking(
        self,
        room_type: str,
        check_in: str,
        check_out: str,
        first_name: str,
        last_name: str = "",
        email: str = "",
        phone: str = "",
        adults: int = 2,
        children: int = 0,
        special_requests: str = "",
        rate_plan: str | None = None,
    ) -> dict:
        start = self._date(check_in, "check_in")
        end = self._date(check_out, "check_out")
        adults = self._int(adults, "adults", minimum=1, maximum=8)
        children = self._int(children, "children", minimum=0, maximum=6)
        booking = create_booking(
            room_type=room_type,
            check_in=start,
            check_out=end,
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone=phone,
            adults=adults,
            children=children,
            source=self.conversation.channel,
            language=self.conversation.language,
            special_requests=special_requests,
            rate_plan=rate_plan,
            hold_minutes=self.settings.booking_hold_minutes,
        )
        self.conversation.booking = booking
        self.conversation.client = booking.client
        self.conversation.state["last_booking"] = booking_payload(booking)
        self.conversation.save(update_fields=["booking", "client", "state", "updated_at"])
        return {"ok": True, "booking": booking_payload(booking)}

    def _tool_confirm_booking(self, reference: str) -> dict:
        booking = confirm_booking(
            reference,
            actor="agent",
            channel=self.conversation.channel,
        )
        self.conversation.booking = booking
        self.conversation.state["last_booking"] = booking_payload(booking)
        self.conversation.save(update_fields=["booking", "state", "updated_at"])
        return {"ok": True, "booking": booking_payload(booking)}

    def _tool_get_booking(self, reference: str) -> dict:
        booking = get_booking(reference)
        return {"ok": True, "booking": booking_payload(booking)}

    def _tool_cancel_booking(self, reference: str, reason: str = "", note: str = "") -> dict:
        booking = cancel_booking(
            reference,
            reason=reason,
            note=note,
            actor="agent",
            channel=self.conversation.channel,
        )
        self.conversation.state["last_booking"] = booking_payload(booking)
        self.conversation.save(update_fields=["state", "updated_at"])
        return {"ok": True, "booking": booking_payload(booking)}

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _date(value, field: str) -> date:
        if isinstance(value, date):
            return value
        text = str(value or "").strip().lower()
        from django.utils import timezone as _tz

        base = _tz.localdate()
        if text in {"today", "dzisiaj", "dzis"}:
            return base
        if text in {"tomorrow", "jutro"}:
            return base + timedelta(days=1)
        if text in {"yesterday", "wczoraj"}:
            return base - timedelta(days=1)
        try:
            return date.fromisoformat(text)
        except (ValueError, TypeError) as exc:
            raise ToolArgumentError(f"{field} must be an ISO date (YYYY-MM-DD).") from exc

    @staticmethod
    def _int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (ValueError, TypeError) as exc:
            raise ToolArgumentError(f"{field} must be a number.") from exc
        if number < minimum or number > maximum:
            raise ToolArgumentError(f"{field} must be between {minimum} and {maximum}.")
        return number
