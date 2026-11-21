"""Public website views (Django templates for the alpha)."""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth import views as auth_views
from django.db import connection
from django.db.models import Q, QuerySet
from django.forms.utils import ErrorDict
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
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
from apps.hotel.deposit import deposit_summary
from apps.hotel.guest_access import (
    GUEST_SESSION_KEY,
    allow_booking_lookup_attempt,
    allow_password_login_attempt,
    bookings_for_email,
    can_issue_email_verification,
    consume_email_verification_token,
    consume_login_token,
    guest_session_matches,
    issue_email_verification_token,
    issue_login_token,
    link_clients_for_guest_account,
    normalise_email,
    reserve_guest_ip_action,
    reserve_login_request,
    reserve_password_reset_request,
    session_email,
    verified_guest_account_for_email,
)
from apps.hotel.models import (
    Booking,
    BookingEvent,
    ChannelFeed,
    EmailDelivery,
    GuestAccount,
    InboundSmsMessage,
    RatePlan,
    Room,
    RoomBlock,
    RoomType,
    SmsDelivery,
)
from apps.hotel.models import (
    Client as HotelClient,
)
from apps.hotel.notifications import (
    max_email_attempts,
    max_sms_attempts,
    send_guest_email_verification,
    send_guest_login_email,
)
from apps.hotel.pricing import StayError

from .forms import (
    GuestAuthenticationForm,
    GuestEmailChangeForm,
    GuestPasswordChangeForm,
    GuestPasswordResetForm,
    GuestProfileForm,
    GuestRegistrationForm,
)

logger = logging.getLogger(__name__)
User = get_user_model()


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
        if not allow_booking_lookup_attempt(request.META.get("REMOTE_ADDR", "")):
            return render(
                request,
                "web/booking_lookup.html",
                {"reference": reference, "contact": contact, "lookup_error": True, "throttled": True},
                status=429,
            )
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


def _current_guest_email(request) -> str:
    """Return the signed-in guest email from Django auth or the magic-link session."""
    user = getattr(request, "user", None)
    if user and user.is_authenticated and not user.is_staff:
        email = normalise_email(getattr(user, "email", ""))
        if email:
            return email
    return session_email(request)


def _bookings_for_authenticated_guest(user) -> QuerySet[Booking]:
    """Bookings visible to an authenticated non-staff guest account."""
    email = normalise_email(getattr(user, "email", ""))
    account = getattr(user, "guest_account", None)
    if account is not None and getattr(account, "is_email_verified", False):
        linked_emails = {email} if email else set()
        linked_emails.update(
            normalise_email(e)
            for e in account.clients.exclude(email="").values_list("email", flat=True)
            if normalise_email(e)
        )
        email_query = Q()
        for linked_email in linked_emails:
            owner = verified_guest_account_for_email(linked_email)
            if owner is not None and owner.id != account.id:
                continue
            email_query |= Q(client__email__iexact=linked_email)
        query = Q(client__account=account) | (
            email_query & (Q(client__account__isnull=True) | Q(client__account=account))
        )
    elif email:
        # No verified account: only expose bookings whose client is not linked
        # to a different verified guest account.
        query = Q(client__email__iexact=email, client__account__isnull=True)
    else:
        return Booking.objects.none()
    return (
        Booking.objects.filter(query)
        .select_related("client", "room", "room__room_type", "rate_plan")
        .distinct()
        .order_by("-check_in", "-pk")
    )


def guest_bookings(request):
    """Guest account: sign in (password or magic link) or list bookings."""
    user = getattr(request, "user", None)
    is_authenticated_guest = bool(
        user and getattr(user, "is_authenticated", False) and not getattr(user, "is_staff", False)
    )
    email = _current_guest_email(request)
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
            _guest_login_context(request, sent=True, email=requested),
        )

    if email:
        bookings = _bookings_for_authenticated_guest(user) if is_authenticated_guest else bookings_for_email(email)
        return render(
            request,
            "web/guest_bookings.html",
            {
                "guest_email": email,
                "bookings": bookings,
                "today": timezone.localdate(),
                "is_authenticated_guest": is_authenticated_guest,
            },
        )
    return render(request, "web/guest_login.html", _guest_login_context(request))


