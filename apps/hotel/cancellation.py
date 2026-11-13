"""Guest cancellation policy.

The policy is intentionally small and explainable:

* a pending hold can always be released for free;
* a confirmed booking cancelled before the free window ends is free;
* a late cancellation is charged for the first ``cancellation_fee_nights``
  nights, capped at the booking total.

The preview is the single source of truth for both the API and the booking
page, so a guest always sees the same amount that will be recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import Booking

MONEY = Decimal("0.01")


@dataclass(frozen=True)
class CancellationPreview:
    is_cancellable: bool
    is_free: bool
    fee: Decimal
    currency: str
    free_until: date | None
    fee_nights: int
    reason: str
    policy_text: str

    def as_dict(self) -> dict:
        return {
            "is_cancellable": self.is_cancellable,
            "is_free": self.is_free,
            "fee": str(self.fee),
            "currency": self.currency,
            "free_until": self.free_until.isoformat() if self.free_until else None,
            "fee_nights": self.fee_nights,
            "reason": self.reason,
            "policy_text": self.policy_text,
        }


def _setting_int(name: str, default: int) -> int:
    try:
        return max(int(settings.HOTEL.get(name, default)), 0)
    except (TypeError, ValueError):
        return default


def nightly_rates(booking: Booking) -> list[Decimal]:
    """Return the stored nightly rates, falling back to an even split."""
    breakdown = booking.price_breakdown or {}
    rates: list[Decimal] = []
    for item in breakdown.get("nightly") or []:
        try:
            rates.append(Decimal(str(item.get("rate"))))
        except (InvalidOperation, TypeError, ValueError):
            continue
    if rates:
        return rates

    if booking.nights and booking.total_price:
        per_night = (booking.total_price / booking.nights).quantize(MONEY)
        return [per_night] * booking.nights
    return []


def cancellation_fee(booking: Booking, nights: int) -> Decimal:
    """Charge the first ``nights`` nightly rates, never more than the total."""
    if nights <= 0:
        return Decimal("0.00")
    rates = nightly_rates(booking)
    fee = sum(rates[:nights], Decimal("0"))
    fee = min(fee, booking.total_price)
    return max(fee, Decimal("0")).quantize(MONEY)


def cancellation_preview(booking: Booking, *, today: date | None = None) -> CancellationPreview:
    today = today or timezone.localdate()
    free_days = _setting_int("cancellation_free_days", 3)
    fee_nights = _setting_int("cancellation_fee_nights", 1)
    free_until = booking.check_in - timedelta(days=free_days)
    currency = booking.currency or settings.HOTEL["currency"]

    if booking.status in (Booking.Status.CANCELLED, Booking.Status.EXPIRED):
        return CancellationPreview(
            is_cancellable=False,
            is_free=True,
            fee=Decimal("0.00"),
            currency=currency,
            free_until=free_until,
            fee_nights=fee_nights,
            reason="already_cancelled",
            policy_text=_("This reservation is already cancelled."),
        )

    if booking.status not in (Booking.Status.PENDING, Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN):
        return CancellationPreview(
            is_cancellable=False,
            is_free=True,
            fee=Decimal("0.00"),
            currency=currency,
            free_until=free_until,
            fee_nights=fee_nights,
            reason="not_cancellable",
            policy_text=_("This reservation can no longer be cancelled."),
        )

    if booking.status == Booking.Status.PENDING:
        return CancellationPreview(
            is_cancellable=True,
            is_free=True,
            fee=Decimal("0.00"),
            currency=currency,
            free_until=free_until,
            fee_nights=fee_nights,
            reason="pending_hold",
            policy_text=_("This is a temporary hold. Cancelling it is free."),
        )

    if today <= free_until:
        return CancellationPreview(
            is_cancellable=True,
            is_free=True,
            fee=Decimal("0.00"),
            currency=currency,
            free_until=free_until,
            fee_nights=fee_nights,
            reason="free_window",
            policy_text=_(
                "Free cancellation until %(date)s. After that, %(nights)s night(s) will be charged."
            )
            % {"date": free_until.strftime("%d %b %Y"), "nights": fee_nights},
        )

    fee = cancellation_fee(booking, fee_nights)
    return CancellationPreview(
        is_cancellable=True,
        is_free=fee == 0,
        fee=fee,
        currency=currency,
        free_until=free_until,
        fee_nights=fee_nights,
        reason="late_cancellation",
        policy_text=_(
            "The free cancellation window ended on %(date)s. Cancelling now costs "
            "%(fee)s %(currency)s (%(nights)s night(s))."
        )
        % {
            "date": free_until.strftime("%d %b %Y"),
            "fee": fee,
            "currency": currency,
            "nights": fee_nights,
        },
    )
