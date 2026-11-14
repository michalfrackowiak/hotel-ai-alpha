"""Public REST API: availability, pricing, bookings, chat and voice."""

from __future__ import annotations

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework import generics, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, ScopedRateThrottle
from rest_framework.views import APIView

from apps.agents.engine import AgentEngine, AgentUnavailableError, conversation_history, start_conversation
from apps.agents.models import AgentSettings, Conversation
from apps.hotel.access import token_matches
from apps.hotel.availability import search_availability
from apps.hotel.booking_service import (
    BookingError,
    cancel_booking,
    confirm_booking,
    create_booking,
    get_booking,
    modify_booking,
)
from apps.hotel.cancellation import cancellation_preview
from apps.hotel.models import RoomType
from apps.hotel.pricing import StayError, quote
from apps.voice.providers import (
    SpeechProviderError,
    get_stt_provider,
    get_tts_provider,
    voice_client_config,
)

from .exceptions import ApiError
from .serializers import (
    AvailabilityQuerySerializer,
    BookingCancelSerializer,
    BookingCreateSerializer,
    BookingModifySerializer,
    BookingSerializer,
    ChatMessageCreateSerializer,
    ChatSessionCreateSerializer,
    PricingQuerySerializer,
    RoomTypeSerializer,
    VoiceSpeakSerializer,
    VoiceTranscribeSerializer,
)


def _raise_booking_error(exc: BookingError):
    raise ApiError(exc.code, exc.message, status_code=exc.status, details=exc.details)


def _booking_token(request) -> str:
    token = request.query_params.get("token") or request.headers.get("X-Booking-Token") or ""
    if not token:
        authorization = request.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
    return token


def _authorized_booking(request, reference: str):
    booking = get_booking(reference.upper())
    if not token_matches(booking, _booking_token(request)):
        raise BookingError(
            "booking_access_denied",
            _("Provide the booking access link or token from your confirmation."),
            status=403,
        )
    return booking


class HealthView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response(
            {
                "status": "ok",
                "time": timezone.now().isoformat(),
                "service": "hotel-ai-alpha",
            }
        )


class RoomTypeListView(generics.ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = RoomTypeSerializer
    pagination_class = None

    def get_queryset(self):
        return RoomType.objects.filter(is_active=True).prefetch_related("rooms")


class AvailabilityView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        serializer = AvailabilityQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        results = search_availability(
            data["check_in"],
            data["check_out"],
            guests=data.get("guests", 2),
            room_type_slug=data.get("room_type") or None,
        )
        return Response(
            {
                "query": {
                    "check_in": data["check_in"],
                    "check_out": data["check_out"],
                    "guests": data.get("guests", 2),
                },
                "results": results,
            }
        )


class PricingView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return self._quote(request.query_params)

    def post(self, request):
        return self._quote(request.data)

    def _quote(self, payload):
        serializer = PricingQuerySerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        room_type = RoomType.objects.filter(slug=data["room_type"], is_active=True).first()
        if room_type is None:
            raise ApiError("room_type_not_found", "The selected room type is not available.", status_code=404)
        rate_plan = None
        if data.get("rate_plan"):
            from apps.hotel.models import RatePlan

            rate_plan = RatePlan.objects.filter(slug=data["rate_plan"]).first()
            if rate_plan is None or not rate_plan.applies_to(room_type):
                raise ApiError("invalid_rate_plan", "The selected rate plan is not available.", status_code=400)
        try:
            priced = quote(
                room_type,
                data["check_in"],
                data["check_out"],
                adults=data.get("adults", 2),
                children=data.get("children", 0),
                rate_plan=rate_plan,
            )
        except StayError as exc:
            raise ApiError("invalid_stay", str(exc), details={"check_out": str(exc)}) from exc
        return Response({"quote": priced.as_dict()})


class BookingCreateView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = BookingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            booking = create_booking(
                room_type=data["room_type"],
                check_in=data["check_in"],
                check_out=data["check_out"],
                first_name=data["first_name"],
                last_name=data.get("last_name", ""),
                email=data.get("email", ""),
                phone=data.get("phone", ""),
                adults=data.get("adults", 2),
                children=data.get("children", 0),
                source=data.get("source", "web"),
                language=data.get("language", "en"),
                special_requests=data.get("special_requests", ""),
                rate_plan=data.get("rate_plan") or None,
            )
        except BookingError as exc:
            _raise_booking_error(exc)
        return Response(BookingSerializer(booking).data, status=status.HTTP_201_CREATED)


class BookingDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, reference: str):
        try:
            booking = _authorized_booking(request, reference)
        except BookingError as exc:
            _raise_booking_error(exc)
        return Response(BookingSerializer(booking).data)

    def patch(self, request, reference: str):
        serializer = BookingModifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            booking = _authorized_booking(request, reference)
            booking = modify_booking(
                booking.reference,
                check_in=data.get("check_in"),
                check_out=data.get("check_out"),
                room_type=data.get("room_type"),
                adults=data.get("adults"),
                children=data.get("children"),
                rate_plan=data.get("rate_plan"),
                actor="guest",
                channel=booking.source,
            )
        except BookingError as exc:
            _raise_booking_error(exc)
        return Response(BookingSerializer(booking).data)