def guest_login(request, token: str):
    email = consume_login_token(token)
    if email is None:
        return render(
            request,
            "web/guest_login.html",
            _guest_login_context(request, link_invalid=True),
            status=400,
        )
    if request.user.is_authenticated and not request.user.is_staff:
        auth_logout(request)
    request.session[GUEST_SESSION_KEY] = email
    request.session.cycle_key()
    account = verified_guest_account_for_email(email)
    if account is not None:
        link_clients_for_guest_account(account)
    return redirect("web:guest-bookings")


def guest_password_login(request):
    if request.method != "POST":
        return redirect("web:guest-bookings")

    submitted_email = normalise_email(request.POST.get("username"))
    client_ip = request.META.get("REMOTE_ADDR", "")
    attempt_allowed = allow_password_login_attempt(submitted_email, client_ip)

    form = GuestAuthenticationForm(request, data=request.POST)
    valid = form.is_valid()
    if attempt_allowed and valid:
        user = form.get_user()
        if user is None or user.is_staff or not getattr(user, "is_active", False):
            form.add_error(None, form.error_messages["invalid_login"])
        else:
            auth_login(request, user)
            request.session.pop(GUEST_SESSION_KEY, None)
            request.session.cycle_key()
            account = getattr(user, "guest_account", None)
            if account is not None:
                link_clients_for_guest_account(account)
            return redirect("web:guest-bookings")

    if not attempt_allowed:
        # Anti-enumeration: throttled attempts get the same generic failure.
        form._errors = ErrorDict()
        form.add_error(None, form.error_messages["invalid_login"])

    return render(
        request,
        "web/guest_login.html",
        _guest_login_context(
            request,
            login_form=form,
            register_form=GuestRegistrationForm(),
            password_login_failed=True,
        ),
        status=400,
    )


def _send_guest_verification(request, user, email: str | None = None) -> bool:
    """Issue and send a guest email verification link.

    When ``email`` is provided, verification is sent to that address (used for
    verified email changes). Returns True when a token was issued.
    """
    if not can_issue_email_verification(request, user):
        return False
    target_email = normalise_email(email or getattr(user, "email", ""))
    issued = issue_email_verification_token(user, email=target_email)
    if issued is None:
        return False
    _record, raw_token = issued
    verify_path = reverse("web:guest-verify-email", args=[raw_token])
    verify_url = f"{settings.HOTEL['public_url'].rstrip('/')}{verify_path}"
    send_guest_email_verification(
        target_email,
        verify_url,
        language=translation.get_language(),
    )
    return True


