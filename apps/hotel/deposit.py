"""Guest deposit/prepayment policy.

The policy is intentionally small and explainable: a hotel-level setting can
require no deposit, a fixed amount, or a percentage of the booking total. The
summary is the single source of truth for guest-facing views, confirmations and
the API so they all communicate the same amounts.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from django.conf import settings
from django.utils.translation import gettext as _

if TYPE_CHECKING:
    from .models import Booking

MONEY = Decimal("0.01")


@dataclass(frozen=True)
class DepositSummary:
    policy: str
    deposit_amount: Decimal
    amount_paid: Decimal
    amount_due: Decimal
    amount_due_now: Decimal
    currency: str
    policy_text: str

    @property
    def applies(self) -> bool:
        return self.policy != "none" and self.deposit_amount > Decimal("0.00")

    @property
    def is_deposit_met(self) -> bool:
        return self.amount_paid >= self.deposit_amount

    def as_dict(self) -> dict:
        return {
            "policy": self.policy,
            "applies": self.applies,
            "deposit_amount": str(self.deposit_amount),
            "amount_paid": str(self.amount_paid),
            "amount_due": str(self.amount_due),
            "amount_due_now": str(self.amount_due_now),
            "currency": self.currency,
            "is_deposit_met": self.is_deposit_met,
            "policy_text": self.policy_text,
        }


def _policy() -> str:
    value = str(settings.HOTEL.get("deposit_policy", "none") or "none").strip().lower()
    return value if value in {"none", "percent", "fixed"} else "none"


def _policy_value() -> Decimal:
    raw = settings.HOTEL.get("deposit_value", 0)
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    return max(value, Decimal("0"))


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY)


def deposit_amount_for_total(total: Decimal) -> tuple[str, Decimal]:
    """Return (policy, deposit_amount) for a booking total."""
    policy = _policy()
    value = _policy_value()
    total = max(total or Decimal("0"), Decimal("0"))
    if policy == "percent":
        amount = (total * value / Decimal("100")).quantize(MONEY)
    elif policy == "fixed":
        amount = value.quantize(MONEY)
    else:
        amount = Decimal("0.00")
    amount = min(max(amount, Decimal("0.00")), total)
    return policy, amount


def deposit_summary(booking: Booking) -> DepositSummary:
    total = max(booking.total_price or Decimal("0"), Decimal("0"))
    paid = max(booking.amount_paid or Decimal("0"), Decimal("0"))
    balance_due = max(total - paid, Decimal("0")).quantize(MONEY)
    policy, amount = deposit_amount_for_total(total)
    currency = booking.currency or settings.HOTEL["currency"]

    if policy == "none" or amount <= Decimal("0.00"):
        policy = "none"
        amount = Decimal("0.00")
        due_now = balance_due
        policy_text = _("No deposit is required. The balance due is %(amount)s %(currency)s.") % {
            "amount": balance_due,
            "currency": currency,
        }
    else:
        deposit_due = min(max(amount - paid, Decimal("0")).quantize(MONEY), balance_due)
        due_now = deposit_due
        if policy == "percent":
            value = _policy_value().normalize()
            if due_now > Decimal("0.00"):
                policy_text = _(
                    "A deposit of %(deposit)s %(currency)s (%(percent)s%% of the total) is required. "
                    "Amount due now: %(due_now)s %(currency)s. Remaining balance: %(balance)s %(currency)s."
                ) % {
                    "deposit": amount,
                    "percent": value,
                    "due_now": due_now,
                    "balance": balance_due,
                    "currency": currency,
                }
            else:
                policy_text = _(
                    "A deposit of %(deposit)s %(currency)s (%(percent)s%% of the total) is required. "
                    "The deposit is covered. Remaining balance: %(balance)s %(currency)s."
                ) % {
                    "deposit": amount,
                    "percent": value,
                    "balance": balance_due,
                    "currency": currency,
                }
        else:  # fixed
            if due_now > Decimal("0.00"):
                policy_text = _(
                    "A deposit of %(deposit)s %(currency)s is required. "
                    "Amount due now: %(due_now)s %(currency)s. Remaining balance: %(balance)s %(currency)s."
                ) % {
                    "deposit": amount,
                    "due_now": due_now,
                    "balance": balance_due,
                    "currency": currency,
                }
            else:
                policy_text = _(
                    "A deposit of %(deposit)s %(currency)s is required. "
                    "The deposit is covered. Remaining balance: %(balance)s %(currency)s."
                ) % {
                    "deposit": amount,
                    "balance": balance_due,
                    "currency": currency,
                }

    return DepositSummary(
        policy=policy,
        deposit_amount=amount,
        amount_paid=paid.quantize(MONEY),
        amount_due=balance_due,
        amount_due_now=due_now,
        currency=currency,
        policy_text=policy_text,
    )