class BookingCancellationPreviewView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, reference: str):
        try:
            booking = _authorized_booking(request, reference)
        except BookingError as exc:
            _raise_booking_error(exc)
        return Response({"preview": cancellation_preview(booking).as_dict()})


class BookingConfirmView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, reference: str):
        try:
            booking = _authorized_booking(request, reference)
            booking = confirm_booking(booking.reference, actor="guest", channel=booking.source)
        except BookingError as exc:
            _raise_booking_error(exc)
        return Response(BookingSerializer(booking).data)


class BookingCancelView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, reference: str):
        serializer = BookingCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            booking = _authorized_booking(request, reference)
            booking = cancel_booking(
                booking.reference,
                reason=data.get("reason", ""),
                note=data.get("note", ""),
                actor="guest",
                channel=booking.source,
            )
        except BookingError as exc:
            _raise_booking_error(exc)
        return Response(BookingSerializer(booking).data)


def _provider_info() -> dict:
    settings_obj = AgentSettings.load()
    record = settings_obj.default_provider
    if record is None:
        return {"name": "Demo policy", "type": "demo", "model": "demo-policy-v1"}
    return {"name": record.name, "type": record.provider_type, "model": record.model}


class ChatSessionCreateView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AnonRateThrottle, ScopedRateThrottle]
    throttle_scope = "chat"

    def post(self, request):
        serializer = ChatSessionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            conversation = start_conversation(
                channel=serializer.validated_data.get("channel", Conversation.Channel.CHAT),
                language=serializer.validated_data.get("language", "en"),
            )
        except AgentUnavailableError as exc:
            raise ApiError("agent_disabled", str(exc), status_code=503) from exc
        greeting = conversation.messages.order_by("created_at").first()
        return Response(
            {
                "session_key": conversation.session_key,
                "channel": conversation.channel,
                "language": conversation.language,
                "greeting": greeting.content if greeting else "",
                "provider": _provider_info(),
            },
            status=status.HTTP_201_CREATED,
        )


class ChatSessionDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, session_key: str):
        conversation = get_object_or_404(Conversation, session_key=session_key, is_active=True)
        return Response(
            {
                "session_key": conversation.session_key,
                "channel": conversation.channel,
                "language": conversation.language,
                "messages": conversation_history(conversation),
                "booking": BookingSerializer(conversation.booking).data if conversation.booking else None,
            }
        )


class ChatMessageView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AnonRateThrottle, ScopedRateThrottle]
    throttle_scope = "chat"

    def post(self, request, session_key: str):
        conversation = get_object_or_404(Conversation, session_key=session_key, is_active=True)
        serializer = ChatMessageCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        engine = AgentEngine(conversation)
        turn = engine.handle_user_message(serializer.validated_data["message"])
        conversation.refresh_from_db()
        booking = conversation.booking
        return Response(
            {
                "session_key": conversation.session_key,
                "language": conversation.language,
                "reply": turn.content,
                "tool_calls": turn.tool_calls,
                "provider": turn.provider,
                "model": turn.model,
                "latency_ms": turn.latency_ms,
                "booking_reference": turn.booking_reference,
                "booking": BookingSerializer(booking).data if booking else None,
                "error": turn.error,
            }
        )