def guest_register(request):
    if request.method != "POST":
        return redirect("web:guest-bookings")
    client_ip = request.META.get("REMOTE_ADDR", "")
    if not reserve_guest_ip_action("register", client_ip):
        # Anti-abuse/anti-enumeration: throttled requests look like success.
        email = normalise_email(request.POST.get("email"))
        return render(
            request,
            "web/guest_verify_email.html",
            {"verification_sent": True, "email": email},
        )
    form = GuestRegistrationForm(request.POST)
    if form.is_valid():
        email = form.cleaned_data["email"]
        existing = User.objects.filter(email__iexact=email).first()
        owner_account = verified_guest_account_for_email(email)
        if owner_account is not None and (existing is None or owner_account.user_id != existing.pk):
            # Neutral: email is already associated with a different verified guest account
            # (including preserved historical client emails on linked bookings).
            return render(
                request,
                "web/guest_verify_email.html",
                {"verification_sent": True, "email": email},
            )
        if existing is None:
            user = User.objects.create_user(
                username=email,
                email=email,
                password=form.cleaned_data["password1"],
                first_name=form.cleaned_data.get("first_name", ""),
                last_name=form.cleaned_data.get("last_name", ""),
                is_staff=False,
                is_active=False,
            )
            GuestAccount.objects.get_or_create(user=user)
            _send_guest_verification(request, user)
        elif not existing.is_staff:
            account = getattr(existing, "guest_account", None)
            if account is None or not account.is_email_verified:
                # Allow completing verification for an existing unverified guest
                # account without revealing that the address is registered.
                if not existing.has_usable_password() or not account or not account.is_email_verified:
                    if existing.check_password(form.cleaned_data["password1"]) or not existing.has_usable_password():
                        existing.first_name = existing.first_name or form.cleaned_data.get("first_name", "")
                        existing.last_name = existing.last_name or form.cleaned_data.get("last_name", "")
                        if not existing.has_usable_password():
                            existing.set_password(form.cleaned_data["password1"])
                        existing.save()
                        GuestAccount.objects.get_or_create(user=existing)
                GuestAccount.objects.get_or_create(user=existing)
                _send_guest_verification(request, existing)
        # Staff/existing verified accounts: neutral response, no email leak.
        return render(
            request,
            "web/guest_verify_email.html",
            {"verification_sent": True, "email": email},
        )
    return render(
        request,
        "web/guest_login.html",
        _guest_login_context(
            request,
            login_form=GuestAuthenticationForm(request),
            register_form=form,
            registration_failed=True,
        ),
        status=400,
    )


def guest_verify_email(request, token: str):
    email = consume_email_verification_token(token)
    if email is None:
        return render(
            request,
            "web/guest_verify_email.html",
            {"link_invalid": True},
            status=400,
        )
    return render(
        request,
        "web/guest_verify_email.html",
        {"verified": True, "email": email},
    )


class GuestPasswordResetView(auth_views.PasswordResetView):
    template_name = "web/guest_password_reset.html"
    email_template_name = "hotel/email/guest_password_reset.txt"
    subject_template_name = "hotel/email/guest_password_reset_subject.txt"
    html_email_template_name = "hotel/email/guest_password_reset.html"
    form_class = GuestPasswordResetForm
    success_url = reverse_lazy("web:guest-password-reset-done")

    def get_extra_email_context(self):
        return {"hotel": settings.HOTEL}

    def form_valid(self, form):
        email = normalise_email(form.cleaned_data.get("email"))
        if reserve_password_reset_request(email, self.request.META.get("REMOTE_ADDR", "")):
            return super().form_valid(form)
        # Anti-enumeration/anti-spam: behave as if the request was accepted.
        return redirect(self.get_success_url())


class GuestPasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    """Password reset confirm that also marks guest email verified and links clients."""

    def form_valid(self, form):
        response = super().form_valid(form)
        user = getattr(form, "user", None) or getattr(self, "user", None)
        if user is None or getattr(user, "is_staff", False) or not getattr(user, "is_active", False):
            return response

        account, _created = GuestAccount.objects.get_or_create(user=user)
        if account.email_verified_at is None:
            account.email_verified_at = timezone.now()
            account.save(update_fields=["email_verified_at", "updated_at"])
        link_clients_for_guest_account(account)
        return response


def guest_password_change(request):
    user = request.user
    if not user.is_authenticated or user.is_staff:
        return redirect("web:guest-bookings")
    if request.method == "POST":
        attempt_allowed = allow_password_login_attempt(
            normalise_email(getattr(user, "email", "")),
            request.META.get("REMOTE_ADDR", ""),
        )
        form = GuestPasswordChangeForm(user, request.POST)
        if attempt_allowed and form.is_valid():
            form.save()
            update_session_auth_hash(request, form.user)
            return redirect("web:guest-password-change-done")
        if not attempt_allowed:
            form._errors = ErrorDict()
            form.cleaned_data = {}
            form.add_error(None, _("Too many attempts. Please try again later."))
    else:
        form = GuestPasswordChangeForm(user)
    return render(request, "web/guest_password_change.html", {"form": form})


