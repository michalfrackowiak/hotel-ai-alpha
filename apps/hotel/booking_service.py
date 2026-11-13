"""Transactional booking workflow.

Every state transition goes through this module so that holds, pricing and
calendar synchronisation stay consistent across the web, chat and voice
channels.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from .availability import available_rooms, room_is_available
from .models import Booking, Client, Room, RoomType
from .pricing import StayError, quote, validate_stay

logger = logging.getLogger(__name__)


class BookingError(Exception):
    """A booking request that cannot be fulfilled, with a stable error code."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.status = status

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


def _translate_validation_error(exc: ValidationError) -> BookingError:
    if hasattr(exc, "message_dict"):
        details = {key: [str(item) for item in value] for key, value in exc.message_dict.items()}
        first = next(iter(details.values()))[0]
    else:
        details = {"non_field_errors": [str(item) for item in exc.messages]}
        first = str(exc.messages[0]) if exc.messages else _("The booking is invalid.")
    return BookingError("validation_error", first, details=details)


def resolve_room_type(room_type: RoomType | str) -> RoomType:
    if isinstance(room_type, RoomType):
        return room_type
    try:
        return RoomType.objects.get(slug=room_type, is_active=True)
    except RoomType.DoesNotExist as exc:
        raise BookingError(
            "room_type_not_found",
            _("The selected room type is not available."),
            details={"room_type": room_type},
            status=404,
        ) from exc


def normalize_language(language: str | None) -> str:
    supported = settings.HOTEL["supported_languages"]
    return language if language in supported else settings.HOTEL["default_language"]


def find_or_create_client(
    *,
    first_name: str,
    last_name: str,
    email: str = "",
    phone: str = "",
    language: str = "en",
) -> Client:
    email = (email or "").strip().lower()
    phone = (phone or "").strip()
    first_name = (first_name or "").strip() or "Guest"
    last_name = (last_name or "").strip()

    if not email and not phone:
        raise BookingError(
            "contact_required",
            _("Please provide an email address or a phone number."),
            details={"email": _("Email or phone is required.")},
        )

    client: Client | None = None
    if email:
        client = Client.objects.filter(email__iexact=email).order_by("pk").first()
    if client is None and phone:
        client = Client.objects.filter(phone=phone).order_by("pk").first()

    if client is None:
        client = Client(first_name=first_name, last_name=last_name, email=email, phone=phone, language=language)
        try:
            client.full_clean()
        except ValidationError as exc:
            raise _translate_validation_error(exc) from exc
        client.save()
    else:
        changed = False
        if first_name and client.first_name != first_name:
            client.first_name = first_name
            changed = True
        if last_name and client.last_name != last_name:
            client.last_name = last_name
            changed = True
        if email and client.email.lower() != email:
            client.email = email
            changed = True
        if phone and client.phone != phone:
            client.phone = phone
            changed = True
        if language and client.language != language:
            client.language = language
            changed = True
        if changed:
            client.save()

    try:
        client.full_clean()
    except ValidationError as exc:
        raise _translate_validation_error(exc) from exc
    return client


def create_booking(
    *,
    room_type: RoomType | str,
    check_in,
    check_out,
    first_name: str,
    last_name: str = "",
    email: str = "",
    phone: str = "",
    adults: int = 2,
    children: int = 0,
    source: str = Booking.Source.WEB,
    language: str = "en",
    special_requests: str = "",
    room: Room | None = None,
    hold_minutes: int | None = None,
) -> Booking:
    """Create a pending booking with a hold on the room."""
    language = normalize_language(language)
    if source not in Booking.Source.values:
        source = Booking.Source.API

    try:
        validate_stay(check_in, check_out)
    except StayError as exc:
        raise BookingError("invalid_stay", str(exc), details={"check_in": str(exc)}) from exc

    adults = max(int(adults or 1), 1)
    children = max(int(children or 0), 0)

    with transaction.atomic():
        room_type = resolve_room_type(room_type)
        # Lock the room-type row so two concurrent requests cannot grab the
        # same last room. SQLite serialises writers; PostgreSQL honours this.
        room_type = RoomType.objects.select_for_update().get(pk=room_type.pk)

        if adults + children > room_type.capacity:
            raise BookingError(
                "capacity_exceeded",
                _("This room type sleeps up to %(capacity)s guests.") % {"capacity": room_type.capacity},
                details={"capacity": room_type.capacity},
            )

        if room is not None:
            if room.room_type_id != room_type.pk or not room.is_active:
                raise BookingError("invalid_room", _("The selected room is not available."))
            if not room_is_available(room, check_in, check_out):
                raise BookingError("no_availability", _("The selected room is no longer available."))
            assigned_room = room
        else:
            candidates = available_rooms(room_type, check_in, check_out)
            if not candidates:
                raise BookingError(
                    "no_availability",
                    _("No rooms of this type are available for the selected dates."),
                )
            assigned_room = candidates[0]

        client = find_or_create_client(
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone=phone,
            language=language,
        )

        try:
            priced = quote(
                room_type,
                check_in,
                check_out,
                adults=adults,
                children=children,
                room=assigned_room,
            )
        except StayError as exc:
            raise BookingError("invalid_stay", str(exc)) from exc

        minutes = hold_minutes if hold_minutes is not None else settings.HOTEL["hold_minutes"]
        booking = Booking(
            client=client,
            room=assigned_room,
            check_in=check_in,
            check_out=check_out,
            adults=adults,
            children=children,
            status=Booking.Status.PENDING,
            source=source,
            language=language,
            currency=priced.currency,
            total_price=priced.total,
            price_breakdown=priced.as_dict(),
            special_requests=special_requests or "",
            hold_expires_at=timezone.now() + timedelta(minutes=minutes),
        )
        try:
            booking.full_clean()
        except ValidationError as exc:
            raise _translate_validation_error(exc) from exc
        booking.save()

    logger.info("Created booking %s (%s → %s)", booking.reference, check_in, check_out)
    return booking


