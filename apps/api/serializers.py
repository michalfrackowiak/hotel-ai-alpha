"""API serializers for rooms, availability, pricing, bookings and chat."""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.agents.models import Conversation
from apps.hotel.access import booking_manage_url, make_booking_token
from apps.hotel.cancellation import cancellation_preview
from apps.hotel.models import Booking, Client, RoomType
from apps.hotel.pricing import StayError, validate_stay

LANGUAGE_CHOICES = ["en", "pl"]


class RoomTypeSerializer(serializers.ModelSerializer):
    rooms_available = serializers.SerializerMethodField()

    class Meta:
        model = RoomType
        fields = (
            "id",
            "slug",
            "name",
            "short_description",
            "description",
            "capacity",
            "base_price",
            "currency",
            "size_sqm",
            "bed_configuration",
            "amenities",
            "rooms_available",
        )

    def get_rooms_available(self, obj: RoomType) -> int:
        return obj.rooms.filter(is_active=True).count()


class AvailabilityQuerySerializer(serializers.Serializer):
    check_in = serializers.DateField()
    check_out = serializers.DateField()
    guests = serializers.IntegerField(min_value=1, max_value=12, default=2, required=False)
    room_type = serializers.SlugField(required=False, allow_blank=True)

    def validate(self, attrs):
        try:
            validate_stay(attrs["check_in"], attrs["check_out"])
        except StayError as exc:
            raise serializers.ValidationError({"check_out": str(exc)}) from exc
        return attrs


class PricingQuerySerializer(serializers.Serializer):
    room_type = serializers.SlugField()
    check_in = serializers.DateField()
    check_out = serializers.DateField()
    adults = serializers.IntegerField(min_value=1, max_value=8, default=2, required=False)
    children = serializers.IntegerField(min_value=0, max_value=6, default=0, required=False)
    rate_plan = serializers.SlugField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        try:
            validate_stay(attrs["check_in"], attrs["check_out"])
        except StayError as exc:
            raise serializers.ValidationError({"check_out": str(exc)}) from exc
        return attrs


class ClientSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = Client
        fields = ("id", "first_name", "last_name", "full_name", "email", "phone", "language", "country")


class BookingSerializer(serializers.ModelSerializer):
    guest = ClientSerializer(source="client", read_only=True)
    room = serializers.CharField(source="room.number", read_only=True)
    room_type = serializers.CharField(source="room.room_type.slug", read_only=True)
    room_type_name = serializers.CharField(source="room.room_type.name", read_only=True)
    rate_plan = serializers.CharField(source="rate_plan.slug", read_only=True, allow_null=True)
    nights = serializers.IntegerField(read_only=True)
    can_confirm = serializers.BooleanField(read_only=True)
    can_cancel = serializers.BooleanField(read_only=True)
    can_modify = serializers.BooleanField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    source_display = serializers.CharField(source="get_source_display", read_only=True)
    cancellation_reason_display = serializers.CharField(source="get_cancellation_reason_display", read_only=True)
    cancelled_by_display = serializers.CharField(source="get_cancelled_by_display", read_only=True)
    confirmation_email_status = serializers.CharField(source="get_confirmation_email_status_display", read_only=True)
    cancellation = serializers.SerializerMethodField()
    calendar = serializers.SerializerMethodField()
    manage_token = serializers.SerializerMethodField()
    manage_url = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = (
            "reference",
            "status",
            "status_display",
            "source",
            "source_display",
            "language",
            "guest",
            "room",
            "room_type",
            "room_type_name",
            "rate_plan",
            "check_in",
            "check_out",
            "nights",
            "adults",
            "children",
            "currency",
            "total_price",
            "price_breakdown",
            "special_requests",
            "hold_expires_at",
            "can_confirm",
            "can_cancel",
            "can_modify",
            "calendar",
            "cancellation",
            "cancellation_fee",
            "cancellation_reason",
            "cancellation_reason_display",
            "cancellation_note",
            "cancelled_by",
            "cancelled_by_display",
            "manage_token",
            "manage_url",
            "created_at",
            "confirmed_at",
            "cancelled_at",
            "confirmation_email_status",
            "confirmation_email_error",
        )

    def get_manage_token(self, obj: Booking) -> str:
        return make_booking_token(obj)

    def get_manage_url(self, obj: Booking) -> str:
        return booking_manage_url(obj)

    def get_cancellation(self, obj: Booking):
        return cancellation_preview(obj).as_dict()

    def get_calendar(self, obj: Booking):
        record = obj.calendar_events.order_by("-created_at").first()
        if record is None:
            return None
        return {
            "status": record.status,
            "event_id": record.event_id,
            "last_synced_at": record.last_synced_at,
            "error": record.error_message,
        }


class BookingCreateSerializer(serializers.Serializer):
    room_type = serializers.SlugField()
    rate_plan = serializers.SlugField(required=False, allow_blank=True, default="")
    check_in = serializers.DateField()
    check_out = serializers.DateField()
    adults = serializers.IntegerField(min_value=1, max_value=8, default=2, required=False)
    children = serializers.IntegerField(min_value=0, max_value=6, default=0, required=False)
    first_name = serializers.CharField(max_length=80)
    last_name = serializers.CharField(max_length=80, required=False, allow_blank=True, default="")
    email = serializers.EmailField(required=False, allow_blank=True, default="")
    phone = serializers.CharField(max_length=40, required=False, allow_blank=True, default="")
    language = serializers.ChoiceField(choices=LANGUAGE_CHOICES, default="en", required=False)
    source = serializers.ChoiceField(
        choices=["web", "chat", "voice", "api"], default="web", required=False
    )
    special_requests = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        try:
            validate_stay(attrs["check_in"], attrs["check_out"])
        except DjangoValidationError as exc:
            message = str(exc.messages[0]) if getattr(exc, "messages", None) else str(exc)
            raise serializers.ValidationError({"check_out": message}) from exc
        if not attrs.get("email") and not attrs.get("phone"):
            raise serializers.ValidationError(
                {"email": "Provide an email address or a phone number."}
            )
        return attrs


class BookingModifySerializer(serializers.Serializer):
    check_in = serializers.DateField(required=False)
    check_out = serializers.DateField(required=False)
    room_type = serializers.SlugField(required=False)
    rate_plan = serializers.SlugField(required=False, allow_blank=True)
    adults = serializers.IntegerField(min_value=1, max_value=8, required=False)
    children = serializers.IntegerField(min_value=0, max_value=6, required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Provide at least one field to change.")
        return attrs


class BookingCancelSerializer(serializers.Serializer):
    reason = serializers.ChoiceField(
        choices=Booking.CancellationReason.values,
        required=False,
        allow_blank=True,
        default="",
    )
    note = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")


class ChatSessionCreateSerializer(serializers.Serializer):
    language = serializers.ChoiceField(choices=LANGUAGE_CHOICES, default="en", required=False)
    channel = serializers.ChoiceField(
        choices=Conversation.Channel.values, default=Conversation.Channel.CHAT, required=False
    )


class ChatMessageCreateSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=4000, trim_whitespace=True)


class VoiceTranscribeSerializer(serializers.Serializer):
    language = serializers.ChoiceField(choices=LANGUAGE_CHOICES, default="en", required=False)


class VoiceSpeakSerializer(serializers.Serializer):
    text = serializers.CharField(max_length=2000)
    language = serializers.ChoiceField(choices=LANGUAGE_CHOICES, default="en", required=False)
    voice = serializers.CharField(max_length=80, required=False, allow_blank=True, default="")