def guest_password_change_done(request):
    user = request.user
    if not user.is_authenticated or user.is_staff:
        return redirect("web:guest-bookings")
    return render(request, "web/guest_password_change_done.html")


def guest_profile(request):
    user = request.user
    if not user.is_authenticated or user.is_staff:
        return redirect("web:guest-bookings")

    account, _created = GuestAccount.objects.get_or_create(user=user)
    linked_clients = list(account.clients.all().order_by("-updated_at", "-pk"))
    primary_client = linked_clients[0] if linked_clients else None
    saved = False
    email_change_sent = False
    email_change_target = ""

    form = GuestProfileForm(
        initial={
            "first_name": user.first_name,
            "last_name": user.last_name,
            "phone": (primary_client.phone if primary_client else ""),
            "language": (primary_client.language if primary_client else "pl"),
            "marketing_consent": bool(primary_client.marketing_consent) if primary_client else False,
        }
    )
    email_change_form = GuestEmailChangeForm(user=user)

    if request.method == "POST":
        if request.POST.get("form") == "email_change":
            email_change_form = GuestEmailChangeForm(request.POST, user=user)
            if email_change_form.is_valid():
                new_email = email_change_form.cleaned_data["new_email"]
                target_taken = User.objects.filter(email__iexact=new_email).exclude(pk=user.pk).exists()
                owner_account = verified_guest_account_for_email(new_email)
                if owner_account is not None and owner_account.user_id != user.pk:
                    target_taken = True
                # Best-effort anti-spam: issue/send only when eligible and cooldown allows.
                # Response remains neutral to avoid revealing whether the target email is
                # unavailable or delivery was throttled.
                if not target_taken:
                    _send_guest_verification(request, user, email=new_email)
                email_change_sent = True
                email_change_target = new_email
                email_change_form = GuestEmailChangeForm(user=user)
        else:
            form = GuestProfileForm(request.POST)
            if form.is_valid():
                user.first_name = (form.cleaned_data.get("first_name") or "").strip()
                user.last_name = (form.cleaned_data.get("last_name") or "").strip()
                user.save(update_fields=["first_name", "last_name"])

                phone = (form.cleaned_data.get("phone") or "").strip()
                language = (form.cleaned_data.get("language") or "").strip() or "pl"
                if language not in {"en", "pl"}:
                    language = "pl"
                marketing_consent = bool(form.cleaned_data.get("marketing_consent"))

                # Keep linkage/profile coherent across transitions: ensure any matching
                # unlinked clients for a verified account are linked before propagation.
                if account.is_email_verified:
                    link_clients_for_guest_account(account)
                    linked_clients = list(account.clients.all().order_by("-updated_at", "-pk"))

                for client_obj in linked_clients:
                    update_fields = []
                    if client_obj.first_name != user.first_name and user.first_name:
                        client_obj.first_name = user.first_name
                        update_fields.append("first_name")
                    if client_obj.last_name != user.last_name:
                        client_obj.last_name = user.last_name
                        update_fields.append("last_name")
                    if client_obj.language != language:
                        client_obj.language = language
                        update_fields.append("language")
                    if client_obj.marketing_consent != marketing_consent:
                        client_obj.marketing_consent = marketing_consent
                        update_fields.append("marketing_consent")
                    # Keep client contact valid: phone can be cleared only when email exists.
                    new_phone = phone
                    if not new_phone and not (client_obj.email or "").strip():
                        new_phone = client_obj.phone
                    if client_obj.phone != new_phone:
                        client_obj.phone = new_phone
                        update_fields.append("phone")
                    if update_fields:
                        update_fields.append("updated_at")
                        client_obj.save(update_fields=update_fields)

                if not linked_clients:
                    contact_email = normalise_email(getattr(user, "email", ""))
                    if contact_email:
                        owner_account = verified_guest_account_for_email(contact_email)
                        conflict = owner_account is not None and owner_account.user_id != user.pk
                        if not conflict:
                            conflict = (
                                HotelClient.objects.filter(email__iexact=contact_email)
                                .exclude(account__isnull=True)
                                .exclude(account=account)
                                .exists()
                            )
                        if not conflict:
                            linked_clients = [
                                HotelClient.objects.create(
                                    account=account,
                                    first_name=(user.first_name or "").strip() or (contact_email.split("@")[0] or "Guest"),
                                    last_name=(user.last_name or "").strip(),
                                    email=contact_email,
                                    phone=phone,
                                    language=language,
                                    marketing_consent=marketing_consent,
                                )
                            ]
                saved = True

    return render(
        request,
        "web/guest_profile.html",
        {
            "form": form,
            "email_change_form": email_change_form,
            "guest_account": account,
            "saved": saved,
            "email_change_sent": email_change_sent,
            "email_change_target": email_change_target,
            "guest_email": (user.email or "").strip(),
        },
    )


