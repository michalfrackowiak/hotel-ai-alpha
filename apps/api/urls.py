"""API URL configuration (mounted at /api/v1/)."""

from django.urls import path

from . import views

app_name = "api"

urlpatterns = [
    path("health/", views.HealthView.as_view(), name="health"),
    path("room-types/", views.RoomTypeListView.as_view(), name="room-types"),
    path("availability/", views.AvailabilityView.as_view(), name="availability"),
    path("pricing/", views.PricingView.as_view(), name="pricing"),
    path("bookings/", views.BookingCreateView.as_view(), name="booking-create"),
    path("bookings/<str:reference>/", views.BookingDetailView.as_view(), name="booking-detail"),
    path("bookings/<str:reference>/confirm/", views.BookingConfirmView.as_view(), name="booking-confirm"),
    path("bookings/<str:reference>/cancel/", views.BookingCancelView.as_view(), name="booking-cancel"),
    path(
        "bookings/<str:reference>/cancellation-preview/",
        views.BookingCancellationPreviewView.as_view(),
        name="booking-cancellation-preview",
    ),
    path("chat/sessions/", views.ChatSessionCreateView.as_view(), name="chat-session-create"),
    path("chat/sessions/<str:session_key>/", views.ChatSessionDetailView.as_view(), name="chat-session"),
    path(
        "chat/sessions/<str:session_key>/messages/",
        views.ChatMessageView.as_view(),
        name="chat-message",
    ),
    path("voice/config/", views.VoiceConfigView.as_view(), name="voice-config"),
    path("voice/transcribe/", views.VoiceTranscribeView.as_view(), name="voice-transcribe"),
    path("voice/speak/", views.VoiceSpeakView.as_view(), name="voice-speak"),
    path("email/webhooks/<str:provider>/", views.EmailWebhookView.as_view(), name="email-webhook"),
]
