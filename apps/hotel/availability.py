"""Availability search across room types."""

from __future__ import annotations

from datetime import date

from django.db.models import Q
from django.utils import timezone

from .models import Booking, Room, RoomType
from .pricing import Quote, StayError, quote, validate_stay


def _blocking_filter():
    now = timezone.now()
    return Q(status__in=Booking.BLOCKING_STATUSES) | (
        Q(status=Booking.Status.PENDING)
        & (Q(hold_expires_at__isnull=True) | Q(hold_expires_at__gt=now))
    )


def booked_room_ids(
    room_type: RoomType,
    check_in: date,
    check_out: date,
    *,
    exclude_booking: Booking | None = None,
) -> list[int]:
    """Rooms of this type that overlap the requested stay."""
    queryset = Booking.objects.filter(
        room__room_type=room_type,
        check_in__lt=check_out,
        check_out__gt=check_in,
    ).filter(_blocking_filter())
    if exclude_booking and exclude_booking.pk:
        queryset = queryset.exclude(pk=exclude_booking.pk)
    return list(queryset.values_list("room_id", flat=True).distinct())


def available_rooms(
    room_type: RoomType,
    check_in: date,
    check_out: date,
    *,
    exclude_booking: Booking | None = None,
) -> list[Room]:
    blocked = booked_room_ids(room_type, check_in, check_out, exclude_booking=exclude_booking)
    return list(
        Room.objects.filter(room_type=room_type, is_active=True)
        .exclude(pk__in=blocked)
        .order_by("number")
    )


def room_is_available(
    room: Room,
    check_in: date,
    check_out: date,
    *,
    exclude_booking: Booking | None = None,
) -> bool:
    return room in available_rooms(room.room_type, check_in, check_out, exclude_booking=exclude_booking)


def search_availability(
    check_in: date,
    check_out: date,
    *,
    guests: int = 2,
    room_type_slug: str | None = None,
) -> list[dict]:
    """Return one entry per room type that can host the stay."""
    validate_stay(check_in, check_out)
    if guests < 1:
        guests = 1

    room_types = RoomType.objects.filter(is_active=True, capacity__gte=guests)
    if room_type_slug:
        room_types = room_types.filter(slug=room_type_slug)

    results: list[dict] = []
    for room_type in room_types:
        rooms = available_rooms(room_type, check_in, check_out)
        if not rooms:
            continue
        try:
            price: Quote = quote(room_type, check_in, check_out, adults=guests, room=rooms[0])
        except StayError:
            # A rate-calendar minimum stay makes this room type unavailable for
            # the requested range; other room types may still be bookable.
            continue
        results.append(
            {
                "room_type": {
                    "slug": room_type.slug,
                    "name": room_type.name,
                    "short_description": room_type.short_description,
                    "capacity": room_type.capacity,
                    "base_price": str(room_type.base_price),
                    "currency": room_type.currency,
                    "size_sqm": room_type.size_sqm,
                    "bed_configuration": room_type.bed_configuration,
                    "amenities": room_type.amenities,
                },
                "available_rooms": len(rooms),
                "room_numbers": [room.number for room in rooms],
                "quote": price.as_dict(),
            }
        )

    results.sort(key=lambda item: (item["quote"]["total"], item["room_type"]["name"]))
    return results


def availability_for_agent(check_in: date, check_out: date, guests: int = 2) -> dict:
    """Compact, token-friendly availability payload for the LLM tools."""
    results = search_availability(check_in, check_out, guests=guests)
    return {
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "guests": guests,
        "currency": results[0]["quote"]["currency"] if results else None,
        "options": [
            {
                "room_type": item["room_type"]["slug"],
                "name": item["room_type"]["name"],
                "capacity": item["room_type"]["capacity"],
                "available_rooms": item["available_rooms"],
                "nights": item["quote"]["nights"],
                "nightly_from": item["quote"]["nightly"][0]["rate"] if item["quote"]["nightly"] else None,
                "total": item["quote"]["total"],
                "currency": item["quote"]["currency"],
            }
            for item in results
        ],
    }
