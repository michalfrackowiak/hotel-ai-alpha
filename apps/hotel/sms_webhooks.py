"""Normalise SMS delivery receipts and inbound replies from providers.

Twilio (and similar services) report delivery outcomes and guest replies with
provider-specific payloads. This module turns those payloads into a small set of
events so the API view can update delivery history and record inbound messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

RECEIPT_STATUS = {
    "sent": "sent",
    "delivered": "delivered",
    "failed": "failed",
    "undelivered": "undelivered",
    "canceled": "failed",
    "cancelled": "failed",
}

INBOUND_STATUS = {"received", "receiving"}


@dataclass
class SmsReceiptEvent:
    status: str
    message_id: str = ""
    reference: str = ""
    reason: str = ""
    to: str = ""


@dataclass
class SmsInboundEvent:
    message_id: str = ""
    from_number: str = ""
    to_number: str = ""
    body: str = ""


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _normalise_receipt_status(value: Any) -> str:
    return RECEIPT_STATUS.get(str(value or "").strip().lower(), "")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _reference_from_payload(data: dict) -> str:
    # Allow generic/custom payloads to pass a booking reference explicitly.
    for key in ("booking_reference", "reference", "BookingReference"):
        ref = _text(data.get(key)).upper()
        if ref:
            return ref
    return ""


def _parse_generic(data: dict) -> SmsReceiptEvent | SmsInboundEvent | None:
    # Generic JSON payloads: explicit event/type decides the meaning.
    raw_event = _text(data.get("event") or data.get("type")).lower()
    if raw_event in {"inbound", "reply", "received", "incoming", "sms_inbound"}:
        body = _text(data.get("body") or data.get("Body") or data.get("text"))
        from_number = _text(data.get("from") or data.get("From") or data.get("from_number"))
        if not body and not from_number:
            return None
        return SmsInboundEvent(
            message_id=_text(data.get("message_id") or data.get("MessageSid") or data.get("sid")),
            from_number=from_number,
            to_number=_text(data.get("to") or data.get("To") or data.get("to_number")),
            body=body,
        )

    status = _normalise_receipt_status(
        data.get("status") or data.get("message_status") or data.get("MessageStatus") or raw_event
    )
    if not status:
        return None
    return SmsReceiptEvent(
        status=status,
        message_id=_text(data.get("message_id") or data.get("MessageSid") or data.get("sid")),
        reference=_reference_from_payload(data),
        reason=_text(data.get("reason") or data.get("error") or data.get("ErrorMessage")),
        to=_text(data.get("to") or data.get("To")),
    )


def _parse_twilio(data: dict) -> SmsReceiptEvent | SmsInboundEvent | None:
    message_id = _text(data.get("MessageSid") or data.get("SmsSid"))
    to_number = _text(data.get("To"))
    from_number = _text(data.get("From"))
    body = _text(data.get("Body"))
    sms_status = _text(data.get("SmsStatus") or data.get("MessageStatus")).lower()

    # Inbound replies carry a body and are marked as received/incoming.
    if body and (sms_status in INBOUND_STATUS or "MessageStatus" not in data):
        return SmsInboundEvent(
            message_id=message_id,
            from_number=from_number,
            to_number=to_number,
            body=body,
        )

    status = _normalise_receipt_status(data.get("MessageStatus") or data.get("SmsStatus"))
    if not status:
        # Some providers post inbound without an explicit status string.
        if body and from_number:
            return SmsInboundEvent(
                message_id=message_id,
                from_number=from_number,
                to_number=to_number,
                body=body,
            )
        return _parse_generic(data)

    reason_parts = [_text(data.get("ErrorCode")), _text(data.get("ErrorMessage"))]
    reason = " ".join(part for part in reason_parts if part).strip()
    return SmsReceiptEvent(
        status=status,
        message_id=message_id,
        reference=_reference_from_payload(data),
        reason=reason,
        to=to_number,
    )


def parse_sms_webhook(provider: str, payload: Any) -> SmsReceiptEvent | SmsInboundEvent | None:
    """Return a normalised event, or ``None`` when the payload is not actionable."""
    data = _as_dict(payload)
    provider = _text(provider).lower()
    if provider == "twilio":
        return _parse_twilio(data)
    if provider == "generic":
        return _parse_generic(data)
    # Unknown providers: try Twilio-shaped form posts, then generic JSON.
    event = _parse_twilio(data)
    if event is not None:
        return event
    return _parse_generic(data)
