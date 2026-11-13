"""Deterministic pricing engine.

Rates are gross (VAT included) amounts in the hotel currency. Rules are applied
per night, in priority order, with percentage rules summed and fixed rules added.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import PricingRule, Room, RoomType

MONEY = Decimal("0.01")


class StayError(ValidationError):
    """Raised when a requested stay is invalid."""

    def __init__(self, message: str, code: str = "invalid_stay"):
        super().__init__(message, code=code)


def today() -> date:
    return timezone.localdate()


def validate_stay(check_in: date, check_out: date, *, allow_past: bool = False) -> int:
    """Validate a stay and return the number of nights."""
    if not isinstance(check_in, date) or not isinstance(check_out, date):
        raise StayError(_("Both check-in and check-out dates are required."), "dates_required")
    if check_out <= check_in:
        raise StayError(_("Check-out must be after check-in."), "invalid_date_range")
    nights = (check_out - check_in).days
    max_nights = settings.HOTEL["max_booking_nights"]
    if nights > max_nights:
        raise StayError(
            _("Stays longer than %(nights)s nights are not supported.") % {"nights": max_nights},
            "stay_too_long",
        )
    if not allow_past:
        current = today()
        if check_in < current:
            raise StayError(_("Check-in cannot be in the past."), "check_in_in_past")
        latest = current + timedelta(days=settings.HOTEL["advance_booking_days"])
        if check_in > latest:
            raise StayError(
                _("Bookings can be made at most %(days)s days ahead.") % {"days": settings.HOTEL["advance_booking_days"]},
                "too_far_ahead",
            )
    return nights


@dataclass
class NightlyRate:
    night: date
    base_rate: Decimal
    rate: Decimal
    rules: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "date": self.night.isoformat(),
            "base_rate": str(self.base_rate),
            "rate": str(self.rate),
            "rules": self.rules,
        }


@dataclass
class Quote:
    room_type: RoomType
    room: Room | None
    check_in: date
    check_out: date
    nights: int
    adults: int
    children: int
    currency: str
    nightly: list[NightlyRate]
    subtotal: Decimal
    total: Decimal
    tax_rate: Decimal
    tax_included: Decimal

    def as_dict(self) -> dict:
        return {
            "room_type": self.room_type.slug,
            "room_type_name": self.room_type.name,
            "room": self.room.number if self.room else None,
            "check_in": self.check_in.isoformat(),
            "check_out": self.check_out.isoformat(),
            "nights": self.nights,
            "adults": self.adults,
            "children": self.children,
            "currency": self.currency,
            "nightly": [item.as_dict() for item in self.nightly],
            "subtotal": str(self.subtotal),
            "total": str(self.total),
            "tax_rate": str(self.tax_rate),
            "tax_included": str(self.tax_included),
        }


def applicable_rules(room_type: RoomType) -> list[PricingRule]:
    return list(
        PricingRule.objects.filter(is_active=True)
        .filter(Q(room_type=room_type) | Q(room_type__isnull=True))
        .order_by("-priority", "pk")
    )


def quote(
    room_type: RoomType,
    check_in: date,
    check_out: date,
    *,
    adults: int = 2,
    children: int = 0,
    room: Room | None = None,
    rules: list[PricingRule] | None = None,
) -> Quote:
    """Build a nightly price breakdown for a room type and date range."""
    nights = validate_stay(check_in, check_out)
    if adults < 1:
        raise StayError(_("At least one adult is required."), "adults_required")
    if adults + children > room_type.capacity:
        raise StayError(
            _("This room type sleeps up to %(capacity)s guests.") % {"capacity": room_type.capacity},
            "capacity_exceeded",
        )

    rule_list = rules if rules is not None else applicable_rules(room_type)
    base_price = room_type.base_price.quantize(MONEY)
    nightly: list[NightlyRate] = []

    for offset in range(nights):
        night = check_in + timedelta(days=offset)
        matching = [rule for rule in rule_list if rule.applies_to(night, check_in, nights)]
        percent = sum(
            (rule.amount for rule in matching if rule.adjustment_type == PricingRule.AdjustmentType.PERCENT),
            Decimal("0"),
        )
        fixed = sum(
            (rule.amount for rule in matching if rule.adjustment_type == PricingRule.AdjustmentType.FIXED),
            Decimal("0"),
        )
        rate = base_price * (Decimal("1") + percent / Decimal("100")) + fixed
        rate = max(rate, Decimal("0")).quantize(MONEY)
        nightly.append(
            NightlyRate(
                night=night,
                base_rate=base_price,
                rate=rate,
                rules=[rule.name for rule in matching],
            )
        )

    subtotal = sum((item.rate for item in nightly), Decimal("0")).quantize(MONEY)
    tax_rate = Decimal(str(settings.HOTEL["tax_rate"]))
    tax_included = (
        (subtotal * tax_rate / (Decimal("1") + tax_rate)).quantize(MONEY) if tax_rate else Decimal("0")
    )

    return Quote(
        room_type=room_type,
        room=room,
        check_in=check_in,
        check_out=check_out,
        nights=nights,
        adults=adults,
        children=children,
        currency=room_type.currency,
        nightly=nightly,
        subtotal=subtotal,
        total=subtotal,
        tax_rate=tax_rate,
        tax_included=tax_included,
    )
