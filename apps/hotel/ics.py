"""Minimal iCalendar generation for booking confirmation emails."""

from __future__ import annotations

from urllib.parse import urlparse

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _

from .availability import blocking_filter
from .models import Booking, Room


def _escape(value: str) -> str:
    return (
        (value or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """Fold long iCalendar lines at 75 octets, as required by RFC 5545."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    chunks = [encoded[:75]]
    remaining = encoded[75:]
    while remaining:
        chunks.append(b" " + remaining[:74])
        remaining = remaining[74:]
    return "\r\n".join(chunk.decode("utf-8", "ignore") for chunk in chunks)


def build_booking_ics(booking: Booking) -> str:
    hotel = settings.HOTEL
    host = urlparse(hotel["public_url"]).netloc or "hotel.local"
    now = timezone.now().strftime("%Y%m%dT%H%M%SZ")
    status = "CONFIRMED" if booking.is_confirmed else "TENTATIVE"
    summary = _("Stay at %(hotel)s · %(reference)s") % {
        "hotel": hotel["name"],
        "reference": booking.reference,
    }
    description = "\n".join(
        [
            _("Booking %(reference)s") % {"reference": booking.reference},
            _("Guest: %(name)s") % {"name": booking.client.full_name},
            _("Room: %(room)s · %(type)s") % {"room": booking.room.number, "type": booking.room_type.name},
            _("Total: %(total)s %(currency)s")
            % {"total": booking.total_price, "currency": booking.currency},
        ]
    )
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//{hotel['name']}//Booking confirmation//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{booking.reference}@{host}",
        f"DTSTAMP:{now}",
        f"DTSTART;VALUE=DATE:{booking.check_in:%Y%m%d}",
        f"DTEND;VALUE=DATE:{booking.check_out:%Y%m%d}",
        f"SUMMARY:{_escape(summary)}",
        f"DESCRIPTION:{_escape(description)}",
        f"LOCATION:{_escape(hotel['address'])}",
        f"STATUS:{status}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def build_room_calendar(room: Room) -> str:
    """Export one room's blocking bookings and channel blocks as an iCal feed."""
    hotel = settings.HOTEL
    host = urlparse(hotel["public_url"]).netloc or "hotel.local"
    now = timezone.now().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//{hotel['name']}//Room availability//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(room.display_name)}",
    ]

    bookings = (
        Booking.objects.filter(room=room)
        .filter(blocking_filter())
        .select_related("client")
        .order_by("check_in", "pk")
    )
    for booking in bookings:
        status = "CONFIRMED" if booking.is_confirmed else "TENTATIVE"
        summary = _("Booking %(reference)s") % {"reference": booking.reference}
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{booking.reference}@{host}",
                f"DTSTAMP:{now}",
                f"DTSTART;VALUE=DATE:{booking.check_in:%Y%m%d}",
                f"DTEND;VALUE=DATE:{booking.check_out:%Y%m%d}",
                f"SUMMARY:{_escape(summary)}",
                f"STATUS:{status}",
                "TRANSP:OPAQUE",
                "END:VEVENT",
            ]
        )

    for block in room.blocks.order_by("check_in", "pk"):
        summary = block.summary or _("Blocked")
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:block-{block.pk}@{host}",
                f"DTSTAMP:{now}",
                f"DTSTART;VALUE=DATE:{block.check_in:%Y%m%d}",
                f"DTEND;VALUE=DATE:{block.check_out:%Y%m%d}",
                f"SUMMARY:{_escape(summary)}",
                "STATUS:CONFIRMED",
                "TRANSP:OPAQUE",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
