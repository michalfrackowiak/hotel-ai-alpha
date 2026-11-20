"""Domain models: rooms, clients, pricing and bookings."""

from __future__ import annotations

import secrets
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.text import format_lazy
from django.utils.translation import gettext_lazy as _

REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_reference() -> str:
    """Human friendly, unambiguous booking reference, e.g. ``HA-7KQ2M4``."""
    token = "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(6))
    return f"HA-{token}"


def generate_access_token() -> str:
    """Unguessable second secret used by signed booking management links."""
    return secrets.token_urlsafe(24)


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class RoomType(TimeStampedModel):
    name = models.CharField(_("name"), max_length=120)
    slug = models.SlugField(_("slug"), max_length=120, unique=True)
    short_description = models.CharField(_("short description"), max_length=200, blank=True)
    description = models.TextField(_("description"), blank=True)
    capacity = models.PositiveSmallIntegerField(
        _("capacity"), default=2, validators=[MinValueValidator(1), MaxValueValidator(12)]
    )
    base_price = models.DecimalField(
        _("base price per night"),
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    currency = models.CharField(_("currency"), max_length=3, default="PLN")
    size_sqm = models.PositiveSmallIntegerField(_("size (m²)"), null=True, blank=True)
    bed_configuration = models.CharField(_("beds"), max_length=120, blank=True)
    amenities = models.JSONField(_("amenities"), default=list, blank=True)
    is_active = models.BooleanField(_("active"), default=True)
    sort_order = models.PositiveSmallIntegerField(_("sort order"), default=0)

    class Meta:
        ordering = ["sort_order", "base_price", "name"]
        verbose_name = _("room type")
        verbose_name_plural = _("room types")

    def __str__(self) -> str:
        return self.name

    @property
    def active_rooms(self):
        return self.rooms.filter(is_active=True)


class Room(TimeStampedModel):
    class HousekeepingStatus(models.TextChoices):
        CLEAN = "clean", _("Clean")
        DIRTY = "dirty", _("Dirty")
        INSPECTED = "inspected", _("Inspected")
        OUT_OF_ORDER = "out_of_order", _("Out of order")

    number = models.CharField(_("number"), max_length=20, unique=True)
    name = models.CharField(_("name"), max_length=120, blank=True)
    room_type = models.ForeignKey(RoomType, on_delete=models.PROTECT, related_name="rooms")
    floor = models.SmallIntegerField(_("floor"), default=0)
    is_active = models.BooleanField(_("active"), default=True)
    housekeeping_status = models.CharField(
        _("housekeeping status"),
        max_length=20,
        choices=HousekeepingStatus.choices,
        default=HousekeepingStatus.CLEAN,
        db_index=True,
    )
    notes = models.TextField(_("notes"), blank=True)

    class Meta:
        ordering = ["number"]
        verbose_name = _("room")
        verbose_name_plural = _("rooms")

    def __str__(self) -> str:
        return f"{self.number} · {self.room_type.name}"

    @property
    def display_name(self) -> str:
        return f"{self.number} {self.name}".strip()


class ChannelFeed(TimeStampedModel):
    """An iCal feed imported from an OTA for one physical room.

    Imported reservations are stored as :class:`RoomBlock` rows so availability
    and the booking write path honour them without creating synthetic bookings.
    """

    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="channel_feeds")
    name = models.CharField(_("name"), max_length=120)
    url = models.URLField(_("feed URL"), max_length=500)
    is_active = models.BooleanField(_("active"), default=True)
    last_synced_at = models.DateTimeField(_("last synced at"), null=True, blank=True)
    last_error = models.TextField(_("last error"), blank=True, default="")
    last_event_count = models.PositiveIntegerField(_("last event count"), default=0)
    conflict_count = models.PositiveIntegerField(_("conflict count"), default=0)

    class Meta:
        ordering = ["room__number", "name"]
        verbose_name = _("channel feed")
        verbose_name_plural = _("channel feeds")

    def __str__(self) -> str:
        return f"{self.name} · {self.room.number}"


