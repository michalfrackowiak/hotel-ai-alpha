"""Public website views (Django templates for the alpha)."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db import connection
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.dateparse import parse_date
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.hotel.access import (
    booking_manage_path,
    find_booking_for_contact,
    make_booking_token,
    token_matches,
)
from apps.hotel.availability import search_availability
from apps.hotel.booking_service import (
    BookingError,
    confirm_booking,
    create_booking,
    get_booking,
)
from apps.hotel.cancellation import cancellation_preview
from apps.hotel.guest_access import (
    GUEST_SESSION_KEY,
    bookings_for_email,
    consume_login_token,
    guest_session_matches,
    issue_login_token,
    normalise_email,
    reserve_login_request,
    session_email,
)
from apps.hotel.models import Booking, BookingEvent, RatePlan, Room, RoomType
from apps.hotel.notifications import max_email_attempts, send_guest_login_email
from apps.hotel.pricing import StayError

logger = logging.getLogger(__name__)


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


def _int_param(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def book(request):
    """Public booking flow: pick a stay, choose a rate, create the booking.

    The page quotes through ``search_availability`` so the guest sees the same
    prices and rate-plan options as the chat and voice channels. Creation and
    confirmation still go through ``apps.hotel.booking_service``.
    """
    params = request.POST if request.method == "POST" else request.GET
    today = timezone.localdate()

    room_type_slug = (params.get("room_type") or "").strip()
    check_in_value = (params.get("check_in") or "").strip()
    check_out_value = (params.get("check_out") or "").strip()
    adults = max(_int_param(params.get("adults") or params.get("guests"), 2), 1)
    children = max(_int_param(params.get("children"), 0), 0)
    selected_plan = (params.get("rate_plan") or "").strip()

    check_in = parse_date(check_in_value) if check_in_value else None
    check_out = parse_date(check_out_value) if check_out_value else None
    room_type = None
    quote_data = None
    plans: list[dict] = []
    error = ""

    if room_type_slug:
        room_type = RoomType.objects.filter(slug=room_type_slug, is_active=True).first()
        if room_type is None:
            error = _("The selected room type is not available.")

    if room_type and (check_in_value or check_out_value) and (check_in is None or check_out is None):
        error = _("Both check-in and check-out dates are required.")

    if room_type and check_in and check_out and not error:
        try:
            results = search_availability(
                check_in,
                check_out,
                guests=adults + children,
                room_type_slug=room_type.slug,
            )
        except StayError as exc:
            error = str(exc)
        else:
            if not results:
                error = _("No rooms are available for those dates. Try different dates or ask the concierge.")
            else:
                result = results[0]
                quote_data = result["quote"]
                plan_rows = result.get("rate_plans", [])
                records = {
                    record.slug: record
                    for record in RatePlan.objects.filter(
                        is_active=True,
                        slug__in=[row["slug"] for row in plan_rows],
                    )
                }
                for row in plan_rows:
                    record = records.get(row["slug"])
                    plans.append(
                        {
                            **row,
                            "description": record.description if record else "",
                        }
                    )
                if selected_plan and selected_plan not in {row["slug"] for row in plans}:
                    selected_plan = ""

    if request.method == "POST":
        first_name = (request.POST.get("first_name") or "").strip()
        if error:
            pass
        elif quote_data is None:
            error = _("Choose a room type and your dates first.")
        elif not first_name:
            error = _("Please enter your first name.")
        else:
            try:
                booking = create_booking(
                    room_type=room_type,
                    check_in=check_in,
                    check_out=check_out,
                    first_name=first_name,
                    last_name=(request.POST.get("last_name") or "").strip(),
                    email=(request.POST.get("email") or "").strip(),
                    phone=(request.POST.get("phone") or "").strip(),
                    adults=adults,
                    children=children,
                    source=Booking.Source.WEB,
                    language=translation.get_language(),
                    special_requests=(request.POST.get("special_requests") or "").strip(),
                    rate_plan=selected_plan or None,
                )
            except BookingError as exc:
                error = exc.message
            else:
                try:
                    confirm_booking(
                        booking.reference,
                        actor=BookingEvent.Actor.GUEST,
                        channel=Booking.Source.WEB,
                    )
                except BookingError:
                    logger.exception("Could not confirm web booking %s", booking.reference)
                return redirect(booking_manage_path(booking))

    return render(
        request,
        "web/book.html",
        {
            "room_types": RoomType.objects.filter(is_active=True).order_by(
                "sort_order", "base_price", "name"
            ),
            "room_type": room_type,
            "quote": quote_data,
            "plans": plans,
            "selected_plan": selected_plan,
            "check_in": check_in,
            "check_out": check_out,
            "check_in_value": check_in_value,
            "check_out_value": check_out_value,
            "adults": adults,
            "children": children,
            "today": today,
            "default_check_in": (today + timedelta(days=1)).isoformat(),
            "default_check_out": (today + timedelta(days=3)).isoformat(),
            "error": error,
            "form_data": request.POST if request.method == "POST" else {},
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


def guest_bookings(request):
    """Passwordless guest account: request a link, then list every booking."""
    email = session_email(request)
    if request.method == "POST":
        requested = normalise_email(request.POST.get("email"))
        client_ip = request.META.get("REMOTE_ADDR", "")
        if requested and reserve_login_request(requested, client_ip):
            issued = issue_login_token(requested)
            if issued is not None:
                _record, raw_token = issued
                login_path = reverse("web:guest-login", args=[raw_token])
                login_url = f"{settings.HOTEL['public_url'].rstrip('/')}{login_path}"
                send_guest_login_email(
                    requested,
                    login_url,
                    language=translation.get_language(),
                )
        return render(
            request,
            "web/guest_login.html",
            {"sent": True, "email": requested},
        )

    if email:
        return render(
            request,
            "web/guest_bookings.html",
            {
                "guest_email": email,
                "bookings": bookings_for_email(email),
                "today": timezone.localdate(),
            },
        )
    return render(request, "web/guest_login.html")


def guest_login(request, token: str):
    email = consume_login_token(token)
    if email is None:
        return render(
            request,
            "web/guest_login.html",
            {"link_invalid": True},
            status=400,
        )
    request.session[GUEST_SESSION_KEY] = email
    request.session.cycle_key()
    return redirect("web:guest-bookings")


@require_POST
def guest_logout(request):
    request.session.pop(GUEST_SESSION_KEY, None)
    request.session.cycle_key()
    return redirect("web:guest-bookings")


def booking_detail(request, reference: str):
    reference = reference.upper()
    try:
        booking = get_booking(reference)
    except BookingError:
        return render(request, "web/booking_not_found.html", {"reference": reference}, status=404)

    session_key = f"booking-token-{reference}"
    token = request.GET.get("token") or request.session.get(session_key)
    if not token_matches(booking, token):
        if not guest_session_matches(request, booking):
            return render(
                request,
                "web/booking_lookup.html",
                {"reference": reference, "access_denied": True},
                status=403,
            )
        token = make_booking_token(booking)
    request.session[session_key] = token
    return render(
        request,
        "web/booking.html",
        {
            "booking": booking,
            "access_token": token,
            "cancellation": cancellation_preview(booking),
            "room_types": RoomType.objects.filter(is_active=True).order_by("sort_order", "base_price", "name"),
            "today": timezone.localdate(),
        },
    )


@staff_member_required
def ops_dashboard(request):
    """One screen for the start of the reception shift."""
    from apps.calendar_sync.models import BookingCalendarEvent

    today = timezone.localdate()
    now = timezone.now()
    active_statuses = (Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN)

    arrivals = (
        Booking.objects.filter(check_in=today, status__in=active_statuses)
        .select_related("client", "room", "room__room_type")
        .order_by("room__number")
    )
    departures = (
        Booking.objects.filter(check_out=today, status__in=(Booking.Status.CHECKED_IN, Booking.Status.CHECKED_OUT, Booking.Status.CONFIRMED))
        .select_related("client", "room", "room__room_type")
        .order_by("room__number")
    )
    pending_holds = (
        Booking.objects.filter(status=Booking.Status.PENDING, hold_expires_at__gt=now)
        .select_related("client", "room", "room__room_type")
        .order_by("hold_expires_at")
    )
    stale_holds = Booking.objects.filter(status=Booking.Status.PENDING, hold_expires_at__lte=now).count()
    failed_calendar = (
        BookingCalendarEvent.objects.filter(status=BookingCalendarEvent.Status.FAILED)
        .select_related("booking", "booking__client")
        .order_by("-updated_at")[:20]
    )
    stale_calendar = BookingCalendarEvent.objects.filter(
        status=BookingCalendarEvent.Status.SYNCED,
        booking__status__in=(Booking.Status.CANCELLED, Booking.Status.EXPIRED, Booking.Status.NO_SHOW),
    ).count()
    not_synced_calendar = BookingCalendarEvent.objects.filter(
        status=BookingCalendarEvent.Status.PENDING,
    ).count() + Booking.objects.filter(
        status__in=(Booking.Status.CONFIRMED, Booking.Status.CHECKED_IN),
    ).filter(calendar_events__isnull=True).count()
    missing_email = Booking.objects.filter(
        status__in=active_statuses,
        client__email__gt="",
        confirmation_email_sent_at__isnull=True,
    ).count()
    email_delivered_count = Booking.objects.filter(confirmation_email_status="delivered").count()
    email_failed_count = Booking.objects.filter(confirmation_email_status="failed").count()
    email_bounced_count = Booking.objects.filter(
        confirmation_email_status__in=("bounced", "complained")
    ).count()
    email_retry_count = (
        Booking.objects.filter(
            status__in=active_statuses,
            client__email__gt="",
            confirmation_email_sent_at__isnull=True,
            confirmation_email_attempts__lt=max_email_attempts(),
        )
        .filter(
            Q(confirmation_email_next_retry_at__isnull=True)
            | Q(confirmation_email_next_retry_at__lte=now)
        )
        .count()
    )
    problem_emails = (
        Booking.objects.filter(
            confirmation_email_status__in=("failed", "bounced", "complained")
        )
        .select_related("client")
        .order_by("-updated_at")[:20]
    )
    recent_events = BookingEvent.objects.select_related("booking").order_by("-created_at", "-pk")[:20]
    dirty_rooms = Room.objects.filter(housekeeping_status=Room.HousekeepingStatus.DIRTY).select_related("room_type").order_by("number")
    out_of_order_rooms = Room.objects.filter(housekeeping_status=Room.HousekeepingStatus.OUT_OF_ORDER).select_related("room_type").order_by("number")
    return render(
        request,
        "web/ops.html",
        {
            "today": today,
            "arrivals": arrivals,
            "departures": departures,
            "pending_holds": pending_holds,
            "stale_holds": stale_holds,
            "failed_calendar": failed_calendar,
            "missing_email": missing_email,
            "email_delivered_count": email_delivered_count,
            "email_failed_count": email_failed_count,
            "email_bounced_count": email_bounced_count,
            "email_retry_count": email_retry_count,
            "problem_emails": problem_emails,
            "recent_events": recent_events,
            "arrival_count": arrivals.count(),
            "departure_count": departures.count(),
            "pending_count": pending_holds.count(),
            "failed_calendar_count": failed_calendar.count(),
            "stale_calendar_count": stale_calendar,
            "calendar_drift_count": failed_calendar.count() + stale_calendar + not_synced_calendar,
            "dirty_rooms": dirty_rooms,
            "dirty_room_count": dirty_rooms.count(),
            "out_of_order_rooms": out_of_order_rooms,
        },
    )


def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001
        return JsonResponse({"status": "error", "database": "unavailable"}, status=503)
    return JsonResponse({"status": "ok", "time": timezone.now().isoformat()})
