"""Offline demo policy.

This connector implements a small, deterministic receptionist so the alpha can
be demonstrated (and tested) without any API key. It exercises exactly the same
tools as the LLM connectors: availability → pricing → hold → confirmation.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from django.utils import timezone

from .base import (
    BaseLLMProvider,
    ChatMessage,
    LLMResponse,
    ProviderHealth,
    ToolCall,
    ToolSpec,
)

MONTHS = {
    "january": 1, "jan": 1, "stycznia": 1, "styczeń": 1,
    "february": 2, "feb": 2, "lutego": 2, "luty": 2,
    "march": 3, "mar": 3, "marca": 3, "marzec": 3,
    "april": 4, "apr": 4, "kwietnia": 4, "kwiecień": 4,
    "may": 5, "maja": 5, "maj": 5,
    "june": 6, "jun": 6, "czerwca": 6, "czerwiec": 6,
    "july": 7, "jul": 7, "lipca": 7, "lipiec": 7,
    "august": 8, "aug": 8, "sierpnia": 8, "sierpień": 8,
    "september": 9, "sep": 9, "września": 9, "wrzesień": 9,
    "october": 10, "oct": 10, "października": 10, "październik": 10,
    "november": 11, "nov": 11, "listopada": 11, "listopad": 11,
    "december": 12, "dec": 12, "grudnia": 12, "grudzień": 12,
}

AFFIRMATIVE = re.compile(
    r"\b(yes|yeah|yep|sure|ok|okay|confirm|confirmed|book it|go ahead|"
    r"tak|potwierdzam|zgoda|rezerwuj|proszę|dobrze)\b",
    re.IGNORECASE,
)
CANCEL_INTENT = re.compile(r"\b(cancel|cancelled|anuluj|anulować|odwołaj|odwołać)\b", re.IGNORECASE)
REFERENCE = re.compile(r"\bHA-[A-Z0-9]{6}\b", re.IGNORECASE)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
PHONE = re.compile(r"(?<![\d])(\+?\d[\d\s()\-]{7,}\d)(?![\d])")
GUESTS = re.compile(
    r"(\d+)\s*(?:adult|adults|guest|guests|person|people|osob[a-y]?|goś[ćc]i?)", re.IGNORECASE
)
NIGHTS = re.compile(r"(\d+)\s*(?:night|nights|noc|noce|nocy)", re.IGNORECASE)
FOR_N = re.compile(r"\b(?:for|dla)\s+(\d+)\b", re.IGNORECASE)
STOPWORDS = {
    "email", "e-mail", "mail", "phone", "telephone", "tel", "number", "numer",
    "is", "jest", "my", "moje", "name", "imię", "nazwisko", "and", "i",
    "please", "proszę", "at", "o",
}

# Words that must never be mistaken for a guest's name when the reply is a
# bare "Anna Kowalska" style message.
NAME_STOPWORDS = STOPWORDS | set(MONTHS) | {
    "do", "does", "did", "can", "could", "would", "should", "will", "is", "are", "am",
    "i", "we", "you", "he", "she", "they", "the", "a", "an", "this", "that",
    "hello", "hi", "hey", "good", "morning", "afternoon", "evening", "please",
    "thanks", "thank", "yes", "no", "sure", "ok", "okay", "room", "rooms",
    "suite", "standard", "deluxe", "double", "single", "twin", "night", "nights",
    "day", "days", "today", "tomorrow", "weekend", "week", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday",
    "tak", "nie", "proszę", "dzień", "dobry", "dobra", "witam", "cześć", "pokój",
    "pokoje", "pokoju", "noc", "noce", "nocy", "rezerwacja", "rezerwację",
    "rezerwacje", "chcę", "chciałbym", "chciałabym", "jestem", "nazywam", "się",
    "mam", "na", "od", "dla", "gości", "gość", "osoby", "osób",
}

NAME_TOKEN = re.compile(
    r"^[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+(?:-[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+)?$"
)


class DemoProvider(BaseLLMProvider):
    provider_type = "demo"
    requires_api_key = False

    def health_check(self) -> ProviderHealth:
        return ProviderHealth(ok=True, message="Demo policy is always available.", latency_ms=0)

    def chat(self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None) -> LLMResponse:
        system = next((message.content for message in messages if message.role == "system"), "")
        language = self._language(system, messages)
        users = [message for message in messages if message.role == "user"]
        last_user = users[-1].content if users else ""
        history = "\n".join(message.content for message in users)
        slots = self._extract_slots(history, last_user)

        results = self._tool_results(messages)
        availability = results.get("check_availability")
        created = results.get("create_booking")
        confirmed = results.get("confirm_booking")
        cancelled = results.get("cancel_booking")

        if cancelled:
            if cancelled.get("ok"):
                return self._text(self._cancelled(language, cancelled.get("booking", {})), language)
            return self._text(self._error(language, cancelled), language)

        if confirmed:
            if confirmed.get("ok"):
                return self._text(self._confirmed(language, confirmed.get("booking", {})), language)
            return self._text(self._error(language, confirmed), language)

        if created:
            if not created.get("ok"):
                return self._text(self._error(language, created), language)
            booking = created.get("booking", {})
            if self._last_is_tool(messages) or not self._is_affirmative(last_user):
                return self._text(self._ask_confirm(language, booking), language)
            return self._tool("confirm_booking", {"reference": booking.get("reference")}, language)

        reference_match = REFERENCE.search(history)
        if reference_match and CANCEL_INTENT.search(last_user):
            return self._tool(
                "cancel_booking",
                {"reference": reference_match.group(0).upper(), "reason": "guest_request"},
                language,
            )

        availability_data = self._fresh_availability(availability, slots)
        options = (availability_data or {}).get("options") or []

        if availability is not None and availability_data is None:
            # Dates changed since the last search: search again.
            if slots["check_in"] and slots["check_out"]:
                return self._tool(
                    "check_availability",
                    {
                        "check_in": slots["check_in"].isoformat(),
                        "check_out": slots["check_out"].isoformat(),
                        "guests": slots["guests"],
                    },
                    language,
                )

        if availability is not None and not availability.get("ok"):
            return self._text(self._error(language, availability), language)

        if availability_data is not None and not options:
            return self._text(self._no_availability(language, slots), language)

        contact = slots["email"] or slots["phone"]
        if slots["check_in"] and slots["check_out"] and options and contact:
            if slots["check_in"] >= slots["check_out"]:
                return self._text(self._ask_dates(language), language)
            room_type = self._pick_room_type(last_user, options, slots)
            return self._tool(
                "create_booking",
                {
                    "room_type": room_type,
                    "check_in": slots["check_in"].isoformat(),
                    "check_out": slots["check_out"].isoformat(),
                    "first_name": slots["first_name"] or "Guest",
                    "last_name": slots["last_name"],
                    "email": slots["email"],
                    "phone": slots["phone"],
                    "adults": slots["guests"],
                    "children": 0,
                },
                language,
            )

        if options and self._last_is_tool(messages):
            return self._text(self._offer(language, availability_data, slots), language)

        if options and contact:
            return self._text(self._offer(language, availability_data, slots), language)

        if slots["check_in"] and slots["check_out"]:
            return self._tool(
                "check_availability",
                {
                    "check_in": slots["check_in"].isoformat(),
                    "check_out": slots["check_out"].isoformat(),
                    "guests": slots["guests"],
                },
                language,
            )

        return self._text(self._ask_dates(language), language)

    # ------------------------------------------------------------------ tools
    def _tool_results(self, messages: list[ChatMessage]) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for message in reversed(messages):
            if message.role != "tool" or not message.name or message.name in found:
                continue
            try:
                payload = json.loads(message.content)
            except (json.JSONDecodeError, TypeError):
                payload = {"ok": False, "error": {"message": message.content[:200]}}
            found[message.name] = payload if isinstance(payload, dict) else {"ok": False}
        return found

    def _fresh_availability(self, availability: dict | None, slots: dict) -> dict | None:
        if not availability or not availability.get("ok"):
            return None
        data = availability.get("availability") or {}
        if not slots["check_in"] or not slots["check_out"]:
            return None
        if data.get("check_in") != slots["check_in"].isoformat():
            return None
        if data.get("check_out") != slots["check_out"].isoformat():
            return None
        return data

    @staticmethod
    def _last_is_tool(messages: list[ChatMessage]) -> bool:
        return bool(messages) and messages[-1].role == "tool"

    @staticmethod
    def _is_affirmative(text: str) -> bool:
        return bool(AFFIRMATIVE.search(text or ""))

    # ------------------------------------------------------------------ slots
    def _extract_slots(self, history: str, latest: str) -> dict[str, Any]:
        check_in, check_out = self._extract_dates(latest)
        if not check_in:
            check_in, check_out = self._extract_dates(history)

        email = (EMAIL.search(latest) or EMAIL.search(history))
        phone_text = self._strip_dates(latest) or self._strip_dates(history)
        phone = PHONE.search(phone_text) if phone_text else None
        guests = self._first_int(GUESTS, latest) or self._first_int(GUESTS, history) or 2
        first_name, last_name = self._extract_name(history, latest)

        return {
            "check_in": check_in,
            "check_out": check_out,
            "email": email.group(0) if email else "",
            "phone": phone.group(1).strip() if phone else "",
            "guests": max(1, min(guests, 8)),
            "first_name": first_name,
            "last_name": last_name,
            "room_type": self._extract_room_type(history),
        }

    def _extract_dates(self, text: str) -> tuple[date | None, date | None]:
        if not text:
            return None, None
        lowered = text.lower()
        found: list[date] = []

        # "12–15 December 2026" / "12 do 15 grudnia 2026"
        for match in re.finditer(
            r"\b(\d{1,2})\s*(?:-|–|to|do)\s*(\d{1,2})\s+([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+)\s+(20\d{2})\b",
            lowered,
        ):
            month = MONTHS.get(match.group(3))
            if month:
                found.extend(self._safe_date(int(match.group(4)), month, int(match.group(1))))
                found.append(self._safe_date(int(match.group(4)), month, int(match.group(2))))
                break

        if not found:
            for match in re.finditer(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", lowered):
                found.append(self._safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
            for match in re.finditer(r"\b(\d{1,2})[./](\d{1,2})[./](20\d{2})\b", lowered):
                found.append(self._safe_date(int(match.group(3)), int(match.group(2)), int(match.group(1))))
            for match in re.finditer(
                r"\b(\d{1,2})\s+([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+)\s+(20\d{2})\b", lowered
            ):
                month = MONTHS.get(match.group(2))
                if month:
                    found.append(self._safe_date(int(match.group(3)), month, int(match.group(1))))

        dates: list[date] = []
        for item in found:
            if item and item not in dates:
                dates.append(item)

        if not dates:
            today = timezone.localdate()
            if re.search(r"\btomorrow\b|\bjutro\b", lowered):
                dates = [today + timedelta(days=1)]
            elif re.search(r"\btoday\b|\bdziś\b|\bdzisiaj\b", lowered):
                dates = [today]

        if len(dates) >= 2:
            return dates[0], dates[1]
        if len(dates) == 1:
            nights = self._first_int(NIGHTS, lowered)
            if nights:
                return dates[0], dates[0] + timedelta(days=nights)
            return dates[0], None
        return None, None

    @staticmethod
    def _safe_date(year: int, month: int, day: int) -> date | None:
        try:
            return date(year, month, day)
        except ValueError:
            return None

    @staticmethod
    def _strip_dates(text: str) -> str:
        cleaned = re.sub(r"\b20\d{2}-\d{1,2}-\d{1,2}\b", " ", text or "")
        cleaned = re.sub(r"\b\d{1,2}[./]\d{1,2}[./]20\d{2}\b", " ", cleaned)
        return cleaned

    @staticmethod
    def _first_int(pattern: re.Pattern, text: str) -> int | None:
        match = pattern.search(text or "")
        return int(match.group(1)) if match else None

    def _extract_name(self, text: str, latest: str = "") -> tuple[str, str]:
        for source in (latest, text):
            match = re.search(
                r"(?:my name is|i am|jestem|nazywam się)\s+"
                r"([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż-]+(?:\s+[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż-]+)?)",
                source or "",
                re.IGNORECASE,
            )
            if match:
                parts = match.group(1).split()
                return parts[0], " ".join(parts[1:])

        # A bare reply such as "Anna Kowalska" (1–3 capitalised words).
        for source in (latest, text):
            name = self._bare_name(source)
            if name[0]:
                return name

        email_match = EMAIL.search(text or "") or EMAIL.search(latest or "")
        if email_match:
            prefix = (text or "")[: email_match.start()]
            words = [
                word.strip(" ,.;:-")
                for word in re.split(r"\s+", prefix)
                if word.strip(" ,.;:-").lower() not in STOPWORDS
            ]
            words = [word for word in words if word and word[0].isupper()]
            if len(words) >= 2:
                return words[-2], words[-1]
            if words:
                return words[-1], ""
        return "", ""

    @staticmethod
    def _bare_name(text: str) -> tuple[str, str]:
        cleaned = re.sub(r"[.,;:!?()\"'“”‘’]", " ", (text or "").strip())
        tokens = [token for token in cleaned.split() if token]
        if not tokens or len(tokens) > 3:
            return "", ""
        if any(token.lower() in NAME_STOPWORDS for token in tokens):
            return "", ""
        if not all(NAME_TOKEN.match(token) for token in tokens):
            return "", ""
        return tokens[0], " ".join(tokens[1:])

    @staticmethod
    def _extract_room_type(text: str) -> str:
        lowered = (text or "").lower()
        if any(word in lowered for word in ("suite", "apartament", "deluxe", "luksus")):
            return "deluxe-suite"
        if any(word in lowered for word in ("standard", "podstaw", "double")):
            return "standard-double"
        return ""

    def _pick_room_type(self, latest: str, options: list[dict], slots: dict) -> str:
        available = [option.get("room_type") for option in options if option.get("room_type")]
        preferred = self._extract_room_type(latest) or slots.get("room_type")
        if preferred and preferred in available:
            return preferred
        return available[0] if available else "standard-double"

    @staticmethod
    def _language(system: str, messages: list[ChatMessage]) -> str:
        match = re.search(r"LANGUAGE:\s*(en|pl)", system or "")
        if match:
            return match.group(1)
        text = " ".join(message.content for message in messages[-4:] if message.role == "user")
        if re.search(r"[ąćęłńóśźż]", text) or re.search(
            r"\b(tak|dzień|proszę|pokój|rezerwacj|noc)\w*", text, re.IGNORECASE
        ):
            return "pl"
        return "en"

    # ------------------------------------------------------------------ copy
    @staticmethod
    def _t(language: str, english: str, polish: str) -> str:
        return polish if language == "pl" else english

    def _text(self, content: str, language: str) -> LLMResponse:
        return LLMResponse(content=content, provider=self.provider_type, model=self.config.model)

    def _tool(self, name: str, arguments: dict, language: str) -> LLMResponse:
        return LLMResponse(
            content="",
            tool_calls=[ToolCall(id=f"demo_{uuid.uuid4().hex[:10]}", name=name, arguments=arguments)],
            provider=self.provider_type,
            model=self.config.model,
        )

    def _ask_dates(self, language: str) -> str:
        return self._t(
            language,
            "I'd be happy to help with a reservation. Could you tell me your arrival and departure dates, "
            "and how many guests will be staying?",
            "Chętnie pomogę w rezerwacji. Proszę podaj datę przyjazdu i wyjazdu oraz liczbę gości.",
        )

    def _ask_confirm(self, language: str, booking: dict) -> str:
        return self._t(
            language,
            "I've placed a temporary hold: {room}, {check_in} to {check_out}, {nights} night(s), "
            "total {total} {currency}. Your reference is {reference}. Shall I confirm the booking?",
            "Wstępnie zablokowałam dla Ciebie: {room}, od {check_in} do {check_out}, {nights} noc(y), "
            "razem {total} {currency}. Numer rezerwacji to {reference}. Czy potwierdzić rezerwację?",
        ).format(**self._booking_fields(booking, language))

    def _confirmed(self, language: str, booking: dict) -> str:
        return self._t(
            language,
            "Your booking is confirmed. Reference {reference}: {room}, {check_in} to {check_out}, "
            "total {total} {currency}. We look forward to welcoming you.",
            "Rezerwacja potwierdzona. Numer {reference}: {room}, od {check_in} do {check_out}, "
            "razem {total} {currency}. Do zobaczenia w hotelu.",
        ).format(**self._booking_fields(booking, language))

    def _cancelled(self, language: str, booking: dict) -> str:
        fee = booking.get("cancellation_fee") or "0"
        currency = booking.get("currency", "")
        return self._t(
            language,
            "Your booking {reference} has been cancelled. Cancellation fee: {fee} {currency}.",
            "Rezerwacja {reference} została anulowana. Opłata za anulowanie: {fee} {currency}.",
        ).format(reference=booking.get("reference", ""), fee=fee, currency=currency)

    def _booking_fields(self, booking: dict, language: str) -> dict:
        return {
            "reference": booking.get("reference", ""),
            "room": booking.get("room_type_name") or booking.get("room_type", ""),
            "check_in": self._fmt_date(booking.get("check_in"), language),
            "check_out": self._fmt_date(booking.get("check_out"), language),
            "nights": booking.get("nights", ""),
            "total": booking.get("total", ""),
            "currency": booking.get("currency", ""),
        }

    def _offer(self, language: str, availability: dict, slots: dict) -> str:
        options = availability.get("options") or []
        lines: list[str] = []
        for option in options[:3]:
            lines.append(
                self._t(
                    language,
                    "{name}: {total} {currency} for {nights} night(s)",
                    "{name}: {total} {currency} za {nights} noc(y)",
                ).format(
                    name=option.get("name", ""),
                    total=option.get("total", ""),
                    currency=option.get("currency", ""),
                    nights=option.get("nights", ""),
                )
            )
        intro = self._t(
            language,
            "For {check_in} to {check_out} I can offer: {options}. Which room would you like? "
            "To place a hold I also need your full name and an email address or phone number.",
            "Na termin od {check_in} do {check_out} mogę zaproponować: {options}. Który pokój wybierasz? "
            "Aby zablokować rezerwację, potrzebuję jeszcze imienia i nazwiska oraz adresu e-mail lub telefonu.",
        )
        return intro.format(
            check_in=self._fmt_date(availability.get("check_in"), language),
            check_out=self._fmt_date(availability.get("check_out"), language),
            options="; ".join(lines),
        )

    def _no_availability(self, language: str, slots: dict) -> str:
        return self._t(
            language,
            "I'm sorry, there are no rooms available for those dates. Would you like to try different dates?",
            "Przykro mi, nie mamy wolnych pokoi w tych terminach. Czy chcesz sprawdzić inne daty?",
        )

    def _error(self, language: str, result: dict) -> str:
        error = result.get("error") or {}
        message = error.get("message") or self._t(language, "unexpected error", "nieoczekiwany błąd")
        return self._t(
            language,
            "I couldn't complete that: {message}. Could you check the details and try again?",
            "Nie udało się tego dokończyć: {message}. Czy możesz sprawdzić dane i spróbować ponownie?",
        ).format(message=message)

    @staticmethod
    def _fmt_date(value: Any, language: str) -> str:
        if not value:
            return ""
        if isinstance(value, str):
            try:
                value = datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                return value
        fmt = "%d.%m.%Y" if language == "pl" else "%d %b %Y"
        return value.strftime(fmt)
