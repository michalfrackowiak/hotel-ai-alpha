"""Display-currency selection for the guest-facing pages.

Stored amounts always stay in the hotel currency; this module only converts
for display, using the rates from ``settings.HOTEL["exchange_rates"]``.
"""

from __future__ import annotations

import threading
from decimal import Decimal, InvalidOperation

from django.conf import settings

_local = threading.local()


def set_current_currency(value: str) -> None:
    _local.currency = value


def get_current_currency() -> str:
    return getattr(_local, "currency", settings.HOTEL["currency"])


def supported_currencies() -> list[str]:
    return list(settings.HOTEL.get("supported_display_currencies") or [])


def exchange_rates() -> dict[str, str]:
    return settings.HOTEL.get("exchange_rates") or {}


def resolve_currency(request) -> str:
    base = settings.HOTEL["currency"]
    candidate = None
    if hasattr(request, "GET"):
        candidate = request.GET.get("currency")
    if not candidate and hasattr(request, "session"):
        candidate = request.session.get("display_currency")
    if not candidate:
        candidate = request.COOKIES.get("display_currency") if hasattr(request, "COOKIES") else None
    if candidate and candidate in supported_currencies() and candidate in exchange_rates():
        return candidate
    return base if base in supported_currencies() else (supported_currencies()[0] if supported_currencies() else base)


def convert_amount(amount, target: str) -> str:
    base = settings.HOTEL["currency"]
    if target == base or target not in exchange_rates():
        return str(amount)
    try:
        value = Decimal(str(amount)) * Decimal(str(exchange_rates()[target]))
    except (InvalidOperation, ValueError):
        return str(amount)
    return str(value.quantize(Decimal("0.01")))


class CurrencyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        currency = resolve_currency(request)
        request.display_currency = currency
        set_current_currency(currency)
        if hasattr(request, "session"):
            request.session["display_currency"] = currency
        response = self.get_response(request)
        response.set_cookie("display_currency", currency, max_age=60 * 60 * 24 * 365, samesite="Lax")
        return response
