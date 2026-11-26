"""Phone voice service helpers."""

from __future__ import annotations

import logging
import secrets

from django.conf import settings
from django.utils import timezone
from django_openline import services as openline_services
from django_openline.exceptions import OpenLineError

from apps.agents.engine import AgentUnavailableError, start_conversation
from apps.agents.models import Conversation

from .models import PhoneCall
from .telephony import STREAM_TOKEN_METADATA_KEY, build_stream_url, normalize_phone_number

logger = logging.getLogger(__name__)


class PhoneServiceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def place_outbound_call(*, to_number: str, device_id: str = "", reference: str = "", metadata: dict | None = None):
    """Place an outbound OpenLine call and create a domain PhoneCall record."""
    if not getattr(settings, "VOICE_PHONE_ENABLED", False):
        raise PhoneServiceError("phone_disabled", "Phone voice is disabled.")
    # Fail fast if the agent is unavailable before touching the provider.
    try:
        from apps.agents.models import AgentSettings

        agent_settings = AgentSettings.load()
    except Exception as exc:
        raise PhoneServiceError("phone_unavailable", "Phone voice is unavailable.") from exc
    if not agent_settings.is_enabled or not getattr(agent_settings, "phone_enabled", False):
        raise PhoneServiceError("phone_unavailable", "The phone assistant is currently disabled.")

    to_number = normalize_phone_number(to_number)
    if not to_number:
        raise PhoneServiceError("invalid_phone", "A destination phone number is required.")

    stream_token = secrets.token_urlsafe(32)
    stream_url = build_stream_url(token=stream_token)
    if not stream_url:
        raise PhoneServiceError("missing_stream_url", "VOICE_PUBLIC_WSS_URL or HOTEL_PUBLIC_URL must be configured.")

    metadata = dict(metadata or {})
    metadata["outbound_requested_at"] = timezone.now().isoformat()
    metadata[STREAM_TOKEN_METADATA_KEY] = stream_token

    try:
        call = openline_services.place_call(
            to=to_number,
            device_id=device_id or None,
            stream_url=stream_url,
            stream_format=(getattr(settings, "OPENLINE_DEFAULT_STREAM_FORMAT", "twilio") or "twilio"),
            reference=reference,
            metadata=metadata,
        )
    except OpenLineError as exc:
        logger.warning("Outbound OpenLine call failed: %s", exc)
        raise PhoneServiceError("openline_error", "Could not place the outbound call.") from exc

    external_id = getattr(call, "openline_id", "") or str(getattr(call, "id", ""))
    if not external_id:
        raise PhoneServiceError("missing_call_id", "OpenLine did not return a call id.")

    phone_call, created = PhoneCall.objects.get_or_create(
        external_id=external_id,
        defaults={
            "provider": PhoneCall.Provider.OPENLINE,
            "direction": PhoneCall.Direction.OUTBOUND,
            "to_number": to_number,
            "from_number": normalize_phone_number(getattr(call, "from_number", "")),
            "status": PhoneCall.Status.RINGING,
            "metadata": metadata,
        },
    )

    if phone_call.conversation_id is None:
        try:
            conversation = start_conversation(channel=Conversation.Channel.PHONE, language="pl")
        except AgentUnavailableError as exc:
            if created:
                phone_call.delete()
            raise PhoneServiceError("phone_unavailable", str(exc)) from exc
        except Exception:
            if created:
                phone_call.delete()
            raise
        phone_call.conversation = conversation
        phone_call.save(update_fields=["conversation", "updated_at"])

    updates = []
    if phone_call.direction != PhoneCall.Direction.OUTBOUND:
        phone_call.direction = PhoneCall.Direction.OUTBOUND
        updates.append("direction")
    if to_number and phone_call.to_number != to_number:
        phone_call.to_number = to_number
        updates.append("to_number")
    merged = dict(phone_call.metadata or {})
    existing_token = merged.get(STREAM_TOKEN_METADATA_KEY)
    merged.update(metadata)
    if existing_token:
        merged[STREAM_TOKEN_METADATA_KEY] = existing_token
    if merged != (phone_call.metadata or {}):
        phone_call.metadata = merged
        updates.append("metadata")
    if updates:
        updates.append("updated_at")
        phone_call.save(update_fields=list(dict.fromkeys(updates)))
    return phone_call