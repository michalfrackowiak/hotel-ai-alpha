"""OpenLine telephony control plane.

Maps OpenLine webhook events to domain ``PhoneCall`` records and conversations.
Handlers must be idempotent: OpenLine retries webhooks and deliveries may arrive
out of order.
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.agents.engine import AgentUnavailableError, start_conversation
from apps.agents.models import Conversation
from apps.hotel.models import Client

from .models import PhoneCall

logger = logging.getLogger(__name__)

E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_EMAIL_RE = re.compile(r"([A-Z0-9._%+-])[A-Z0-9._%+-]*(@[A-Z0-9.-]+\.[A-Z]{2,})", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\w)(\+?\d[\d\s().-]{6,}\d)")
_REFERENCE_RE = re.compile(r"\b([A-Z]{2}-?[A-Z0-9]{2})[A-Z0-9]{2,}([A-Z0-9]{2})\b")


def redact_pii(value: str | None) -> str:
    """Best-effort PII redaction for logs and diagnostics."""
    text = str(value or "")
    if not text:
        return ""
    text = _EMAIL_RE.sub(lambda m: f"{m.group(1)}***{m.group(2)}", text)
    text = _REFERENCE_RE.sub(lambda m: f"{m.group(1)}***{m.group(2)}", text)

    def _mask_phone(match: re.Match[str]) -> str:
        raw = match.group(1)
        digits = re.sub(r"\D", "", raw)
        if len(digits) < 7:
            return raw
        return raw[:2] + "***" + raw[-2:]

    return _PHONE_RE.sub(_mask_phone, text)


def normalize_phone_number(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    cleaned = re.sub(r"[^\d+]", "", raw)
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if not cleaned.startswith("+") and cleaned.isdigit():
        # Keep as-is; cannot safely guess country code.
        return cleaned
    return cleaned


def _cap(value: str | None, max_length: int) -> str:
    text = (value or "").strip()
    if len(text) <= max_length:
        return text
    return text[:max_length]


def is_e164(value: str | None) -> bool:
    return bool(E164_RE.match((value or "").strip()))


def resolve_client_by_phone(number: str | None) -> Client | None:
    normalized = normalize_phone_number(number)
    if not normalized:
        return None
    candidates = [normalized]
    digits = re.sub(r"\D", "", normalized)
    if digits:
        candidates.append("+" + digits)
        candidates.append(digits)
    return Client.objects.filter(phone__in=candidates).order_by("pk").first()


_STATUS_BY_EVENT = {
    "call.ringing": PhoneCall.Status.RINGING,
    "call.new": PhoneCall.Status.RINGING,
    "call.dialing": PhoneCall.Status.RINGING,
    "call.connecting": PhoneCall.Status.RINGING,
    "call.answered": PhoneCall.Status.ANSWERED,
    "call.active": PhoneCall.Status.ACTIVE,
    "call.audio_processing": PhoneCall.Status.ACTIVE,
    "call.holding": PhoneCall.Status.ACTIVE,
    "call.held": PhoneCall.Status.ACTIVE,
    "call.resumed": PhoneCall.Status.ACTIVE,
    "call.unhold": PhoneCall.Status.ACTIVE,
    "call.ended": PhoneCall.Status.ENDED,
    "call.disconnected": PhoneCall.Status.ENDED,
    "call.hangup": PhoneCall.Status.ENDED,
    "call.completed": PhoneCall.Status.ENDED,
    "call.rejected": PhoneCall.Status.FAILED,
    "call.failed": PhoneCall.Status.FAILED,
}

_STATUS_BY_STATE = {
    "ringing": PhoneCall.Status.RINGING,
    "queued": PhoneCall.Status.RINGING,
    "dialing": PhoneCall.Status.RINGING,
    "answered": PhoneCall.Status.ANSWERED,
    "active": PhoneCall.Status.ACTIVE,
    "holding": PhoneCall.Status.ACTIVE,
    "held": PhoneCall.Status.ACTIVE,
    "ended": PhoneCall.Status.ENDED,
    "completed": PhoneCall.Status.ENDED,
    "disconnected": PhoneCall.Status.ENDED,
    "rejected": PhoneCall.Status.FAILED,
    "failed": PhoneCall.Status.FAILED,
    "missed": PhoneCall.Status.FAILED,
}

_TERMINAL = {PhoneCall.Status.ENDED, PhoneCall.Status.FAILED, PhoneCall.Status.TRANSFERRED}


def _status_for(event_type: str, provider_status: str) -> str | None:
    status = _STATUS_BY_EVENT.get((event_type or "").lower())
    if status:
        return status
    return _STATUS_BY_STATE.get((provider_status or "").lower())


def _is_inbound(direction: str, event_type: str) -> bool:
    direction = (direction or "").lower()
    if direction in {"inbound", "incoming"}:
        return True
    if direction in {"outbound", "outgoing"}:
        return False
    return (event_type or "").lower().startswith("call.ringing")


def _extract_call_snapshot(payload: dict) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    call = payload.get("call") if isinstance(payload.get("call"), dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    nested = data.get("call") if isinstance(data.get("call"), dict) else {}

    def pick(*keys: str) -> str:
        for scope in (call, nested, data, payload):
            for key in keys:
                value = scope.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, int) and not isinstance(value, bool):
                    return str(value)
        return ""

    return {
        "id": pick("id", "callId", "call_id", "callSid", "call_sid"),
        "direction": pick("direction"),
        "from_number": pick("from", "fromNumber", "from_number", "caller"),
        "to_number": pick("to", "toNumber", "to_number", "callee"),
        "status": pick("status", "state", "callStatus"),
        "started_at": pick("startedAt", "started_at", "createdAt", "created_at"),
        "answered_at": pick("answeredAt", "answered_at"),
        "ended_at": pick("endedAt", "ended_at"),
        "duration_seconds": pick("durationSeconds", "duration_seconds", "duration"),
    }


def _parse_datetime(value: str):
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        from django.utils.dateparse import parse_datetime

        parsed = parse_datetime(text)
        if parsed is None:
            return None
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        return parsed
    except Exception:
        return None


def _parse_duration(value: str):
    if not value:
        return None
    try:
        return max(int(float(value)), 0)
    except Exception:
        return None


def _ensure_conversation(phone_call: PhoneCall) -> Conversation | None:
    if phone_call.conversation_id:
        return phone_call.conversation
    if not phone_voice_enabled():
        return None
    try:
        conversation = start_conversation(channel=Conversation.Channel.PHONE, language="pl")
    except AgentUnavailableError:
        logger.info("Phone conversation not started for %s: phone disabled", phone_call.external_id)
        return None
    except Exception:
        logger.exception("Failed to start phone conversation for %s", phone_call.external_id)
        return None

    client = resolve_client_by_phone(phone_call.from_number)
    if client is not None:
        conversation.client = client
        conversation.save(update_fields=["client", "updated_at"])
    phone_call.conversation = conversation
    phone_call.save(update_fields=["conversation", "updated_at"])
    return conversation


@transaction.atomic
def handle_openline_event(*, event, call, payload: dict) -> PhoneCall | None:
    """Create/update a PhoneCall from a stored OpenLine webhook event."""
    payload = payload if isinstance(payload, dict) else {}
    external_id = _cap(
        getattr(call, "openline_id", "") or _extract_call_snapshot(payload).get("id", ""),
        128,
    )
    if not external_id:
        return None

    snapshot = _extract_call_snapshot(payload)
    event_type = _cap(getattr(event, "event_type", "") or "", 128)
    provider_status = _cap(snapshot.get("status") or getattr(call, "status", "") or "", 64)
    domain_status = _status_for(event_type, provider_status)

    defaults = {
        "provider": PhoneCall.Provider.OPENLINE,
        "direction": PhoneCall.Direction.INBOUND
        if _is_inbound(snapshot.get("direction") or getattr(call, "direction", ""), event_type)
        else PhoneCall.Direction.OUTBOUND,
        "from_number": _cap(normalize_phone_number(snapshot.get("from_number") or getattr(call, "from_number", "")), 64),
        "to_number": _cap(normalize_phone_number(snapshot.get("to_number") or getattr(call, "to_number", "")), 64),
        "status": domain_status or PhoneCall.Status.RINGING,
        "metadata": {"last_openline_event": event_type},
    }
    try:
        with transaction.atomic():
            phone_call, _created = PhoneCall.objects.select_for_update().get_or_create(
                external_id=external_id,
                defaults=defaults,
            )
    except IntegrityError:
        phone_call = PhoneCall.objects.select_for_update().get(external_id=external_id)

    changed: list[str] = []
    direction = PhoneCall.Direction.INBOUND if _is_inbound(
        snapshot.get("direction") or getattr(call, "direction", ""), event_type
    ) else PhoneCall.Direction.OUTBOUND
    if phone_call.direction != direction:
        phone_call.direction = direction
        changed.append("direction")

    from_number = _cap(normalize_phone_number(snapshot.get("from_number") or getattr(call, "from_number", "")), 64)
    to_number = _cap(normalize_phone_number(snapshot.get("to_number") or getattr(call, "to_number", "")), 64)
    if from_number and phone_call.from_number != from_number:
        phone_call.from_number = from_number
        changed.append("from_number")
    if to_number and phone_call.to_number != to_number:
        phone_call.to_number = to_number
        changed.append("to_number")

    if domain_status and phone_call.status not in _TERMINAL:
        if phone_call.status != domain_status:
            phone_call.status = domain_status
            changed.append("status")
    elif domain_status in _TERMINAL and phone_call.status not in _TERMINAL:
        # Terminal states can be applied even if we missed intermediate ones.
        phone_call.status = domain_status
        changed.append("status")
    # Never overwrite a terminal domain status (especially TRANSFERRED) with a
    # later terminal webhook that arrived out of order.

    started_at = _parse_datetime(snapshot.get("started_at"))
    answered_at = _parse_datetime(snapshot.get("answered_at"))
    ended_at = _parse_datetime(snapshot.get("ended_at"))
    if started_at and phone_call.started_at is None:
        phone_call.started_at = started_at
        changed.append("started_at")
    if answered_at and phone_call.answered_at is None:
        phone_call.answered_at = answered_at
        changed.append("answered_at")
    if ended_at and phone_call.ended_at is None:
        phone_call.ended_at = ended_at
        changed.append("ended_at")
    if phone_call.started_at is None and phone_call.status != PhoneCall.Status.RINGING:
        phone_call.started_at = timezone.now()
        changed.append("started_at")
    if phone_call.answered_at is None and phone_call.status in {
        PhoneCall.Status.ANSWERED,
        PhoneCall.Status.ACTIVE,
        PhoneCall.Status.TRANSFERRED,
    }:
        phone_call.answered_at = timezone.now()
        changed.append("answered_at")
    if phone_call.ended_at is None and phone_call.status in _TERMINAL:
        phone_call.ended_at = timezone.now()
        changed.append("ended_at")

    duration = _parse_duration(snapshot.get("duration_seconds"))
    if duration is not None and phone_call.duration_seconds != duration:
        phone_call.duration_seconds = duration
        changed.append("duration_seconds")
    elif phone_call.ended_at and phone_call.answered_at and phone_call.duration_seconds is None:
        phone_call.duration_seconds = max(
            int((phone_call.ended_at - phone_call.answered_at).total_seconds()), 0
        )
        changed.append("duration_seconds")

    metadata = dict(phone_call.metadata or {})
    meaningful_before = {
        k: metadata.get(k)
        for k in ("last_openline_event", "last_openline_status")
    }
    metadata["last_openline_event"] = event_type
    metadata["last_openline_status"] = provider_status
    meaningful_after = {
        k: metadata.get(k)
        for k in ("last_openline_event", "last_openline_status")
    }
    if meaningful_after != meaningful_before:
        metadata["last_openline_event_at"] = timezone.now().isoformat()
        phone_call.metadata = metadata
        changed.append("metadata")

    if phone_call.status not in _TERMINAL and phone_call.conversation_id is None:
        conversation = _ensure_conversation(phone_call)
        if conversation is not None and phone_call.conversation_id == conversation.pk and "conversation" not in changed:
            changed.append("conversation")

    if changed:
        changed.append("updated_at")
        phone_call.save(update_fields=list(dict.fromkeys(changed)))

    return phone_call


STREAM_TOKEN_METADATA_KEY = "stream_token"


def ensure_stream_token(phone_call: PhoneCall) -> str:
    """Return a per-call stream token, creating and persisting it if missing."""
    metadata = dict(phone_call.metadata or {})
    token = str(metadata.get(STREAM_TOKEN_METADATA_KEY) or "").strip()
    if token:
        return token
    token = secrets.token_urlsafe(32)
    metadata[STREAM_TOKEN_METADATA_KEY] = token
    phone_call.metadata = metadata
    if phone_call.pk:
        phone_call.save(update_fields=["metadata", "updated_at"])
    return token


def verify_stream_token(phone_call: PhoneCall, provided: str | None) -> bool:
    expected = str((phone_call.metadata or {}).get(STREAM_TOKEN_METADATA_KEY) or "").strip()
    candidate = str(provided or "").strip()
    if not expected or not candidate:
        return False
    return hmac.compare_digest(expected, candidate)


def phone_voice_enabled() -> bool:
    if not getattr(settings, "VOICE_PHONE_ENABLED", False):
        return False
    try:
        from apps.agents.models import AgentSettings

        return bool(AgentSettings.load().phone_enabled)
    except Exception:
        logger.warning("Could not load AgentSettings.phone_enabled; treating phone as disabled")
        return False


def should_attach_stream(phone_call: PhoneCall) -> bool:
    if phone_call.status in _TERMINAL:
        return False
    if not phone_voice_enabled():
        return False
    return (getattr(settings, "OPENLINE_INCOMING_STREAM_MODE", "webhook_attach") or "").strip() == "webhook_attach"


def build_stream_url(*, token: str = "") -> str:
    explicit = (getattr(settings, "VOICE_PUBLIC_WSS_URL", "") or "").strip()
    if explicit:
        base_url = explicit.rstrip("/")
    else:
        base = (getattr(settings, "HOTEL_PUBLIC_URL", "") or "").strip().rstrip("/")
        if base.startswith("https://"):
            base_url = "wss://" + base[len("https://") :] + "/ws/voice/openline/"
        elif base.startswith("http://"):
            base_url = "ws://" + base[len("http://") :] + "/ws/voice/openline/"
        else:
            return ""
    if token:
        from urllib.parse import urlencode

        return f"{base_url}?{urlencode({'token': token})}"
    return base_url
