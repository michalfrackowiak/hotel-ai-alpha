from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from . import views
from .forms import GuestSetPasswordForm

app_name = "web"

urlpatterns = [
    path("", views.home, name="home"),
    path("book/", views.book, name="book"),
    path("chat/", views.chat_page, name="chat"),
    path("voice/", views.voice_page, name="voice"),
    path("booking/", views.booking_lookup, name="booking-lookup"),
    path("booking/<str:reference>/", views.booking_detail, name="booking-detail"),
    path("my-bookings/", views.guest_bookings, name="guest-bookings"),
    path("my-bookings/login/<str:token>/", views.guest_login, name="guest-login"),
    path("my-bookings/password-login/", views.guest_password_login, name="guest-password-login"),
    path("my-bookings/register/", views.guest_register, name="guest-register"),
    path("my-bookings/google/", views.guest_google_login, name="guest-google-login"),
    path("my-bookings/google/callback/", views.guest_google_callback, name="guest-google-callback"),
    path("my-bookings/verify-email/<str:token>/", views.guest_verify_email, name="guest-verify-email"),
    path(
        "my-bookings/password-reset/",
        views.GuestPasswordResetView.as_view(),
        name="guest-password-reset",
    ),
    path(
        "my-bookings/password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="web/guest_password_reset_done.html",
        ),
        name="guest-password-reset-done",
    ),
    path(
        "my-bookings/password-reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="web/guest_password_reset_confirm.html",
            form_class=GuestSetPasswordForm,
            success_url=reverse_lazy("web:guest-password-reset-complete"),
        ),
        name="guest-password-reset-confirm",
    ),
    path(
        "my-bookings/password-reset/complete/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="web/guest_password_reset_complete.html",
        ),
        name="guest-password-reset-complete",
    ),
    path("my-bookings/password-change/", views.guest_password_change, name="guest-password-change"),
    path("my-bookings/password-change/done/", views.guest_password_change_done, name="guest-password-change-done"),
    path("my-bookings/profile/", views.guest_profile, name="guest-profile"),
    path("my-bookings/logout/", views.guest_logout, name="guest-logout"),
    path("ops/", views.ops_dashboard, name="ops"),
    path("calendar/<str:token>.ics", views.room_calendar, name="room-calendar"),
]
