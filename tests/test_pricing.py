"""Pricing engine: nightly rates, rules, VAT and stay validation."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.hotel.models import PricingRule
from apps.hotel.pricing import StayError, quote, validate_stay


def test_base_quote_multiplies_nights(room_type, stay_dates):
    check_in, check_out = stay_dates
    priced = quote(room_type, check_in, check_out, adults=2)

    assert priced.nights == 3
    assert priced.total == Decimal("1200.00")
    assert priced.subtotal == priced.total
    assert [night.rate for night in priced.nightly] == [Decimal("400.00")] * 3
    assert priced.currency == "PLN"


def test_percentage_rule_applies_per_night(room_type, next_friday):
    PricingRule.objects.create(
        name="Weekend uplift",
        room_type=room_type,
        kind=PricingRule.Kind.WEEKEND,
        days_of_week="4,5",
        adjustment_type=PricingRule.AdjustmentType.PERCENT,
        amount=Decimal("15.00"),
        priority=10,
    )
    priced = quote(room_type, next_friday, next_friday + timedelta(days=2), adults=2)

    assert priced.nights == 2
    assert [night.rate for night in priced.nightly] == [Decimal("460.00"), Decimal("460.00")]
    assert priced.total == Decimal("920.00")
    assert all(night.rules == ["Weekend uplift"] for night in priced.nightly)


def test_fixed_rule_and_priority_stack(room_type, stay_dates):
    check_in, check_out = stay_dates
    PricingRule.objects.create(
        name="Breakfast",
        room_type=room_type,
        adjustment_type=PricingRule.AdjustmentType.FIXED,
        amount=Decimal("50.00"),
        priority=1,
    )
    PricingRule.objects.create(
        name="Long stay discount",
        room_type=room_type,
        adjustment_type=PricingRule.AdjustmentType.PERCENT,
        amount=Decimal("-10.00"),
        min_nights=3,
        priority=5,
    )

    priced = quote(room_type, check_in, check_out, adults=2)

    # 400 * 0.9 + 50 = 410 per night
    assert priced.total == Decimal("1230.00")
    assert priced.nightly[0].rules == ["Long stay discount", "Breakfast"]


def test_tax_included_is_informational(room_type, stay_dates):
    check_in, check_out = stay_dates
    priced = quote(room_type, check_in, check_out, adults=2)

    expected_tax = (priced.subtotal * priced.tax_rate / (Decimal("1") + priced.tax_rate)).quantize(
        Decimal("0.01")
    )
    assert priced.tax_included == expected_tax
    assert priced.total == priced.subtotal


def test_room_type_rule_and_global_rule_both_apply(room_type, deluxe_suite, stay_dates):
    check_in, check_out = stay_dates
    PricingRule.objects.create(
        name="Global city tax",
        room_type=None,
        adjustment_type=PricingRule.AdjustmentType.FIXED,
        amount=Decimal("10.00"),
    )
    PricingRule.objects.create(
        name="Suite premium",
        room_type=deluxe_suite,
        adjustment_type=PricingRule.AdjustmentType.PERCENT,
        amount=Decimal("25.00"),
    )

    standard = quote(room_type, check_in, check_out, adults=2)
    deluxe = quote(deluxe_suite, check_in, check_out, adults=2)

    assert standard.total == Decimal("1230.00")
    assert deluxe.total == Decimal("3030.00")


def test_capacity_is_enforced(room_type, stay_dates):
    check_in, check_out = stay_dates
    with pytest.raises(StayError):
        quote(room_type, check_in, check_out, adults=3)


def test_validate_stay_rejects_invalid_ranges():
    today = timezone.localdate()

    with pytest.raises(StayError):
        validate_stay(today, today)
    with pytest.raises(StayError):
        validate_stay(today - timedelta(days=1), today + timedelta(days=1))
    with pytest.raises(StayError):
        validate_stay(today, today + timedelta(days=400))


def test_stay_returns_nights(stay_dates):
    check_in, check_out = stay_dates
    assert validate_stay(check_in, check_out) == 3
