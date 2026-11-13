"""Transactional booking workflow.

Every state transition goes through this module so that holds, pricing and
calendar synchronisation stay consistent across the web, chat and voice
channels.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from .availability import available_rooms, room_is_available
from .cancellation import cancellation_preview
from .models import Booking, BookingEvent, Client, RatePlan, Room, RoomType
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


def record_event(
    booking: Booking,
    event_type: str,
    *,
    actor: str = BookingEvent.Actor.SYSTEM,
    channel: str = "",
    summary: str = "",
    data: dict[str, Any] | None = None,
) -> BookingEvent:
    """Append an audit record. Callers never update or delete these rows."""
    return BookingEvent.objects.create(
        booking=booking,
        event_type=event_type,
        actor=actor,
        channel=channel or booking.source,
        summary=summary,
        data=data or {},
    )


def booking_snapshot(booking: Booking) -> dict[str, Any]:
    """The before/after shape stored in audit events."""
    return {
        "status": booking.status,
        "room": booking.room.number,
        "room_type": booking.room.room_type.slug,
        "check_in": booking.check_in.isoformat(),
        "check_out": booking.check_out.isoformat(),
        "adults": booking.adults,
        "children": booking.children,
        "total_price": str(booking.total_price),
        "currency": booking.currency,
    }


def actor_for_source(source: str) -> str:
    if source == Booking.Source.ADMIN:
        return BookingEvent.Actor.STAFF
    if source in (Booking.Source.CHAT, Booking.Source.VOICE):
        return BookingEvent.Actor.AGENT
    return BookingEvent.Actor.GUEST


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
    rate_plan=None,
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

        # Resolve the rate plan (slug or instance) and make sure it applies.
        if isinstance(rate_plan, str):
            try:
                rate_plan = RatePlan.objects.get(slug=rate_plan)
            except RatePlan.DoesNotExist as exc:
                raise BookingError("invalid_rate_plan", _("Unknown rate plan.")) from exc
        if rate_plan is not None and not rate_plan.applies_to(room_type):
            raise BookingError("invalid_rate_plan", _("This rate plan is not available for the selected room type."))
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
                rate_plan=rate_plan,
            )
        except StayError as exc:
            raise BookingError(getattr(exc, "code", "invalid_stay"), str(exc)) from exc

        minutes = hold_minutes if hold_minutes is not None else settings.HOTEL["hold_minutes"]
        booking = Booking(
            client=client,
            room=assigned_room,
            rate_plan=rate_plan,
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
        record_event(
            booking,
            BookingEvent.Type.CREATED,
            actor=actor_for_source(source),
            channel=source,
            summary=_("Booking created for %(check_in)s → %(check_out)s.")
            % {"check_in": check_in, "check_out": check_out},
            data={"after": booking_snapshot(booking)},
        )

    logger.info("Created booking %s (%s → %s)", booking.reference, check_in, check_out)
    return booking


def confirm_booking(
    reference: str,
    *,
    actor: str = BookingEvent.Actor.GUEST,
    channel: str = "",
) -> Booking:
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
            record_event(
                booking,
                BookingEvent.Type.EXPIRED,
                actor=BookingEvent.Actor.SYSTEM,
                channel="system",
                summary=_("Hold expired before confirmation."),
            )
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
        record_event(
            booking,
            BookingEvent.Type.CONFIRMED,
            actor=actor,
            channel=channel or booking.source,
            summary=_("Booking confirmed."),
            data={"after": booking_snapshot(booking)},
        )

    logger.info("Confirmed booking %s", booking.reference)
    _schedule_calendar_sync(booking)
    _schedule_confirmation_email(booking)
    return booking


def cancel_booking(
    reference: str,
    *,
    reason: str = "",
    note: str = "",
    actor: str = BookingEvent.Actor.GUEST,
    channel: str = "",
    fee: Decimal | None = None,
) -> Booking:
    """Cancel a booking, record the policy fee and append an audit event."""
    if reason and reason not in Booking.CancellationReason.values:
        raise BookingError("invalid_reason", _("Choose a valid cancellation reason."))

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

        preview = cancellation_preview(booking)
        charged_fee = preview.fee if fee is None else fee
        before = booking_snapshot(booking)

        booking.status = Booking.Status.CANCELLED
        booking.cancelled_at = timezone.now()
        booking.hold_expires_at = None
        booking.cancellation_reason = reason
        booking.cancellation_note = (note or "").strip()
        booking.cancellation_fee = max(Decimal(charged_fee), Decimal("0")).quantize(Decimal("0.01"))
        booking.cancelled_by = actor
        booking.save()
        record_event(
            booking,
            BookingEvent.Type.CANCELLED,
            actor=actor,
            channel=channel or booking.source,
            summary=_("Booking cancelled."),
            data={
                "before": before,
                "after": booking_snapshot(booking),
                "reason": reason,
                "note": booking.cancellation_note,
                "fee": str(booking.cancellation_fee),
            },
        )

    logger.info("Cancelled booking %s (fee %s)", booking.reference, booking.cancellation_fee)
    _schedule_calendar_sync(booking)
    return booking


def modify_booking(
    reference: str,
    *,
    check_in=None,
    check_out=None,
    room_type: RoomType | str | None = None,
    rate_plan=None,
    adults: int | None = None,
    children: int | None = None,
    actor: str = BookingEvent.Actor.GUEST,
    channel: str = "",
) -> Booking:
    """Change dates, room type or occupancy through one transactional path."""
    with transaction.atomic():
        try:
            booking = Booking.objects.select_for_update().select_related(
                "room", "room__room_type", "client"
            ).get(reference=reference)
        except Booking.DoesNotExist as exc:
            raise BookingError("booking_not_found", _("Booking not found."), status=404) from exc

        if booking.status == Booking.Status.PENDING and booking.is_hold_expired:
            booking.status = Booking.Status.EXPIRED
            booking.save(update_fields=["status", "updated_at"])
            record_event(
                booking,
                BookingEvent.Type.EXPIRED,
                actor=BookingEvent.Actor.SYSTEM,
                channel="system",
                summary=_("Hold expired before modification."),
            )
            raise BookingError(
                "hold_expired",
                _("The reservation hold has expired. Please choose the room again."),
                status=409,
            )

        if booking.status not in (Booking.Status.PENDING, Booking.Status.CONFIRMED):
            raise BookingError(
                "not_modifiable",
                _("This booking can no longer be modified."),
                details={"status": booking.status},
                status=409,
            )

        new_check_in = check_in or booking.check_in
        new_check_out = check_out or booking.check_out
        new_adults = max(int(adults if adults is not None else booking.adults), 1)
        new_children = max(int(children if children is not None else booking.children), 0)

        try:
            validate_stay(new_check_in, new_check_out)
        except StayError as exc:
            raise BookingError(getattr(exc, "code", "invalid_stay"), str(exc)) from exc

        target_type = resolve_room_type(room_type) if room_type else booking.room.room_type
        target_type = RoomType.objects.select_for_update().get(pk=target_type.pk)

        if rate_plan is not None:
            if isinstance(rate_plan, str):
                if rate_plan == "":
                    rate_plan = None
                else:
                    try:
                        rate_plan = RatePlan.objects.get(slug=rate_plan)
                    except RatePlan.DoesNotExist as exc:
                        raise BookingError("invalid_rate_plan", _("Unknown rate plan.")) from exc
            if rate_plan is not None and not rate_plan.applies_to(target_type):
                raise BookingError("invalid_rate_plan", _("This rate plan is not available for the selected room type."))
        if new_adults + new_children > target_type.capacity:
            raise BookingError(
                "capacity_exceeded",
                _("This room type sleeps up to %(capacity)s guests.") % {"capacity": target_type.capacity},
                details={"capacity": target_type.capacity},
            )

        if (
            target_type.pk == booking.room.room_type_id
            and room_is_available(booking.room, new_check_in, new_check_out, exclude_booking=booking)
        ):
            assigned_room = booking.room
        else:
            candidates = available_rooms(
                target_type, new_check_in, new_check_out, exclude_booking=booking
            )
            if not candidates:
                raise BookingError(
                    "no_availability",
                    _("No rooms of this type are available for the selected dates."),
                )
            assigned_room = candidates[0]

        try:
            effective_rate_plan = rate_plan if rate_plan is not None else booking.rate_plan
            priced = quote(
                target_type,
                new_check_in,
                new_check_out,
                adults=new_adults,
                children=new_children,
                room=assigned_room,
                rate_plan=effective_rate_plan,
            )
        except StayError as exc:
            raise BookingError(getattr(exc, "code", "invalid_stay"), str(exc)) from exc

        before = booking_snapshot(booking)
        booking.room = assigned_room
        booking.rate_plan = effective_rate_plan
        booking.check_in = new_check_in
        booking.check_out = new_check_out
        booking.adults = new_adults
        booking.children = new_children
        booking.currency = priced.currency
        booking.total_price = priced.total
        booking.price_breakdown = priced.as_dict()

        after = booking_snapshot(booking)
        if before == after:
            return booking

        try:
            booking.full_clean()
        except ValidationError as exc:
            raise _translate_validation_error(exc) from exc
        booking.save()
        record_event(
            booking,
            BookingEvent.Type.MODIFIED,
            actor=actor,
            channel=channel or booking.source,
            summary=_("Booking changed to %(check_in)s → %(check_out)s.")
            % {"check_in": new_check_in, "check_out": new_check_out},
            data={"before": before, "after": after},
        )

    logger.info("Modified booking %s (%s → %s)", booking.reference, booking.check_in, booking.check_out)
    _schedule_calendar_sync(booking)
    return booking


def expire_stale_holds() -> int:
    """Release rooms whose pending hold has lapsed and audit each release."""
    now = timezone.now()
    stale = list(
        Booking.objects.filter(status=Booking.Status.PENDING, hold_expires_at__lt=now).select_related(
            "room", "room__room_type", "client"
        )
    )
    expired = 0
    for booking in stale:
        booking.status = Booking.Status.EXPIRED
        booking.save(update_fields=["status", "updated_at"])
        record_event(
            booking,
            BookingEvent.Type.EXPIRED,
            actor=BookingEvent.Actor.SYSTEM,
            channel="system",
            summary=_("Hold expired without confirmation."),
        )
        expired += 1
    if expired:
        logger.info("Expired %s stale booking holds", expired)
    return expired


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


def _schedule_confirmation_email(booking: Booking) -> None:
    """Send the guest confirmation after commit, never inside the transaction."""

    def _run() -> None:
        try:
            from .notifications import send_booking_confirmation

            send_booking_confirmation(booking)
        except Exception:  # noqa: BLE001 - email failures must never break a booking
            logger.exception("Confirmation email failed for booking %s", booking.reference)

    transaction.on_commit(_run)
