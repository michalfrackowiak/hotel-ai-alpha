"""Template helpers for multi-currency display."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django import template
from django.conf import settings

from ..currency import convert_amount, exchange_rates, get_current_currency

register = template.Library()


@register.filter
def money(value):
    """Render ``value`` in the hotel currency with an indicative conversion."""
    base = settings.HOTEL["currency"]
    target = get_current_currency()
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return value
    if target == base or target not in exchange_rates():
        return f"{amount:.2f} {base}"
    converted = convert_amount(amount, target)
    return f"{amount:.2f} {base} (~{Decimal(converted):.2f} {target})"


@register.filter
def converted(value, target):
    from ..currency import convert_amount

    return convert_amount(value, target)


@register.simple_tag(takes_context=True)
def currency_code(context):
    return context.get("display_currency", settings.HOTEL["currency"])
