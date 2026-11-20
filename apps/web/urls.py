from django.urls import path

from . import views

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
    path("my-bookings/logout/", views.guest_logout, name="guest-logout"),
    path("ops/", views.ops_dashboard, name="ops"),
    path("calendar/<str:token>.ics", views.room_calendar, name="room-calendar"),
]
