"""Rate plans: derived nightly rates through a named plan."""

from __future__ import annotations

from decimal import Decimal

from apps.hotel.models import RatePlan
from apps.hotel.pricing import quote


def test_rate_plan_applies_a_percentage_discount(room_type, stay_dates):
    check_in, check_out = stay_dates
    plan = RatePlan.objects.create(
        name="Early bird",
        slug="early-bird",
        room_type=room_type,
        kind=RatePlan.Kind.PERCENT,
        adjustment=Decimal("-10.00"),
    )

    priced = quote(room_type, check_in, check_out, adults=2, rate_plan=plan)

    assert priced.nightly[0].rate == Decimal("360.00")
    assert priced.total == Decimal("1080.00")
    assert "Rate plan: Early bird" in priced.nightly[0].rules


def test_rate_plan_applies_a_fixed_surcharge(room_type, stay_dates):
    check_in, check_out = stay_dates
    plan = RatePlan.objects.create(
        name="Breakfast included",
        slug="breakfast",
        kind=RatePlan.Kind.FIXED,
        adjustment=Decimal("50.00"),
    )

    priced = quote(room_type, check_in, check_out, adults=2, rate_plan=plan)

    assert priced.nightly[0].rate == Decimal("450.00")


def test_rate_plan_works_on_top_of_a_daily_rate(room_type, stay_dates):
    from apps.hotel.models import DailyRate

    check_in, check_out = stay_dates
    DailyRate.objects.create(room_type=room_type, date=check_in, rate=Decimal("500.00"))
    plan = RatePlan.objects.create(
        name="Member",
        slug="member",
        kind=RatePlan.Kind.PERCENT,
        adjustment=Decimal("-20.00"),
    )

    priced = quote(room_type, check_in, check_out, adults=2, rate_plan=plan)

    assert priced.nightly[0].rate == Decimal("400.00")