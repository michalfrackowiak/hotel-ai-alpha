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
)
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
    BookingCreateSerializer,
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
        try:
            priced = quote(
                room_type,
                data["check_in"],
                data["check_out"],
                adults=data.get("adults", 2),
                children=data.get("children", 0),
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


class BookingConfirmView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, reference: str):
        try:
            booking = _authorized_booking(request, reference)
            booking = confirm_booking(booking.reference)
        except BookingError as exc:
            _raise_booking_error(exc)
        return Response(BookingSerializer(booking).data)


class BookingCancelView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, reference: str):
        try:
            booking = _authorized_booking(request, reference)
            booking = cancel_booking(booking.reference)
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