@require_POST
def guest_logout(request):
    request.session.pop(GUEST_SESSION_KEY, None)
    if request.user.is_authenticated and not request.user.is_staff:
        auth_logout(request)
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
            "deposit": deposit_summary(booking),
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
    email_history = (
        EmailDelivery.objects.select_related("booking")
        .order_by("-created_at", "-pk")[:20]
    )
    sms_delivered_count = Booking.objects.filter(confirmation_sms_status="delivered").count()
    sms_failed_count = Booking.objects.filter(confirmation_sms_status__in=("failed", "undelivered")).count()
    sms_sent_count = Booking.objects.filter(confirmation_sms_status="sent").count()
    sms_retry_count = (
        Booking.objects.filter(
            status__in=active_statuses,
            client__phone__gt="",
            confirmation_sms_sent_at__isnull=True,
            confirmation_sms_attempts__lt=max_sms_attempts(),
        )
        .filter(
            Q(confirmation_sms_next_retry_at__isnull=True)
            | Q(confirmation_sms_next_retry_at__lte=now)
        )
        .count()
    )
    problem_sms = (
        Booking.objects.filter(confirmation_sms_status__in=("failed", "undelivered"))
        .select_related("client")
        .order_by("-updated_at")[:20]
    )
    sms_history = (
        SmsDelivery.objects.select_related("booking")
        .order_by("-created_at", "-pk")[:20]
    )
    inbound_sms = (
        InboundSmsMessage.objects.select_related("booking", "booking__client")
        .order_by("-received_at", "-created_at")[:20]
    )
    payment_attention_statuses = (Booking.PaymentStatus.UNPAID, Booking.PaymentStatus.PARTIALLY_PAID)
    payment_attention = (
        Booking.objects.filter(
            status__in=(Booking.Status.PENDING, *active_statuses),
            payment_status__in=payment_attention_statuses,
        )
        .select_related("client", "room", "room__room_type")
        .order_by("check_in", "reference")[:20]
    )
    payment_attention_count = Booking.objects.filter(
        status__in=(Booking.Status.PENDING, *active_statuses),
        payment_status__in=payment_attention_statuses,
    ).count()

    recent_events = BookingEvent.objects.select_related("booking").order_by("-created_at", "-pk")[:20]
    dirty_rooms = Room.objects.filter(housekeeping_status=Room.HousekeepingStatus.DIRTY).select_related("room_type").order_by("number")
    out_of_order_rooms = Room.objects.filter(housekeeping_status=Room.HousekeepingStatus.OUT_OF_ORDER).select_related("room_type").order_by("number")
    channel_feeds = (
        ChannelFeed.objects.filter(is_active=True)
        .select_related("room", "room__room_type")
        .order_by("room__number", "name")
    )
    channel_feed_errors = channel_feeds.exclude(last_error="")
    channel_conflicts = sum(feed.conflict_count for feed in channel_feeds)
    upcoming_blocks = (
        RoomBlock.objects.filter(check_out__gte=today)
        .select_related("room", "room__room_type", "feed")
        .order_by("check_in", "room__number")[:20]
    )
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
            "email_history": email_history,
            "sms_delivered_count": sms_delivered_count,
            "sms_failed_count": sms_failed_count,
            "sms_sent_count": sms_sent_count,
            "sms_retry_count": sms_retry_count,
            "payment_attention": payment_attention,
            "payment_attention_count": payment_attention_count,
            "problem_sms": problem_sms,
            "sms_history": sms_history,
            "inbound_sms": inbound_sms,
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
            "channel_feeds": channel_feeds,
            "channel_feed_errors": channel_feed_errors,
            "channel_feed_error_count": channel_feed_errors.count(),
            "channel_conflicts": channel_conflicts,
            "upcoming_blocks": upcoming_blocks,
        },
    )