class VoiceConfigView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        settings_obj = AgentSettings.load()
        config = voice_client_config(settings_obj)
        config["chat_enabled"] = settings_obj.chat_enabled
        config["provider"] = _provider_info()
        return Response(config)


class VoiceTranscribeView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AnonRateThrottle, ScopedRateThrottle]
    throttle_scope = "chat"

    def post(self, request):
        serializer = VoiceTranscribeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        audio = request.FILES.get("audio")
        if audio is None:
            raise ApiError("audio_required", "Attach an audio file in the 'audio' field.")
        settings_obj = AgentSettings.load()
        try:
            provider = get_stt_provider(settings_obj.voice_stt_provider)
            text = provider.transcribe(
                audio.read(),
                language=serializer.validated_data.get("language", "en"),
                filename=audio.name or "audio.webm",
                content_type=audio.content_type or "audio/webm",
            )
        except SpeechProviderError as exc:
            raise ApiError("stt_failed", str(exc), status_code=502) from exc
        return Response({"text": text})


class VoiceSpeakView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AnonRateThrottle, ScopedRateThrottle]
    throttle_scope = "chat"

    def post(self, request):
        serializer = VoiceSpeakSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        settings_obj = AgentSettings.load()
        try:
            provider = get_tts_provider(settings_obj.voice_tts_provider)
            audio, content_type = provider.synthesize(
                serializer.validated_data["text"],
                language=serializer.validated_data.get("language", "en"),
                voice=serializer.validated_data.get("voice", ""),
            )
        except SpeechProviderError as exc:
            raise ApiError("tts_failed", str(exc), status_code=502) from exc
        return HttpResponse(audio, content_type=content_type)


class EmailWebhookView(APIView):
    """Receive bounce/complaint/delivery events from an email provider."""

    permission_classes = [AllowAny]

    def post(self, request, provider: str):
        from django.conf import settings

        secret = getattr(settings, "EMAIL_WEBHOOK_SECRET", "")
        if secret:
            supplied = request.headers.get("X-Email-Webhook-Secret", "")
            if supplied != secret:
                raise ApiError("webhook_access_denied", "Invalid webhook secret.", status_code=403)

        from apps.hotel.email_webhooks import parse_email_webhook
        from apps.hotel.models import Booking

        data = request.data if isinstance(request.data, dict) else {}
        event = parse_email_webhook(provider, data)
        if event is None:
            return Response({"status": "ignored", "event": data.get("event") or data.get("type") or ""})

        booking = None
        if event.reference:
            booking = Booking.objects.filter(reference=event.reference).first()
            if booking is None:
                raise ApiError("booking_not_found", "Booking not found.", status_code=404)
        elif event.message_id:
            booking = Booking.objects.filter(confirmation_email_provider_id=event.message_id).first()
            if booking is None:
                return Response({"status": "ignored", "message_id": event.message_id})
        else:
            raise ApiError("invalid_webhook", "No booking reference or message id.", status_code=400)

        updates = {"confirmation_email_status": event.status}
        if event.status in {"bounced", "complained", "failed"}:
            updates["confirmation_email_error"] = event.reason or event.status
        Booking.objects.filter(pk=booking.pk).update(**updates)
        # Keep the delivery history in sync with the provider's final status.
        from apps.hotel.models import EmailDelivery

        deliveries = EmailDelivery.objects.filter(booking=booking)
        if event.message_id:
            matched = deliveries.filter(provider_message_id=event.message_id).order_by("-created_at").first()
        else:
            matched = None
        latest = matched or deliveries.order_by("-created_at").first()
        if latest is not None and event.status in EmailDelivery.Status.values:
            latest.status = event.status
            if event.status in {"bounced", "complained", "failed"}:
                latest.error_message = event.reason or event.status
            latest.save(update_fields=["status", "error_message", "updated_at"])
        return Response(
            {"status": "ok", "reference": booking.reference, "email_status": event.status}
        )
