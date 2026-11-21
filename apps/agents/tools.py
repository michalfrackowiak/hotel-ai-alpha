"""Tool definitions and execution for the receptionist agent.

Tools are the only way the model can read availability, prices or mutate
bookings. Every handler is defensive: it returns ``{"ok": false, "error": ...}``
instead of raising into the conversation loop.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.core.exceptions import ValidationError
from django.utils import timezone

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
from apps.hotel.notifications import send_booking_confirmation, send_booking_confirmation_sms
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
    {
        "name": "send_confirmation_email",
        "description": (
            "Send (or resend) the booking confirmation email to the guest. Optionally update the "
            "guest's email address first. Only call after the guest agreed to receive the email."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reference": {"type": "string", "description": "Booking reference, e.g. HA-7KQ2M4."},
                "email": {"type": "string", "description": "Optional email address to store and send to."},
            },
            "required": ["reference"],
        },
    },
    {
        "name": "send_confirmation_sms",
        "description": (
            "Send a short booking confirmation SMS to the guest's phone. Optionally update the "
            "guest's phone number first. Only call after the guest chose SMS and gave a phone number."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reference": {"type": "string", "description": "Booking reference, e.g. HA-7KQ2M4."},
                "phone": {"type": "string", "description": "Optional phone number to store and send to."},
            },
            "required": ["reference"],
        },
    },
    {
        "name": "transfer_to_human",
        "description": (
            "Hand the conversation to a reception employee. Use when the guest asks about something "
            "other than a booking, or explicitly asks for a human. Do not use it while a booking is "
            "still in progress unless the guest insists."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Short summary of why the guest needs a human."},
            },
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
        "confirmation_email_status": booking.confirmation_email_status,
        "confirmation_sms_status": booking.confirmation_sms_status,
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

    def _tool_send_confirmation_email(self, reference: str, email: str = "") -> dict:
        booking = get_booking(reference)
        if email:
            email = email.strip().lower()
            if booking.client.account_id:
                from apps.hotel.guest_access import verified_guest_account_for_email

                owner_account = verified_guest_account_for_email(email)
                if owner_account is not None and owner_account.id != booking.client.account_id:
                    return error_payload(
                        "contact_conflict",
                        "This email address cannot be used for this booking.",
                    )
            booking.client.email = email
            booking.client.save(update_fields=["email", "updated_at"])
        booking.refresh_from_db()
        if not (booking.client.email or "").strip():
            return error_payload("contact_required", "An email address is required to send the confirmation.")
        try:
            sent = send_booking_confirmation(booking, force=True)
        except Exception as exc:  # noqa: BLE001
            return error_payload("email_failed", str(exc))
        return {
            "ok": True,
            "sent": bool(sent),
            "email": booking.client.email,
            "reference": booking.reference,
        }

    def _tool_send_confirmation_sms(self, reference: str, phone: str = "") -> dict:
        booking = get_booking(reference)
        if phone:
            booking.client.phone = phone.strip()
            booking.client.save(update_fields=["phone", "updated_at"])
        booking.refresh_from_db()
        if not (booking.client.phone or "").strip():
            return error_payload("contact_required", "A phone number is required to send the SMS.")
        try:
            sent = send_booking_confirmation_sms(booking, force=True)
        except Exception as exc:  # noqa: BLE001
            return error_payload("sms_failed", str(exc))
        return {
            "ok": True,
            "sent": bool(sent),
            "phone": booking.client.phone,
            "reference": booking.reference,
        }

    def _tool_transfer_to_human(self, reason: str = "") -> dict:
        self.conversation.state["handover_requested"] = {
            "reason": (reason or "").strip(),
            "at": timezone.now().isoformat(),
        }
        self.conversation.save(update_fields=["state", "updated_at"])
        return {"ok": True, "handover": True, "reason": (reason or "").strip()}

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _date(value, field: str) -> date:
        if isinstance(value, date):
            return value
        text = str(value or "").strip()
        lowered = text.lower()
        from django.utils import timezone as _tz

        base = _tz.localdate()
        if lowered in {"today", "dzisiaj", "dziś", "dzis", "tonight"}:
            return base
        if lowered in {"tomorrow", "jutro"}:
            return base + timedelta(days=1)
        if lowered in {"yesterday", "wczoraj"}:
            return base - timedelta(days=1)
        if lowered in {"day after tomorrow", "pojutrze"}:
            return base + timedelta(days=2)
        try:
            return date.fromisoformat(lowered)
        except (ValueError, TypeError):
            pass

        # Flexible formats as a safety net for LLM/tool inputs.
        import re

        month_names = {
            "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
            "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
            "august": 8, "aug": 8, "september": 9, "sep": 9, "october": 10, "oct": 10,
            "november": 11, "nov": 11, "december": 12, "dec": 12,
            "stycznia": 1, "styczeń": 1, "lutego": 2, "luty": 2, "marca": 3, "marzec": 3,
            "kwietnia": 4, "kwiecień": 4, "maja": 5, "maj": 5, "czerwca": 6, "czerwiec": 6,
            "lipca": 7, "lipiec": 7, "sierpnia": 8, "sierpień": 8, "września": 9, "wrzesień": 9,
            "października": 10, "październik": 10, "listopada": 11, "listopad": 11,
            "grudnia": 12, "grudzień": 12,
        }

        def _without_year(month: int, day: int) -> date | None:
            try:
                candidate = date(base.year, month, day)
            except ValueError:
                return None
            if candidate < base:
                try:
                    return date(base.year + 1, month, day)
                except ValueError:
                    return None
            return candidate

        for pattern, order in (
            (r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+)\s+(20\d{2})\b", "dmy"),
            (r"\b([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(20\d{2})\b", "mdy"),
        ):
            m = re.search(pattern, lowered)
            if not m:
                continue
            if order == "dmy":
                day_s, month_s, year_s = m.group(1), m.group(2), m.group(3)
            else:
                month_s, day_s, year_s = m.group(1), m.group(2), m.group(3)
            month = month_names.get(month_s)
            if month:
                try:
                    return date(int(year_s), month, int(day_s))
                except ValueError:
                    pass
        # Month name without year.
        for pattern, order in (
            (r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+)\b", "dmy"),
            (r"\b([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+)\s+(\d{1,2})(?:st|nd|rd|th)?\b", "mdy"),
        ):
            m = re.search(pattern, lowered)
            if not m:
                continue
            if order == "dmy":
                day_s, month_s = m.group(1), m.group(2)
            else:
                month_s, day_s = m.group(1), m.group(2)
            month = month_names.get(month_s)
            if month:
                candidate = _without_year(month, int(day_s))
                if candidate is not None:
                    return candidate
        for pattern in (r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b",):
            m = re.search(pattern, lowered)
            if m:
                try:
                    return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                except ValueError:
                    pass
        m = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", lowered)
        if m:
            # Prefer DD/MM/YYYY, fall back to MM/DD/YYYY.
            try:
                return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                try:
                    return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
                except ValueError:
                    pass
        # Day/month without year.
        for pattern in (r"\b(\d{1,2})\.(\d{1,2})\b", r"\b(\d{1,2})/(\d{1,2})\b"):
            m = re.search(pattern, lowered)
            if m:
                day_num, month_num = int(m.group(1)), int(m.group(2))
                if 1 <= day_num <= 31 and 1 <= month_num <= 12:
                    candidate = _without_year(month_num, day_num)
                    if candidate is not None:
                        return candidate

        raise ToolArgumentError(f"{field} must be an ISO date (YYYY-MM-DD).")

    @staticmethod
    def _int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (ValueError, TypeError) as exc:
            raise ToolArgumentError(f"{field} must be a number.") from exc
        if number < minimum or number > maximum:
            raise ToolArgumentError(f"{field} must be between {minimum} and {maximum}.")
        return number
