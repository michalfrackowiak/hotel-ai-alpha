"""Offline demo policy.

This connector implements a small, deterministic receptionist so the alpha can
be demonstrated (and tested) without any API key. It follows the same
conversation scenario as the LLM connectors: language selection, check-in,
check-out/duration, guest count, availability, room selection, confirmation,
delivery method (email or SMS), contact collection and a warm closing.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from django.conf import settings
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

POLISH_ORDINALS = {
    "pierwszego": 1, "drugiego": 2, "trzeciego": 3, "czwartego": 4,
    "piątego": 5, "szóstego": 6, "siódmego": 7, "ósmego": 8,
    "dziewiątego": 9, "dziesiątego": 10, "jedenastego": 11,
    "dwunastego": 12, "trzynastego": 13, "czternastego": 14,
    "piętnastego": 15, "szesnastego": 16, "siedemnastego": 17,
    "osiemnastego": 18, "dziewiętnastego": 19, "dwudziestego": 20,
    "dwudziestego pierwszego": 21, "dwudziestego drugiego": 22,
    "dwudziestego trzeciego": 23, "dwudziestego czwartego": 24,
    "dwudziestego piątego": 25, "dwudziestego szóstego": 26,
    "dwudziestego siódmego": 27, "dwudziestego ósmego": 28,
    "dwudziestego dziewiątego": 29, "trzydziestego": 30,
    "trzydziestego pierwszego": 31,
}

WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "a": 1, "an": 1,
    "jeden": 1, "jedna": 1, "jedno": 1, "dwa": 2, "dwie": 2, "dwoje": 2,
    "trzy": 3, "cztery": 4, "pięć": 5, "piec": 5, "sześć": 6, "szesc": 6, "siedem": 7,
    "osiem": 8, "dziewięć": 9, "dziewiec": 9, "dziesięć": 10, "dziesiec": 10,
    "jedenaście": 11, "jedenascie": 11, "dwanaście": 12, "dwanascie": 12,
    "trzynaście": 13, "trzynascie": 13, "czternaście": 14, "czternascie": 14,
    "piętnaście": 15, "pietnascie": 15,
}

NUMBER_WORD_PATTERN = "|".join(sorted((re.escape(word) for word in WORD_NUMBERS), key=len, reverse=True))

AFFIRMATIVE = re.compile(
    r"\b(yes|yeah|yep|sure|ok|okay|confirm|confirmed|book it|go ahead|"
    r"tak|potwierdzam|zgoda|rezerwuj|proszę|dobrze)\b",
    re.IGNORECASE,
)
NEGATIVE = re.compile(
    r"\b(no|nope|not now|later|nie|nie trzeba|nie chcę|rezygnuję)\b",
    re.IGNORECASE,
)
CANCEL_INTENT = re.compile(r"\b(cancel|cancelled|anuluj|anulować|odwołaj|odwołać)\b", re.IGNORECASE)
HANDOVER_INTENT = re.compile(
    r"\b(complaint|complain|invoice|refund|reklamac\w*|skarg\w*|faktur\w*|zwrot\w*|"
    r"praca|job|konferencj\w*|event|grupa|group|parking|recepcj\w*|human|człowiek|"
    r"pracownik|inna sprawa|other matter)\b",
    re.IGNORECASE,
)
EMAIL_CHOICE = re.compile(r"\b(e-?mail|mail|emailem|mailem)\b", re.IGNORECASE)
SMS_CHOICE = re.compile(r"\b(sms|phone|telefon|numer|komórk\w*|text|tekst)\b", re.IGNORECASE)
REFERENCE = re.compile(r"\bHA-[A-Z0-9]{6}\b", re.IGNORECASE)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
PHONE = re.compile(r"(?<![\d])(\+?\d[\d\s()\-]{7,}\d)(?![\d])")
GUESTS = re.compile(
    r"(\d+)\s*(?:adult|adults|guest|guests|person|people|osob[a-y]?|goś[ćc]i?)", re.IGNORECASE
)
NIGHTS = re.compile(r"(\d+)\s*(?:night|nights|noc|noce|nocy)", re.IGNORECASE)
FOR_N = re.compile(r"\b(?:for|dla)\s+(\d+)\b", re.IGNORECASE)
BARE_NUMBER = re.compile(r"^\s*(\d{1,2})\s*$")
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
        slots = self._extract_slots(history, last_user, messages)

        results = self._tool_results(messages)
        availability = results.get("check_availability")
        created = results.get("create_booking")
        confirmed = results.get("confirm_booking")
        cancelled = results.get("cancel_booking")
        email_result = results.get("send_confirmation_email")
        sms_result = results.get("send_confirmation_sms")
        handover = results.get("transfer_to_human")

        if cancelled:
            if cancelled.get("ok"):
                return self._text(self._cancelled(language, cancelled.get("booking", {})), language)
            return self._text(self._error(language, cancelled), language)

        reference_match = REFERENCE.search(history)
        if reference_match and CANCEL_INTENT.search(last_user):
            return self._tool(
                "cancel_booking",
                {"reference": reference_match.group(0).upper(), "reason": "guest_request"},
                language,
            )

        if handover is not None:
            if handover.get("ok"):
                return self._text(self._handover(language), language)
            return self._text(self._error(language, handover), language)

        if email_result is not None:
            if email_result.get("ok"):
                return self._text(self._closing_email(language, email_result.get("email", "")), language)
            return self._text(self._error(language, email_result), language)

        if sms_result is not None:
            if sms_result.get("ok"):
                return self._text(self._closing_sms(language, sms_result.get("phone", "")), language)
            return self._text(self._error(language, sms_result), language)

        if confirmed is not None:
            if not confirmed.get("ok"):
                return self._text(self._error(language, confirmed), language)
            booking = confirmed.get("booking", {})
            method = self._delivery_method(messages, history, booking)
            if method == "sms":
                phone = slots["phone"] or booking.get("guest_phone") or ""
                if not phone:
                    return self._text(self._ask_phone(language), language)
                return self._tool(
                    "send_confirmation_sms",
                    {"reference": booking.get("reference"), "phone": phone},
                    language,
                )
            email = slots["email"] or booking.get("guest_email") or ""
            if not email:
                return self._text(self._ask_email_address(language), language)
            return self._tool(
                "send_confirmation_email",
                {"reference": booking.get("reference"), "email": email},
                language,
            )

        if created is not None:
            if not created.get("ok"):
                return self._text(self._error(language, created), language)
            booking = created.get("booking", {})
            return self._tool("confirm_booking", {"reference": booking.get("reference")}, language)

        # Step 2: check-in date, with an explicit handover path.
        if not slots["check_in"]:
            if self._is_language_selection(last_user):
                return self._text(self._ask_check_in(language), language)
            if HANDOVER_INTENT.search(last_user or ""):
                return self._tool("transfer_to_human", {"reason": last_user[:200]}, language)
            return self._text(self._ask_check_in(language), language)

        # Step 3: check-out date or duration.
        if not slots["check_out"]:
            return self._text(self._ask_check_out(language), language)

        # Step 4: guest count.
        if not slots["guests_explicit"]:
            return self._text(self._ask_guests(language), language)

        availability_data = self._fresh_availability(availability, slots)
        if availability_data is None:
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

        options = (availability_data or {}).get("options") or []
        if not options:
            return self._text(self._no_availability(language), language)

        # Step 5: room selection.
        room_choice = self._room_choice_after(messages, options)
        if not room_choice:
            return self._text(self._offer(language, availability_data), language)

        # Step 6: read the details back and ask for explicit confirmation.
        confirmation = self._confirmation_state(messages)
        if confirmation == "declined":
            return self._text(self._declined(language), language)
        if confirmation != "confirmed":
            return self._text(
                self._ask_confirm(language, availability_data, room_choice, slots),
                language,
            )

        # Step 7: delivery method and contact collection.
        method = self._delivery_method(messages, history)
        if not method:
            return self._text(self._ask_delivery_method(language), language)

        if method == "sms":
            contact = slots["phone"]
            if not contact:
                return self._text(self._ask_phone(language), language)
        else:
            contact = slots["email"]
            if not contact:
                return self._text(self._ask_email_address(language), language)

        if not slots["first_name"]:
            return self._text(self._ask_name(language), language)

        return self._tool(
            "create_booking",
            {
                "room_type": room_choice,
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

    @staticmethod
    def _is_negative(text: str) -> bool:
        return bool(NEGATIVE.search(text or ""))

    # ------------------------------------------------------------------ slots
    def _extract_slots(
        self,
        history: str,
        latest: str,
        messages: list[ChatMessage] | None = None,
    ) -> dict[str, Any]:
        messages = messages or []
        latest_in, latest_out = self._extract_dates(latest)
        history_in, history_out = self._extract_dates(history)
        asked_check_out = self._asked_check_out(messages)

        if asked_check_out and latest_in and not latest_out:
            # A single date after "what is your departure date?" is the check-out.
            check_in, check_out = history_in, latest_in
        elif asked_check_out and not latest_in and history_in:
            # "Three nights" as the answer to the departure question.
            nights = self._duration_nights(latest.lower())
            check_in = history_in
            check_out = history_in + timedelta(days=nights) if nights else None
        else:
            check_in = latest_in or history_in
            check_out = latest_out
            if not check_out and history_out and check_in and history_out > check_in:
                check_out = history_out
        if check_in and not check_out:
            nights = self._duration_nights(latest.lower())
            if nights:
                check_out = check_in + timedelta(days=nights)

        email = EMAIL.search(latest) or EMAIL.search(history)
        phone_text = self._strip_dates(latest) or self._strip_dates(history)
        phone = PHONE.search(phone_text) if phone_text else None
        guests = self._first_int(GUESTS, latest) or self._first_int(GUESTS, history)
        if guests is None:
            guests = self._guest_count_from_history(messages)
        guests_explicit = guests is not None
        first_name, last_name = self._extract_name(history, latest)

        return {
            "check_in": check_in,
            "check_out": check_out,
            "email": email.group(0) if email else "",
            "phone": phone.group(1).strip() if phone else "",
            "guests": max(1, min(guests or 2, 8)),
            "guests_explicit": guests_explicit,
            "first_name": first_name,
            "last_name": last_name,
            "room_type": self._extract_room_type(history),
        }

    def _extract_dates(self, text: str) -> tuple[date | None, date | None]:
        if not text:
            return None, None
        lowered = text.lower()
        found: list[date] = []
        today = timezone.localdate()

        # "12–15 December 2026" / "12 do 15 grudnia 2026" (with year)
        for match in re.finditer(
            r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:-|–|to|do)\s*(\d{1,2})(?:st|nd|rd|th)?\s+"
            r"(?:of\s+)?([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+)\s+(20\d{2})\b",
            lowered,
        ):
            month = MONTHS.get(match.group(3))
            if month:
                found.append(self._safe_date(int(match.group(4)), month, int(match.group(1))))
                found.append(self._safe_date(int(match.group(4)), month, int(match.group(2))))
                break

        # "12–15 December" / "12 do 15 listopada" (no year)
        if not found:
            for match in re.finditer(
                r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:-|–|to|do)\s*(\d{1,2})(?:st|nd|rd|th)?\s+"
                r"(?:of\s+)?([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+)\b",
                lowered,
            ):
                month = MONTHS.get(match.group(3))
                if month:
                    found.append(self._date_without_year(today, month, int(match.group(1))))
                    found.append(self._date_without_year(today, month, int(match.group(2))))
                    break

        # "20.11-22.11" or "20/11 to 22/11" (day.month/day.month, no year)
        if not found:
            for match in re.finditer(
                r"\b(\d{1,2})[./](\d{1,2})\s*(?:-|–|to|do)\s*(\d{1,2})[./](\d{1,2})\b",
                lowered,
            ):
                first = self._date_without_year(today, int(match.group(2)), int(match.group(1)))
                second = self._date_without_year(today, int(match.group(4)), int(match.group(3)))
                if first and second:
                    found.extend([first, second])
                    break

        if not found:
            for match in re.finditer(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", lowered):
                found.append(self._safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
            for match in re.finditer(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b", lowered):
                found.append(self._safe_date(int(match.group(3)), int(match.group(2)), int(match.group(1))))
            for match in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", lowered):
                # US-style MM/DD/YYYY first; fall back to DD/MM/YYYY.
                us = self._safe_date(int(match.group(3)), int(match.group(1)), int(match.group(2)))
                if us is not None:
                    found.append(us)
                else:
                    eu = self._safe_date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
                    if eu is not None:
                        found.append(eu)
            for match in re.finditer(
                r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+)\s+(20\d{2})\b",
                lowered,
            ):
                month = MONTHS.get(match.group(2))
                if month:
                    found.append(self._safe_date(int(match.group(3)), month, int(match.group(1))))
            for match in re.finditer(
                r"\b([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(20\d{2})\b",
                lowered,
            ):
                month = MONTHS.get(match.group(1))
                if month:
                    found.append(self._safe_date(int(match.group(3)), month, int(match.group(2))))

        # Dates without explicit year, e.g. "20th of November", "20 listopada".
        if not found:
            for match in re.finditer(
                r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+)\b",
                lowered,
            ):
                month = MONTHS.get(match.group(2))
                if month:
                    found.append(self._date_without_year(today, month, int(match.group(1))))
            for match in re.finditer(
                r"\b([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+)\s+(\d{1,2})(?:st|nd|rd|th)?\b",
                lowered,
            ):
                month = MONTHS.get(match.group(1))
                if month:
                    found.append(self._date_without_year(today, month, int(match.group(2))))

        # Polish ordinal day names, with or without a month.
        if not found:
            for word, day in POLISH_ORDINALS.items():
                match = re.search(rf"\b{word}\b(?:\s+([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+))?", lowered)
                if not match:
                    continue
                month = MONTHS.get(match.group(1) or "")
                found.append(self._date_without_year(today, month or today.month, day))

        # "12.07" or "12/07" (day.month) without a year.
        if not found:
            for match in re.finditer(r"\b(\d{1,2})[./](\d{1,2})\b", lowered):
                day, month = int(match.group(1)), int(match.group(2))
                if 1 <= day <= 31 and 1 <= month <= 12:
                    found.append(self._date_without_year(today, month, day))

        dates: list[date] = []
        for item in found:
            if item and item not in dates:
                dates.append(item)

        if not dates:
            has_tonight = bool(re.search(r"\btonight\b", lowered))
            has_today_word = bool(re.search(r"\btoday\b|\bdzi(?:ś|siaj)\b", lowered))
            has_today = has_today_word or has_tonight
            has_tomorrow = bool(re.search(r"\btomorrow\b|\bjutr[ao]\b", lowered))
            has_day_after = bool(re.search(r"\bday after tomorrow\b|\bpojutrze\b", lowered))
            if has_today and has_tomorrow:
                dates = [today, today + timedelta(days=1)]
            elif has_day_after:
                dates = [today + timedelta(days=2)]
            elif has_tomorrow:
                dates = [today + timedelta(days=1)]
            elif has_today:
                dates = [today]
            elif re.search(r"\bnext week\b|\bza tydzie(?:ń|n)\b", lowered):
                dates = [today + timedelta(days=7)]
            else:
                relative = self._relative_date(lowered, today)
                if relative is not None:
                    dates = [relative]
                else:
                    weekday = self._next_weekday(lowered, today)
                    if weekday is not None:
                        dates = [weekday]

        # A bare English ordinal such as "the 20th" means the current month.
        if not dates:
            match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", lowered)
            if match:
                dates = [self._date_without_year(today, today.month, int(match.group(1)))]

        dates = [item for item in dates if item is not None]
        if len(dates) >= 2:
            return dates[0], dates[1]
        if len(dates) == 1:
            nights = self._duration_nights(lowered)
            if nights:
                return dates[0], dates[0] + timedelta(days=nights)
            return dates[0], None
        return None, None


    @classmethod
    def _relative_date(cls, lowered: str, today: date) -> date | None:
        # "in 3 days", "in two weeks", "za 3 dni", "za dwa tygodnie"
        patterns = (
            r"\bin\s+(?P<num>\d+|" + NUMBER_WORD_PATTERN + r")\s+(?P<unit>day|days|week|weeks)\b",
            r"\bza\s+(?P<num>\d+|" + NUMBER_WORD_PATTERN + r")\s+(?P<unit>dni|dnia|dzie[nń]|tygodni(?:e|a)?|tygodnie|tydzie[nń])\b",
        )
        for pattern in patterns:
            match = re.search(pattern, lowered, re.IGNORECASE)
            if not match:
                continue
            amount = cls._parse_number(match.group("num"))
            if amount is None or amount < 1 or amount > 60:
                continue
            unit = match.group("unit").lower()
            if unit.startswith("week") or unit.startswith("tygod") or unit.startswith("tydzi"):
                delta = amount * 7
            else:
                delta = amount
            return today + timedelta(days=delta)
        return None

    @staticmethod
    def _next_weekday(lowered: str, today: date) -> date | None:
        weekdays = {
            "monday": 0, "mon": 0, "poniedziałek": 0, "poniedzialek": 0,
            "tuesday": 1, "tue": 1, "tues": 1, "wtorek": 1,
            "wednesday": 2, "wed": 2, "środa": 2, "sroda": 2, "środę": 2, "srode": 2,
            "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "czwartek": 3,
            "friday": 4, "fri": 4, "piątek": 4, "piatek": 4,
            "saturday": 5, "sat": 5, "sobota": 5, "sobotę": 5, "sobote": 5,
            "sunday": 6, "sun": 6, "niedziela": 6, "niedzielę": 6, "niedziele": 6,
        }
        match = re.search(
            r"\b(?:next|on|this|coming|w|we|w\s+nastepny|w\s+następny|w\s+najbliższy|w\s+najblizszy)\s+"
            r"([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]+)\b",
            lowered,
        )
        if not match:
            return None
        target = weekdays.get(match.group(1))
        if target is None:
            return None
        days_ahead = (target - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        return today + timedelta(days=days_ahead)

    @staticmethod
    def _date_without_year(today: date, month: int, day: int) -> date | None:
        try:
            candidate = date(today.year, month, day)
        except ValueError:
            return None
        if candidate < today:
            try:
                return date(today.year + 1, month, day)
            except ValueError:
                return None
        return candidate

    @classmethod
    def _duration_nights(cls, text: str) -> int | None:
        source = text or ""
        # Common approximate durations.
        if re.search(r"\b(?:a\s+)?couple\s+of\s+(?:nights?|days?)\b", source, re.IGNORECASE):
            return 2
        if re.search(r"\b(?:a\s+)?few\s+(?:nights?|days?)\b", source, re.IGNORECASE):
            return 3
        # Prefer explicit "for/stay/na N nights/days" phrasing as a stay length.
        for match in re.finditer(
            rf"\b(?:for|stay(?:ing)?|na|pobyt|przez)\s+(?P<num>\d+|{NUMBER_WORD_PATTERN})\s*"
            r"(?:night|nights|noc|noce|nocy|day|days|dni|dzie(?:ń|ni))\b",
            source,
            re.IGNORECASE,
        ):
            value = cls._parse_number(match.group("num"))
            if value is not None and 1 <= value <= 30:
                return value
        # "three-night stay", "3 night stay", "a two-nights stay"
        for match in re.finditer(
            rf"\b(?:(?:a|an)\s+)?(?P<num>\d+|{NUMBER_WORD_PATTERN})[-\s]+(?:night|nights|noc|noce|nocy)\s+stay\b",
            source,
            re.IGNORECASE,
        ):
            value = cls._parse_number(match.group("num"))
            if value is not None and 1 <= value <= 30:
                return value
        for match in re.finditer(
            rf"\b(?P<num>\d+|{NUMBER_WORD_PATTERN})\s*(?:night|nights|noc|noce|nocy)\b",
            source,
            re.IGNORECASE,
        ):
            value = cls._parse_number(match.group("num"))
            if value is not None and 1 <= value <= 30:
                return value
        if re.search(
            rf"\b(?:for|na)\s+(?:a|one|{NUMBER_WORD_PATTERN})\s+week(?:s)?\b|"
            r"\b(?:for|na)\s+tydzie(?:ń|n)\b|\ba\s+week\b|\btydzie(?:ń|n)\b",
            source,
            re.IGNORECASE,
        ):
            return 7
        return None

    @staticmethod
    def _parse_number(token: str | None) -> int | None:
        if token is None:
            return None
        raw = str(token).strip().lower()
        if raw.isdigit():
            return int(raw)
        return WORD_NUMBERS.get(raw)

    @staticmethod
    def _safe_date(year: int, month: int, day: int) -> date | None:
        try:
            return date(year, month, day)
        except ValueError:
            return None

    @staticmethod
    def _strip_dates(text: str) -> str:
        cleaned = re.sub(r"\b20\d{2}-\d{1,2}-\d{1,2}\b", " ", text or "")
        cleaned = re.sub(r"\b\d{1,2}\.\d{1,2}\.20\d{2}\b", " ", cleaned)
        cleaned = re.sub(r"\b\d{1,2}/\d{1,2}/20\d{2}\b", " ", cleaned)
        return cleaned

    @staticmethod
    def _first_int(pattern: re.Pattern, text: str) -> int | None:
        match = pattern.search(text or "")
        return int(match.group(1)) if match else None

    @staticmethod
    def _bare_count(text: str) -> int | None:
        match = BARE_NUMBER.match(text or "")
        if match:
            value = int(match.group(1))
            return value if 1 <= value <= 8 else None
        lowered = (text or "").lower().strip(" .,!?:;")
        for word, value in WORD_NUMBERS.items():
            if re.search(rf"\b{word}\b", lowered):
                return value
        return None

    @staticmethod
    def _asked_guests(messages: list[ChatMessage]) -> bool:
        for message in reversed(messages):
            if message.role == "assistant" and (message.content or "").strip():
                text = (message.content or "").lower()
                return "number of guests" in text or "liczbę osób" in text
        return False

    @staticmethod
    def _guest_count_from_history(messages: list[ChatMessage]) -> int | None:
        # Recover a guest count that was given as a bare number (or word) in
        # reply to the "number of guests" question, even after the conversation
        # has moved on to availability, room selection or confirmation. Scanning
        # the full message list (rather than only the last assistant turn) keeps
        # the count stable across turns.
        result: int | None = None
        i = 0
        length = len(messages)
        while i < length:
            message = messages[i]
            if message.role == "assistant" and (
                "number of guests" in (message.content or "").lower()
                or "liczbę osób" in (message.content or "").lower()
            ):
                j = i + 1
                while j < length and messages[j].role != "assistant":
                    if messages[j].role == "user":
                        count = DemoProvider._bare_count(messages[j].content or "")
                        if count is not None:
                            result = count
                    j += 1
            i += 1
        return result

    @staticmethod
    def _asked_check_out(messages: list[ChatMessage]) -> bool:
        for message in reversed(messages):
            if message.role == "assistant" and (message.content or "").strip():
                text = (message.content or "").lower()
                return "departure date" in text or "datę wyjazdu" in text
        return False

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

        for source in (latest, text):
            email_match = EMAIL.search(source or "")
            if email_match:
                name = self._name_before(source or "", email_match.start())
                if name[0]:
                    return name
            phone_match = PHONE.search(self._strip_dates(source or ""))
            if phone_match:
                name = self._name_before(source or "", phone_match.start())
                if name[0]:
                    return name
        return "", ""

    @staticmethod
    def _name_before(text: str, end: int) -> tuple[str, str]:
        prefix = text[:end]
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

    def _room_choice_after(self, messages: list[ChatMessage], options: list[dict]) -> str:
        index = None
        for position, message in enumerate(messages):
            if message.role == "tool" and message.name == "check_availability":
                index = position
        if index is None:
            return ""
        for message in reversed(messages[index + 1 :]):
            if message.role != "user":
                continue
            choice = self._pick_room_type(message.content or "", options)
            if choice:
                return choice
        return ""

    def _pick_room_type(self, latest: str, options: list[dict]) -> str:
        lowered = (latest or "").lower()
        available = [option.get("room_type") for option in options if option.get("room_type")]

        for option in options:
            slug = option.get("room_type") or ""
            name = (option.get("name") or "").lower()
            if slug and slug.replace("-", " ") in lowered:
                return slug
            if name and name in lowered:
                return slug

        wants_suite = any(word in lowered for word in ("suite", "apartament", "deluxe", "luksus"))
        wants_standard = any(word in lowered for word in ("standard", "podstaw", "double"))
        for option in options:
            slug = option.get("room_type") or ""
            name = (option.get("name") or "").lower()
            if wants_suite and ("suite" in name or "deluxe" in slug):
                return slug
            if wants_standard and ("standard" in name or "double" in slug):
                return slug

        if re.search(r"\b(first|pierwsz\w*)\b", lowered) and len(options) >= 1:
            return available[0]
        if re.search(r"\b(second|drug\w*)\b", lowered) and len(options) >= 2:
            return available[1]
        if re.search(r"\b(third|trzec\w*)\b", lowered) and len(options) >= 3:
            return available[2]

        number = BARE_NUMBER.match(lowered.strip())
        if number:
            position = int(number.group(1))
            if 1 <= position <= len(available):
                return available[position - 1]
        return ""

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

    # --------------------------------------------------------------- delivery
    @staticmethod
    def _method_from_text(text: str) -> str:
        lowered = (text or "").lower()
        sms = SMS_CHOICE.search(lowered)
        email = EMAIL_CHOICE.search(lowered)
        if sms and email:
            return "sms" if sms.start() > email.start() else "email"
        if sms:
            return "sms"
        if email:
            return "email"
        return ""

    def _delivery_method(
        self,
        messages: list[ChatMessage],
        history: str,
        booking: dict | None = None,
    ) -> str:
        index = None
        for position, message in enumerate(messages):
            if message.role != "assistant":
                continue
            text = (message.content or "").lower()
            if "sms" in text and ("email" in text or "e-mail" in text):
                index = position
        if index is not None:
            for message in messages[index + 1 :]:
                if message.role != "user":
                    continue
                method = self._method_from_text(message.content or "")
                if method:
                    return method
                if EMAIL.search(message.content or ""):
                    return "email"
                if PHONE.search(self._strip_dates(message.content or "")):
                    return "sms"
        method = self._method_from_text(history)
        if method:
            return method
        if booking:
            if booking.get("guest_phone") and not booking.get("guest_email"):
                return "sms"
            if booking.get("guest_email") and not booking.get("guest_phone"):
                return "email"
        return ""

    @staticmethod
    def _is_language_selection(text: str) -> bool:
        return bool(
            re.search(r"^\s*(1|2)\s*$", text or "")
            or re.search(r"\b(english|polish|polski|po polsku)\b", text or "", re.IGNORECASE)
        )

    @staticmethod
    def _is_confirm_prompt(text: str) -> bool:
        lowered = (text or "").lower()
        return "shall i confirm" in lowered or "czy potwierdzić" in lowered

    def _confirmation_state(self, messages: list[ChatMessage]) -> str:
        index = None
        for position, message in enumerate(messages):
            if message.role == "assistant" and self._is_confirm_prompt(message.content or ""):
                index = position
        if index is None:
            return "not_asked"
        for message in messages[index + 1 :]:
            if message.role != "user":
                continue
            text = message.content or ""
            if self._is_negative(text):
                return "declined"
            if self._is_affirmative(text):
                return "confirmed"
            return "unclear"
        return "asked"

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

    def _ask_check_in(self, language: str) -> str:
        return self._t(
            language,
            "If you would like to check availability, prices or make a reservation, please tell me your "
            "planned arrival date. If you are calling about another matter, stay on the line and I will "
            "connect you with a receptionist.",
            "Jeżeli chcesz dowiedzieć się o dostępność, ceny lub dokonać rezerwacji, proszę podaj planowany "
            "termin przyjazdu. Jeżeli dzwonisz w innej sprawie, pozostań na linii lub napisz, a połączę Cię "
            "z pracownikiem recepcji.",
        )

    def _ask_check_out(self, language: str) -> str:
        return self._t(
            language,
            "Please give me your departure date or tell me how many nights you plan to stay.",
            "Podaj datę wyjazdu lub informację, na ile nocy planujesz pobyt.",
        )

    def _ask_guests(self, language: str) -> str:
        return self._t(
            language,
            "Please tell me the total number of guests (adults and children) for the reservation.",
            "Podaj łączną liczbę osób (dorosłych i dzieci), dla których mamy przygotować rezerwację.",
        )

    def _offer(self, language: str, availability: dict) -> str:
        options = availability.get("options") or []
        lines: list[str] = []
        for option in options[:3]:
            lines.append(
                self._t(
                    language,
                    "{name}: {total} {currency} total for {nights} night(s)",
                    "{name}: {total} {currency} za pobyt ({nights} noc(y))",
                ).format(
                    name=option.get("name", ""),
                    total=option.get("total", ""),
                    currency=option.get("currency", ""),
                    nights=option.get("nights", ""),
                )
            )
        intro = self._t(
            language,
            "I have checked availability. For {check_in} to {check_out} we have: {options}. "
            "Which option would you like to select?",
            "Sprawdziłam dostępność. Na wybrany termin mamy następujące wolne pokoje: {options}. "
            "Którą opcję chciałbyś wybrać?",
        )
        return intro.format(
            check_in=self._fmt_date(availability.get("check_in"), language),
            check_out=self._fmt_date(availability.get("check_out"), language),
            options="; ".join(lines),
        )

    def _ask_confirm(
        self,
        language: str,
        availability: dict,
        room_choice: str,
        slots: dict,
    ) -> str:
        option = next(
            (item for item in availability.get("options") or [] if item.get("room_type") == room_choice),
            {},
        )
        return self._t(
            language,
            "You have selected {room} from {check_in} to {check_out} for {guests} guests, total "
            "{total} {currency}. Shall I confirm your reservation?",
            "Wybrałeś {room} w terminie od {check_in} do {check_out} dla {guests} osób, łącznie "
            "{total} {currency}. Czy potwierdzić rezerwację?",
        ).format(
            room=option.get("name") or room_choice,
            check_in=self._fmt_date(availability.get("check_in"), language),
            check_out=self._fmt_date(availability.get("check_out"), language),
            guests=slots["guests"],
            total=option.get("total", ""),
            currency=option.get("currency", ""),
        )

    def _ask_delivery_method(self, language: str) -> str:
        return self._t(
            language,
            "Your reservation is ready to be confirmed. Would you like the confirmation sent to your "
            "email address or by SMS to your phone number?",
            "Rezerwacja jest gotowa do potwierdzenia. Czy chcesz, aby potwierdzenie zostało wysłane "
            "na adres e-mail, czy na numer telefonu przez SMS?",
        )

    def _ask_email_address(self, language: str) -> str:
        return self._t(
            language,
            "Please type or dictate your email address, and tell me your first and last name.",
            "Proszę podaj lub podyktuj swój adres e-mail oraz imię i nazwisko.",
        )

    def _ask_phone(self, language: str) -> str:
        return self._t(
            language,
            "Please give me the phone number for the SMS confirmation, and tell me your first and last name.",
            "Proszę podaj numer telefonu, na który mamy wysłać potwierdzenie SMS-em, oraz imię i nazwisko.",
        )

    def _ask_name(self, language: str) -> str:
        return self._t(
            language,
            "Thank you. What is your first and last name?",
            "Dziękuję. Jak masz na imię i nazwisko?",
        )

    def _closing_email(self, language: str, email: str) -> str:
        hotel = settings.HOTEL["name"]
        return self._t(
            language,
            "Your reservation is fully confirmed. We have sent the details to {email}. "
            "Thank you for choosing {hotel} and have a wonderful stay!",
            "Twoja rezerwacja została w pełni potwierdzona. Wysłaliśmy szczegóły na adres {email}. "
            "Dziękujemy za wybór {hotel} i życzymy miłego pobytu!",
        ).format(email=email, hotel=hotel)

    def _closing_sms(self, language: str, phone: str) -> str:
        hotel = settings.HOTEL["name"]
        return self._t(
            language,
            "Your reservation is fully confirmed. We have sent a confirmation text message to {phone}. "
            "Thank you for choosing {hotel} and see you soon!",
            "Twoja rezerwacja została w pełni potwierdzona. Wysłaliśmy SMS z potwierdzeniem na numer {phone}. "
            "Dziękujemy za wybór {hotel} i do zobaczenia!",
        ).format(phone=phone, hotel=hotel)

    def _declined(self, language: str) -> str:
        hotel = settings.HOTEL["name"]
        return self._t(
            language,
            "Thank you for your call, and feel free to contact {hotel} again. Goodbye!",
            "Dziękuję za rozmowę i zapraszam do ponownego kontaktu z {hotel}. Do widzenia!",
        ).format(hotel=hotel)

    def _handover(self, language: str) -> str:
        return self._t(
            language,
            "Redirecting you to a reception employee. Please hold.",
            "Przekierowuję do pracownika recepcji. Proszę czekać.",
        )

    def _cancelled(self, language: str, booking: dict) -> str:
        fee = booking.get("cancellation_fee") or "0"
        currency = booking.get("currency", "")
        return self._t(
            language,
            "Your booking {reference} has been cancelled. Cancellation fee: {fee} {currency}.",
            "Rezerwacja {reference} została anulowana. Opłata za anulowanie: {fee} {currency}.",
        ).format(reference=booking.get("reference", ""), fee=fee, currency=currency)

    def _no_availability(self, language: str) -> str:
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
