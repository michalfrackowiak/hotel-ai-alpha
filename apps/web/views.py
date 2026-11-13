"""Public website views (Django templates for the alpha)."""

from __future__ import annotations

from datetime import timedelta

from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.hotel.access import (
    booking_manage_path,
    find_booking_for_contact,
    token_matches,
)
from apps.hotel.booking_service import BookingError, get_booking
from apps.hotel.models import RoomType


def home(request):
    room_types = RoomType.objects.filter(is_active=True).prefetch_related("rooms")
    today = timezone.localdate()
    return render(
        request,
        "web/home.html",
        {
            "room_types": room_types,
            "default_check_in": (today + timedelta(days=1)).isoformat(),
            "default_check_out": (today + timedelta(days=3)).isoformat(),
            "today": today,
        },
    )


def chat_page(request):
    return render(request, "web/chat.html")


def voice_page(request):
    return render(request, "web/voice.html")


def booking_lookup(request):
    reference = (request.GET.get("reference") or "").strip().upper()
    contact = (request.GET.get("contact") or "").strip()

    if reference and contact:
        booking = find_booking_for_contact(reference, contact)
        if booking is not None:
            return redirect(booking_manage_path(booking))
        return render(
            request,
            "web/booking_lookup.html",
            {"reference": reference, "contact": contact, "lookup_error": True},
        )
    if reference:
        return render(
            request,
            "web/booking_lookup.html",
            {"reference": reference, "missing_contact": True},
        )
    return render(request, "web/booking_lookup.html")


def booking_detail(request, reference: str):
    reference = reference.upper()
    try:
        booking = get_booking(reference)
    except BookingError:
        return render(request, "web/booking_not_found.html", {"reference": reference}, status=404)

    session_key = f"booking-token-{reference}"
    token = request.GET.get("token") or request.session.get(session_key)
    if not token_matches(booking, token):
        return render(
            request,
            "web/booking_lookup.html",
            {"reference": reference, "access_denied": True},
            status=403,
        )
    request.session[session_key] = token
    return render(request, "web/booking.html", {"booking": booking, "access_token": token})


def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001
        return JsonResponse({"status": "error", "database": "unavailable"}, status=503)
    return JsonResponse({"status": "ok", "time": timezone.now().isoformat()})
