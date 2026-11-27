"""OpenLine webhook handlers for phone voice integration."""

from __future__ import annotations

import logging

from django.conf import settings
from django_openline.signals import on_event

from .telephony import build_stream_url, ensure_stream_token, handle_openline_event, should_attach_stream

logger = logging.getLogger(__name__)


@on_event()
def handle_openline_webhook(*, event, call, payload, **kwargs):
    """Sync every OpenLine webhook into the domain PhoneCall record."""
    phone_call = handle_openline_event(event=event, call=call, payload=payload or {})
    if phone_call is None:
        return

    event_type = (getattr(event, "event_type", "") or "").lower()
    if event_type not in {"call.ringing", "call.new"}:
        return
    if phone_call.direction != "inbound":
        return
    if not should_attach_stream(phone_call):
        return

    stream_token = ensure_stream_token(phone_call)
    stream_url = build_stream_url(token=stream_token)
    if not stream_url:
        logger.warning("VOICE_PUBLIC_WSS_URL/HOTEL_PUBLIC_URL missing; cannot attach OpenLine stream")
        return

    try:
        from django_openline.services import attach_stream

        attach_stream(
            phone_call.external_id,
            url=stream_url,
            format=(getattr(settings, "OPENLINE_DEFAULT_STREAM_FORMAT", "twilio") or "twilio"),
            parameters={
                "phoneCallId": str(phone_call.pk),
                "conversationId": str(phone_call.conversation_id or ""),
                "streamToken": stream_token,
            },
        )
        logger.info("Attached OpenLine stream for %s", phone_call.external_id)
    except Exception:
        logger.exception("Failed to attach OpenLine stream for %s", phone_call.external_id)