class RoomBlock(TimeStampedModel):
    """Dates during which a physical room cannot be sold.

    Channel imports create ``source=channel`` blocks; reception can add manual
    blocks for maintenance or offline reservations.
    """

    class Source(models.TextChoices):
        CHANNEL = "channel", _("Channel manager")
        MANUAL = "manual", _("Manual block")

    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="blocks")
    feed = models.ForeignKey(
        ChannelFeed,
        on_delete=models.CASCADE,
        related_name="blocks",
        null=True,
        blank=True,
    )
    check_in = models.DateField(_("check-in"))
    check_out = models.DateField(_("check-out"))
    source = models.CharField(_("source"), max_length=20, choices=Source.choices, default=Source.MANUAL)
    external_uid = models.CharField(_("external UID"), max_length=255, blank=True, default="")
    summary = models.CharField(_("summary"), max_length=255, blank=True, default="")

    class Meta:
        ordering = ["check_in", "room__number"]
        verbose_name = _("room block")
        verbose_name_plural = _("room blocks")
        indexes = [
            models.Index(fields=["room", "check_in", "check_out"], name="roomblock_room_dates_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["feed", "external_uid"],
                condition=models.Q(feed__isnull=False) & ~models.Q(external_uid=""),
                name="roomblock_feed_uid_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(check_out__gt=models.F("check_in")),
                name="roomblock_dates_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.room.number} · {self.check_in} → {self.check_out}"

    @property
    def nights(self) -> int:
        return max((self.check_out - self.check_in).days, 0)

    def clean(self) -> None:
        super().clean()
        if self.check_in and self.check_out and self.check_out <= self.check_in:
            raise ValidationError({"check_out": _("Check-out must be after check-in.")})


class Client(TimeStampedModel):
    class Language(models.TextChoices):
        ENGLISH = "en", _("English")
        POLISH = "pl", _("Polish")

    first_name = models.CharField(_("first name"), max_length=80)
    last_name = models.CharField(_("last name"), max_length=80, blank=True, default="")
    email = models.EmailField(_("email"), blank=True)
    phone = models.CharField(_("phone"), max_length=40, blank=True)
    language = models.CharField(_("language"), max_length=2, choices=Language.choices, default="pl")
    country = models.CharField(_("country"), max_length=2, blank=True)
    marketing_consent = models.BooleanField(_("marketing consent"), default=False)
    notes = models.TextField(_("notes"), blank=True)

    class Meta:
        ordering = ["last_name", "first_name"]
        verbose_name = _("client")
        verbose_name_plural = _("clients")

    def __str__(self) -> str:
        return self.full_name

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def display_contact(self) -> str:
        return self.email or self.phone or "—"

    def clean(self) -> None:
        super().clean()
        if not self.email and not self.phone:
            raise ValidationError(_("Provide at least an email address or a phone number."))


class PricingRule(TimeStampedModel):
    class Kind(models.TextChoices):
        SEASONAL = "seasonal", _("Seasonal")
        WEEKEND = "weekend", _("Weekend")
        LAST_MINUTE = "last_minute", _("Last minute")
        LONG_STAY = "long_stay", _("Long stay")

    class AdjustmentType(models.TextChoices):
        PERCENT = "percent", _("Percent")
        FIXED = "fixed", _("Fixed amount per night")

    name = models.CharField(_("name"), max_length=120)
    room_type = models.ForeignKey(
        RoomType,
        on_delete=models.CASCADE,
        related_name="pricing_rules",
        null=True,
        blank=True,
        help_text=_("Leave empty to apply to every room type."),
    )
    kind = models.CharField(_("kind"), max_length=20, choices=Kind.choices, default=Kind.SEASONAL)
    start_date = models.DateField(_("valid from"), null=True, blank=True)
    end_date = models.DateField(_("valid until"), null=True, blank=True)
    days_of_week = models.CharField(
        _("days of week"),
        max_length=20,
        blank=True,
        help_text=_("Comma separated weekday numbers, Monday=0. Example: 4,5 for Friday and Saturday nights."),
    )
    min_nights = models.PositiveSmallIntegerField(_("minimum nights"), default=0)
    adjustment_type = models.CharField(
        _("adjustment type"), max_length=10, choices=AdjustmentType.choices, default=AdjustmentType.PERCENT
    )
    amount = models.DecimalField(
        _("amount"),
        max_digits=10,
        decimal_places=2,
        help_text=_("Percent (e.g. 15 = +15%) or a fixed amount per night."),
    )
    priority = models.SmallIntegerField(_("priority"), default=0)
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        ordering = ["-priority", "name"]
        verbose_name = _("pricing rule")
        verbose_name_plural = _("pricing rules")

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValidationError({"end_date": _("The end date must be after the start date.")})
        if self.days_of_week:
            try:
                days = [int(part) for part in self.days_of_week.split(",") if part.strip()]
            except ValueError as exc:
                raise ValidationError({"days_of_week": _("Use comma separated numbers between 0 and 6.")}) from exc
            if any(day < 0 or day > 6 for day in days):
                raise ValidationError({"days_of_week": _("Weekday numbers must be between 0 and 6.")})
        if self.adjustment_type == self.AdjustmentType.PERCENT and self.amount < Decimal("-100"):
            raise ValidationError({"amount": _("A percentage cannot be lower than -100.")})

    @property
    def weekdays(self) -> set[int]:
        if not self.days_of_week:
            return set()
        return {int(part) for part in self.days_of_week.split(",") if part.strip()}

    def applies_to(self, night: date, check_in: date, nights: int) -> bool:
        if not self.is_active:
            return False
        if self.start_date and night < self.start_date:
            return False
        if self.end_date and night > self.end_date:
            return False
        weekdays = self.weekdays
        if weekdays and night.weekday() not in weekdays:
            return False
        if self.min_nights and nights < self.min_nights:
            return False
        return True

    def adjust(self, price: Decimal) -> Decimal:
        if self.adjustment_type == self.AdjustmentType.PERCENT:
            return (price * (Decimal("1") + self.amount / Decimal("100"))).quantize(Decimal("0.01"))
        return price + self.amount


class RatePlan(TimeStampedModel):
    """A named pricing plan for one (or all) room types.

    A plan derives its nightly rate from the calculated or daily-rate price
    using a percentage or fixed adjustment. Bookings can opt into a plan; the
    quote applies the adjustment to every night of the stay.
    """

    class Kind(models.TextChoices):
        PERCENT = "percent", _("Percentage")
        FIXED = "fixed", _("Fixed amount")

    room_type = models.ForeignKey(
        RoomType,
        on_delete=models.CASCADE,
        related_name="rate_plans",
        null=True,
        blank=True,
        help_text=_("Leave empty to make this plan available for every room type."),
    )
    name = models.CharField(_("name"), max_length=120)
    slug = models.SlugField(_("slug"), max_length=120, unique=True)
    description = models.TextField(_("description"), blank=True)
    kind = models.CharField(_("kind"), max_length=10, choices=Kind.choices, default=Kind.PERCENT)
    adjustment = models.DecimalField(
        _("adjustment"),
        max_digits=10,
        decimal_places=2,
        default=Decimal("0"),
        help_text=_("Percentage (e.g. -10 for 10% off) or a fixed amount in the hotel currency."),
    )
    is_refundable = models.BooleanField(_("refundable"), default=True)
    is_active = models.BooleanField(_("active"), default=True)
    sort_order = models.PositiveSmallIntegerField(_("sort order"), default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = _("rate plan")
        verbose_name_plural = _("rate plans")

    def __str__(self) -> str:
        return self.name

    def applies_to(self, room_type: RoomType) -> bool:
        return self.is_active and (self.room_type_id is None or self.room_type_id == room_type.pk)

    def adjust(self, rate: Decimal) -> Decimal:
        if self.kind == self.Kind.PERCENT:
            value = rate * (Decimal("1") + self.adjustment / Decimal("100"))
        else:
            value = rate + self.adjustment
        return max(value, Decimal("0")).quantize(Decimal("0.01"))


class DailyRate(TimeStampedModel):
    """An explicit nightly rate and minimum stay for one room type and date.

    A row here overrides the calculated rate for that night and is the source
    of truth for the reception's rate calendar.
    """

    room_type = models.ForeignKey(RoomType, on_delete=models.CASCADE, related_name="daily_rates")
    date = models.DateField(_("date"), db_index=True)
    rate = models.DecimalField(
        _("rate"),
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    min_nights = models.PositiveSmallIntegerField(
        _("minimum nights"),
        default=0,
        help_text=_("Minimum stay required for a booking that includes this night. 0 means no minimum."),
    )
    max_nights = models.PositiveSmallIntegerField(
        _("maximum nights"),
        default=0,
        help_text=_("Maximum stay allowed for a booking that includes this night. 0 means no maximum."),
    )
    closed = models.BooleanField(
        _("closed"),
        default=False,
        help_text=_("No booking may include this night."),
    )
    closed_to_arrival = models.BooleanField(
        _("closed to arrival"),
        default=False,
        help_text=_("A booking may not start (check in) on this night."),
    )
    class Meta:
        ordering = ["date", "room_type"]
        verbose_name = _("daily rate")
        verbose_name_plural = _("daily rates")
        constraints = [
            models.UniqueConstraint(fields=["room_type", "date"], name="daily_rate_room_type_date_uniq"),
        ]

    def __str__(self) -> str:
        return f"{self.room_type} · {self.date} · {self.rate}"


class Booking(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        CONFIRMED = "confirmed", _("Confirmed")
        CHECKED_IN = "checked_in", _("Checked in")
        CHECKED_OUT = "checked_out", _("Checked out")
        CANCELLED = "cancelled", _("Cancelled")
        EXPIRED = "expired", _("Expired")
        NO_SHOW = "no_show", _("No show")

    class Source(models.TextChoices):
        WEB = "web", _("Website")
        CHAT = "chat", _("Chat agent")
        VOICE = "voice", _("Voice agent")
        ADMIN = "admin", _("Reception")
        API = "api", _("API")

    class CancellationReason(models.TextChoices):
        GUEST_REQUEST = "guest_request", _("Guest request")
        PLANS_CHANGED = "plans_changed", _("Plans changed")
        FOUND_ALTERNATIVE = "found_alternative", _("Found another option")
        PRICE = "price", _("Price")
        EMERGENCY = "emergency", _("Emergency")
        OTHER = "other", _("Other")

    class CancelledBy(models.TextChoices):
        GUEST = "guest", _("Guest")
        STAFF = "staff", _("Reception")
        AGENT = "agent", _("AI concierge")
        SYSTEM = "system", _("System")

    class PaymentStatus(models.TextChoices):
        UNPAID = "unpaid", _("Unpaid")
        PARTIALLY_PAID = "partially_paid", _("Partially paid")
        PAID = "paid", _("Paid")
        REFUNDED = "refunded", _("Refunded")

    ACTIVE_STATUSES = (Status.PENDING, Status.CONFIRMED, Status.CHECKED_IN)
    BLOCKING_STATUSES = (Status.CONFIRMED, Status.CHECKED_IN)

    reference = models.CharField(
        _("reference"), max_length=12, unique=True, editable=False, default=generate_reference
    )
    access_token = models.CharField(
        _("access token"), max_length=64, unique=True, editable=False, default=generate_access_token
    )
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="bookings")
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="bookings")
    rate_plan = models.ForeignKey(
        "RatePlan", on_delete=models.PROTECT, related_name="bookings", null=True, blank=True
    )
    check_in = models.DateField(_("check-in"), db_index=True)
    check_out = models.DateField(_("check-out"), db_index=True)
    adults = models.PositiveSmallIntegerField(_("adults"), default=2)
    children = models.PositiveSmallIntegerField(_("children"), default=0)
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    source = models.CharField(_("source"), max_length=20, choices=Source.choices, default=Source.WEB)
    language = models.CharField(_("language"), max_length=2, default="pl")
    currency = models.CharField(_("currency"), max_length=3, default="PLN")
    total_price = models.DecimalField(_("total price"), max_digits=10, decimal_places=2, default=Decimal("0"))
    price_breakdown = models.JSONField(_("price breakdown"), default=dict, blank=True)
    special_requests = models.TextField(_("special requests"), blank=True)
    hold_expires_at = models.DateTimeField(_("hold expires at"), null=True, blank=True)
    confirmed_at = models.DateTimeField(_("confirmed at"), null=True, blank=True)
    cancelled_at = models.DateTimeField(_("cancelled at"), null=True, blank=True)
    cancellation_reason = models.CharField(
        _("cancellation reason"),
        max_length=32,
        choices=CancellationReason.choices,
        blank=True,
    )
    cancellation_note = models.TextField(_("cancellation note"), blank=True)
    cancellation_fee = models.DecimalField(
        _("cancellation fee"),
        max_digits=10,
        decimal_places=2,
        default=Decimal("0"),
    )
    cancelled_by = models.CharField(
        _("cancelled by"),
        max_length=20,
        choices=CancelledBy.choices,
        blank=True,
    )
    payment_status = models.CharField(
        _("payment status"),
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.UNPAID,
        db_index=True,
    )
    amount_paid = models.DecimalField(
        _("amount paid"),
        max_digits=10,
        decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    payment_method = models.CharField(_("payment method"), max_length=40, blank=True, default="")
    payment_reference = models.CharField(_("payment reference"), max_length=120, blank=True, default="")
    paid_at = models.DateTimeField(_("paid at"), null=True, blank=True)
    payment_notes = models.TextField(_("payment notes"), blank=True, default="")
    confirmation_email_sent_at = models.DateTimeField(_("confirmation email sent at"), null=True, blank=True)
    confirmation_email_status = models.CharField(
        _("confirmation email status"),
        max_length=20,
        choices=[
            ("pending", _("Pending")),
            ("sent", _("Sent")),
            ("delivered", _("Delivered")),
            ("bounced", _("Bounced")),
            ("complained", _("Complained")),
            ("failed", _("Failed")),
        ],
        default="pending",
        db_index=True,
    )
    confirmation_email_error = models.TextField(_("confirmation email error"), blank=True, default="")
    confirmation_email_provider_id = models.CharField(_("provider message id"), max_length=200, blank=True, default="")
    confirmation_email_attempts = models.PositiveSmallIntegerField(
        _("confirmation email attempts"),
        default=0,
    )
    confirmation_email_last_attempt_at = models.DateTimeField(
        _("confirmation email last attempt at"),
        null=True,
        blank=True,
    )
    confirmation_email_next_retry_at = models.DateTimeField(
        _("confirmation email next retry at"),
        null=True,
        blank=True,
        db_index=True,
    )
    confirmation_sms_sent_at = models.DateTimeField(_("confirmation SMS sent at"), null=True, blank=True)
    confirmation_sms_status = models.CharField(
        _("confirmation SMS status"),
        max_length=20,
        choices=[
            ("pending", _("Pending")),
            ("sent", _("Sent")),
            ("delivered", _("Delivered")),
            ("undelivered", _("Undelivered")),
            ("failed", _("Failed")),
        ],
        default="pending",
        db_index=True,
    )
    confirmation_sms_error = models.TextField(_("confirmation SMS error"), blank=True, default="")
    confirmation_sms_provider_id = models.CharField(_("SMS provider message id"), max_length=200, blank=True, default="")
    confirmation_sms_attempts = models.PositiveSmallIntegerField(
        _("confirmation SMS attempts"),
        default=0,
    )
    confirmation_sms_last_attempt_at = models.DateTimeField(
        _("confirmation SMS last attempt at"),
        null=True,
        blank=True,
    )
    confirmation_sms_next_retry_at = models.DateTimeField(
        _("confirmation SMS next retry at"),
        null=True,
        blank=True,
        db_index=True,
    )
    external_event_id = models.CharField(_("calendar event id"), max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("booking")
        verbose_name_plural = _("bookings")
        indexes = [
            models.Index(fields=["room", "check_in", "check_out"], name="booking_room_dates_idx"),
            models.Index(fields=["status", "hold_expires_at"], name="booking_status_hold_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.reference} · {self.client} · {self.check_in} → {self.check_out}"

    @property
    def nights(self) -> int:
        return max((self.check_out - self.check_in).days, 0)

    @property
    def is_hold_expired(self) -> bool:
        return (
            self.status == self.Status.PENDING
            and self.hold_expires_at is not None
            and self.hold_expires_at <= timezone.now()
        )

    @property
    def is_active(self) -> bool:
        return self.status in self.ACTIVE_STATUSES and not self.is_hold_expired

    @property
    def is_confirmed(self) -> bool:
        return self.status in (self.Status.CONFIRMED, self.Status.CHECKED_IN, self.Status.CHECKED_OUT)

    @property
    def can_confirm(self) -> bool:
        return self.status == self.Status.PENDING and not self.is_hold_expired

    @property
    def can_cancel(self) -> bool:
        return self.status in (self.Status.PENDING, self.Status.CONFIRMED, self.Status.CHECKED_IN)

    @property
    def can_modify(self) -> bool:
        return self.status in (self.Status.PENDING, self.Status.CONFIRMED)

    @property
    def guest_count(self) -> int:
        return self.adults + self.children

    @property
    def room_type(self) -> RoomType:
        return self.room.room_type

    @property
    def amount_due(self) -> Decimal:
        """Outstanding balance against the booking total."""
        due = (self.total_price or Decimal("0")) - (self.amount_paid or Decimal("0"))
        return max(due, Decimal("0")).quantize(Decimal("0.01"))

    def clean(self) -> None:
        super().clean()
        if self.check_in and self.check_out and self.check_out <= self.check_in:
            raise ValidationError({"check_out": _("Check-out must be after check-in.")})
        if self.adults < 1:
            raise ValidationError({"adults": _("At least one adult is required.")})
        if self.room_id and self.adults + self.children > self.room.room_type.capacity:
            raise ValidationError({"adults": _("The room capacity is exceeded.")})
        if self.check_in and self.check_out:
            nights = (self.check_out - self.check_in).days
            if nights > settings.HOTEL["max_booking_nights"]:
                raise ValidationError(
                    {
                        "check_out": format_lazy(
                            _("Stays longer than {nights} nights are not supported."),
                            nights=settings.HOTEL["max_booking_nights"],
                        )
                    }
                )
        if self.room_id and self.check_in and self.check_out and self.status in self.ACTIVE_STATUSES:
            overlapping = (
                Booking.objects.filter(
                    room_id=self.room_id,
                    check_in__lt=self.check_out,
                    check_out__gt=self.check_in,
                )
                .exclude(pk=self.pk)
                .filter(
                    models.Q(status__in=self.BLOCKING_STATUSES)
                    | models.Q(
                        status=self.Status.PENDING,
                        hold_expires_at__gt=timezone.now(),
                    )
                )
            )
            if overlapping.exists():
                raise ValidationError(_("This room is already booked for the selected dates."))
            if RoomBlock.objects.filter(
                room_id=self.room_id,
                check_in__lt=self.check_out,
                check_out__gt=self.check_in,
            ).exists():
                raise ValidationError(_("This room is blocked for the selected dates."))


class EmailDelivery(TimeStampedModel):
    """One attempt to deliver a booking confirmation email.

    Kept as an append-only history so reception can see every attempt and its
    outcome instead of only the current delivery state on the booking.
    """

    class Status(models.TextChoices):
        SENT = "sent", _("Sent")
        FAILED = "failed", _("Failed")
        DELIVERED = "delivered", _("Delivered")
        BOUNCED = "bounced", _("Bounced")
        COMPLAINED = "complained", _("Complained")

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="email_deliveries")
    attempt = models.PositiveSmallIntegerField(_("attempt"), default=1)
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.SENT)
    provider = models.CharField(_("provider"), max_length=40, blank=True, default="")
    provider_message_id = models.CharField(_("provider message id"), max_length=200, blank=True, default="")
    recipient = models.EmailField(_("recipient"), blank=True, default="")
    subject = models.CharField(_("subject"), max_length=255, blank=True, default="")
    error_message = models.TextField(_("error message"), blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("email delivery")
        verbose_name_plural = _("email deliveries")
        indexes = [models.Index(fields=["booking", "-created_at"], name="emaildelivery_booking_idx")]

    def __str__(self) -> str:
        return f"{self.booking.reference} · attempt {self.attempt} · {self.status}"


class SmsDelivery(TimeStampedModel):
    """One attempt to deliver a booking confirmation SMS.

    Kept as an append-only history so reception can see every attempt and its
    outcome, including provider delivery receipts.
    """

    class Status(models.TextChoices):
        SENT = "sent", _("Sent")
        FAILED = "failed", _("Failed")
        DELIVERED = "delivered", _("Delivered")
        UNDELIVERED = "undelivered", _("Undelivered")

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="sms_deliveries")
    attempt = models.PositiveSmallIntegerField(_("attempt"), default=1)
    status = models.CharField(_("status"), max_length=20, choices=Status.choices, default=Status.SENT)
    provider = models.CharField(_("provider"), max_length=40, blank=True, default="")
    provider_message_id = models.CharField(_("provider message id"), max_length=200, blank=True, default="")
    recipient = models.CharField(_("recipient"), max_length=40, blank=True, default="")
    body = models.TextField(_("body"), blank=True, default="")
    error_message = models.TextField(_("error message"), blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("SMS delivery")
        verbose_name_plural = _("SMS deliveries")
        indexes = [models.Index(fields=["booking", "-created_at"], name="smsdelivery_booking_idx")]

    def __str__(self) -> str:
        return f"{self.booking.reference} · attempt {self.attempt} · {self.status}"


class InboundSmsMessage(TimeStampedModel):
    """A text message sent by a guest to the hotel number.

    Used to record inbound replies to confirmation texts. When the sender's
    phone number can be matched, the message is linked to the most recent
    relevant booking.
    """

    booking = models.ForeignKey(
        Booking,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inbound_sms_messages",
    )
    from_number = models.CharField(_("from number"), max_length=40, blank=True, default="")
    to_number = models.CharField(_("to number"), max_length=40, blank=True, default="")
    body = models.TextField(_("body"), blank=True, default="")
    provider = models.CharField(_("provider"), max_length=40, blank=True, default="")
    provider_message_id = models.CharField(_("provider message id"), max_length=200, blank=True, default="")
    received_at = models.DateTimeField(_("received at"), default=timezone.now)

    class Meta:
        ordering = ["-received_at", "-created_at"]
        verbose_name = _("inbound SMS message")
        verbose_name_plural = _("inbound SMS messages")
        indexes = [
            models.Index(fields=["-received_at"], name="inboundsms_received_idx"),
            models.Index(fields=["booking", "-received_at"], name="inboundsms_booking_idx"),
        ]

    def __str__(self) -> str:
        who = self.from_number or "—"
        return f"{who} · {self.received_at:%Y-%m-%d %H:%M}"


class GuestLoginToken(models.Model):
    """Single-use magic-link token for the guest booking account.

    Only a SHA-256 hash of the token is stored, so a database leak does not
    hand out working login links. Tokens expire quickly and are consumed once.
    """

    email = models.EmailField(_("email"), db_index=True)
    token_hash = models.CharField(_("token hash"), max_length=64, unique=True)
    expires_at = models.DateTimeField(_("expires at"))
    used_at = models.DateTimeField(_("used at"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("guest login token")
        verbose_name_plural = _("guest login tokens")
        indexes = [models.Index(fields=["email", "-created_at"], name="guest_login_email_idx")]

    def __str__(self) -> str:
        return f"{self.email} · {self.created_at:%Y-%m-%d %H:%M}"

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= timezone.now()

    @property
    def is_usable(self) -> bool:
        return self.used_at is None and not self.is_expired


class BookingEvent(models.Model):
    """Append-only audit record for every booking transition."""

    class Type(models.TextChoices):
        CREATED = "created", _("Created")
        CONFIRMED = "confirmed", _("Confirmed")
        MODIFIED = "modified", _("Modified")
        CANCELLED = "cancelled", _("Cancelled")
        EXPIRED = "expired", _("Expired")
        CHECKED_IN = "checked_in", _("Checked in")
        CHECKED_OUT = "checked_out", _("Checked out")
        NO_SHOW = "no_show", _("No show")
        EMAIL_SENT = "email_sent", _("Email sent")
        CALENDAR_SYNCED = "calendar_synced", _("Calendar synced")
        PAYMENT_RECORDED = "payment_recorded", _("Payment recorded")
        PAYMENT_ADJUSTED = "payment_adjusted", _("Payment adjusted")
        PAYMENT_REFUNDED = "payment_refunded", _("Payment refunded")
        PAYMENT_WAIVED = "payment_waived", _("Payment waived")

    class Actor(models.TextChoices):
        GUEST = "guest", _("Guest")
        STAFF = "staff", _("Reception")
        AGENT = "agent", _("AI concierge")
        SYSTEM = "system", _("System")

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(_("event type"), max_length=32, choices=Type.choices)
    actor = models.CharField(_("actor"), max_length=20, choices=Actor.choices, default=Actor.SYSTEM)
    channel = models.CharField(_("channel"), max_length=20, blank=True)
    summary = models.CharField(_("summary"), max_length=255, blank=True)
    data = models.JSONField(_("data"), default=dict, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        verbose_name = _("booking event")
        verbose_name_plural = _("booking events")
        indexes = [models.Index(fields=["booking", "-created_at"], name="booking_event_booking_idx")]

    def __str__(self) -> str:
        return f"{self.booking.reference} · {self.get_event_type_display()} · {self.created_at:%Y-%m-%d %H:%M}"

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            raise ValueError("Booking events are append-only.")
        return super().save(*args, **kwargs)