def confirm_booking(reference: str) -> Booking:
    """Confirm a pending booking. Idempotent for already confirmed bookings."""
    with transaction.atomic():
        try:
            booking = Booking.objects.select_for_update().select_related("room", "client").get(
                reference=reference
            )
        except Booking.DoesNotExist as exc:
            raise BookingError("booking_not_found", _("Booking not found."), status=404) from exc

        if booking.status == Booking.Status.CONFIRMED:
            return booking

        if booking.status == Booking.Status.PENDING and booking.is_hold_expired:
            booking.status = Booking.Status.EXPIRED
            booking.save(update_fields=["status", "updated_at"])
            raise BookingError(
                "hold_expired",
                _("The reservation hold has expired. Please choose the room again."),
                status=409,
            )

        if booking.status != Booking.Status.PENDING:
            raise BookingError(
                "not_confirmable",
                _("This booking can no longer be confirmed."),
                details={"status": booking.status},
                status=409,
            )

        booking.status = Booking.Status.CONFIRMED
        booking.confirmed_at = timezone.now()
        booking.hold_expires_at = None
        try:
            booking.full_clean()
        except ValidationError as exc:
            raise _translate_validation_error(exc) from exc
        booking.save()

    logger.info("Confirmed booking %s", booking.reference)
    _schedule_calendar_sync(booking)
    return booking


def cancel_booking(reference: str) -> Booking:
    with transaction.atomic():
        try:
            booking = Booking.objects.select_for_update().select_related("room", "client").get(
                reference=reference
            )
        except Booking.DoesNotExist as exc:
            raise BookingError("booking_not_found", _("Booking not found."), status=404) from exc

        if booking.status in (Booking.Status.CANCELLED, Booking.Status.EXPIRED):
            return booking
        if booking.status in (Booking.Status.CHECKED_OUT, Booking.Status.NO_SHOW):
            raise BookingError(
                "not_cancellable",
                _("This booking can no longer be cancelled."),
                details={"status": booking.status},
                status=409,
            )

        booking.status = Booking.Status.CANCELLED
        booking.cancelled_at = timezone.now()
        booking.hold_expires_at = None
        booking.save()

    logger.info("Cancelled booking %s", booking.reference)
    _schedule_calendar_sync(booking)
    return booking


def expire_stale_holds() -> int:
    """Release rooms whose pending hold has lapsed."""
    now = timezone.now()
    stale_ids = list(
        Booking.objects.filter(status=Booking.Status.PENDING, hold_expires_at__lt=now).values_list(
            "pk", flat=True
        )
    )
    if not stale_ids:
        return 0
    updated = Booking.objects.filter(pk__in=stale_ids).update(
        status=Booking.Status.EXPIRED, updated_at=now
    )
    logger.info("Expired %s stale booking holds", updated)
    return updated


def get_booking(reference: str) -> Booking:
    try:
        return Booking.objects.select_related("room", "room__room_type", "client").get(
            reference=reference
        )
    except Booking.DoesNotExist as exc:
        raise BookingError("booking_not_found", _("Booking not found."), status=404) from exc


def _schedule_calendar_sync(booking: Booking) -> None:
    """Push the booking to the room calendar after the transaction commits."""

    def _run() -> None:
        try:
            from apps.calendar_sync.services import sync_booking

            sync_booking(booking)
        except Exception:  # noqa: BLE001 - calendar failures must never break a booking
            logger.exception("Calendar sync failed for booking %s", booking.reference)

    transaction.on_commit(_run)