def room_calendar(request, token: str):
    """Serve a room's signed iCal export feed for an OTA subscription."""
    from apps.hotel.channel_feeds import resolve_feed_token
    from apps.hotel.ics import build_room_calendar

    room = resolve_feed_token(token)
    if room is None:
        raise Http404("Unknown calendar feed.")
    response = HttpResponse(build_room_calendar(room), content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = f'inline; filename="room-{room.number}.ics"'
    return response


GOOGLE_OAUTH_STATE_KEY = "google_oauth_state"


def _google_oauth_enabled() -> bool:
    return bool(getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "") and getattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", ""))


def _guest_login_context(request, **extra):
    context = {
        "login_form": extra.pop("login_form", GuestAuthenticationForm(request)),
        "register_form": extra.pop("register_form", GuestRegistrationForm()),
        "google_oauth_enabled": _google_oauth_enabled(),
    }
    context.update(extra)
    return context


def _google_oauth_redirect_uri(request) -> str:
    base = (settings.HOTEL.get("public_url") or "").rstrip("/")
    return f"{base}{reverse('web:guest-google-callback')}"


def guest_google_login(request):
    if not _google_oauth_enabled():
        return render(
            request,
            "web/guest_login.html",
            _guest_login_context(request, google_error=_("Google sign-in is not configured.")),
            status=400,
        )
    if not reserve_guest_ip_action("google-login", request.META.get("REMOTE_ADDR", "")):
        return render(
            request,
            "web/guest_login.html",
            _guest_login_context(
                request,
                google_error=_("Google sign-in could not be completed. Please try again."),
            ),
            status=400,
        )

    state = secrets.token_urlsafe(32)
    request.session[GOOGLE_OAUTH_STATE_KEY] = state
    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": _google_oauth_redirect_uri(request),
        "response_type": "code",
        "scope": " ".join(getattr(settings, "GOOGLE_OAUTH_SCOPES", ["openid", "email", "profile"])),
        "state": state,
        "prompt": "select_account",
        "access_type": "online",
    }
    return redirect(f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}")


def _google_fetch_userinfo(code: str, redirect_uri: str) -> dict | None:
    token_url = "https://oauth2.googleapis.com/token"
    userinfo_url = "https://openidconnect.googleapis.com/v1/userinfo"
    try:
        with httpx.Client(timeout=10.0) as client:
            token_response = client.post(
                token_url,
                data={
                    "code": code,
                    "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                    "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            token_response.raise_for_status()
            access_token = token_response.json().get("access_token")
            if not access_token:
                return None
            user_response = client.get(
                userinfo_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            user_response.raise_for_status()
            return user_response.json()
    except Exception:  # noqa: BLE001
        logger.exception("Google OAuth exchange failed")
        return None


def guest_google_callback(request):
    if not _google_oauth_enabled():
        return redirect("web:guest-bookings")

    if not reserve_guest_ip_action("google-callback", request.META.get("REMOTE_ADDR", "")):
        return render(
            request,
            "web/guest_login.html",
            _guest_login_context(
                request,
                google_error=_("Google sign-in could not be completed. Please try again."),
            ),
            status=400,
        )

    error = request.GET.get("error")
    if error:
        return render(
            request,
            "web/guest_login.html",
            _guest_login_context(
                request,
                google_error=_("Google sign-in was cancelled or could not be completed."),
            ),
            status=400,
        )

    expected_state = request.session.pop(GOOGLE_OAUTH_STATE_KEY, "")
    state = request.GET.get("state") or ""
    if not expected_state or not state or not secrets.compare_digest(expected_state, state):
        return render(
            request,
            "web/guest_login.html",
            _guest_login_context(
                request,
                google_error=_("Google sign-in could not be verified. Please try again."),
            ),
            status=400,
        )

    code = request.GET.get("code") or ""
    userinfo = _google_fetch_userinfo(code, _google_oauth_redirect_uri(request)) if code else None
    email = normalise_email((userinfo or {}).get("email"))
    email_verified = bool((userinfo or {}).get("email_verified"))
    if not email or not email_verified:
        return render(
            request,
            "web/guest_login.html",
            _guest_login_context(
                request,
                google_error=_("Google did not provide a verified email address."),
            ),
            status=400,
        )

    existing = User.objects.filter(email__iexact=email).order_by("pk").first()
    if existing and existing.is_staff:
        # Avoid account-type enumeration: treat like other guest auth failures.
        return render(
            request,
            "web/guest_login.html",
            _guest_login_context(
                request,
                google_error=_("Google sign-in was cancelled or could not be completed."),
            ),
            status=400,
        )

    owner_account = verified_guest_account_for_email(email)
    if owner_account is not None and (existing is None or owner_account.user_id != existing.pk):
        return render(
            request,
            "web/guest_login.html",
            _guest_login_context(
                request,
                google_error=_("Google sign-in could not be completed. Please try again."),
            ),
            status=400,
        )

    now = timezone.now()
    if existing is None:
        base_username = email
        username = base_username
        suffix = 1
        while User.objects.filter(username=username).exists():
            suffix += 1
            username = f"{base_username}-{suffix}"
        existing = User.objects.create_user(
            username=username,
            email=email,
            first_name=((userinfo or {}).get("given_name") or "").strip()[:150],
            last_name=((userinfo or {}).get("family_name") or "").strip()[:150],
            is_staff=False,
            is_active=True,
        )
        existing.set_unusable_password()
        existing.save(update_fields=["password"])
    else:
        updates = []
        if not existing.is_active:
            existing.is_active = True
            updates.append("is_active")
        if not existing.first_name and (userinfo or {}).get("given_name"):
            existing.first_name = str((userinfo or {}).get("given_name") or "").strip()[:150]
            updates.append("first_name")
        if not existing.last_name and (userinfo or {}).get("family_name"):
            existing.last_name = str((userinfo or {}).get("family_name") or "").strip()[:150]
            updates.append("last_name")
        if updates:
            existing.save(update_fields=updates)

    account, _created = GuestAccount.objects.get_or_create(user=existing)
    if account.email_verified_at is None:
        account.email_verified_at = now
        account.save(update_fields=["email_verified_at", "updated_at"])
    link_clients_for_guest_account(account)

    auth_login(request, existing, backend="django.contrib.auth.backends.ModelBackend")
    request.session.pop(GUEST_SESSION_KEY, None)
    request.session.cycle_key()
    return redirect("web:guest-bookings")


def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001
        return JsonResponse({"status": "error", "database": "unavailable"}, status=503)
    return JsonResponse({"status": "ok", "time": timezone.now().isoformat()})
