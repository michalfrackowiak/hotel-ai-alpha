"""Normalise bounce, complaint and delivery webhooks from email providers.

Providers report the same three outcomes with different payloads. This module
turns each provider's native shape into one :class:`EmailWebhookEvent` so the
API view only has to look up the booking and update its delivery status.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

STATUS_BY_EVENT = {
    "delivered": "delivered",
    "delivery": "delivered",
    "bounce": "bounced",
    "bounced": "bounced",
    "complaint": "complained",
    "complained": "complained",
    "spamcomplaint": "complained",
    "failed": "failed",
}

SES_PROVIDERS = {"ses", "sns", "amazon"}


@dataclass
class EmailWebhookEvent:
    status: str
    reference: str = ""
    message_id: str = ""
    reason: str = ""


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _normalise_status(value) -> str:
    return STATUS_BY_EVENT.get(str(value or "").strip().lower(), "")


def _unwrap_sns(payload: dict) -> dict:
    """SNS wraps the SES notification as a JSON string in ``Message``."""
    if payload.get("Type") == "Notification" and isinstance(payload.get("Message"), str):
        try:
            inner = json.loads(payload["Message"])
        except json.JSONDecodeError:
            return payload
        if isinstance(inner, dict):
            return inner
    return payload


def _reference_from_tags(tags) -> str:
    if isinstance(tags, dict):
        value = tags.get("booking_reference")
        if isinstance(value, list):
            value = value[0] if value else ""
        return str(value or "").strip().upper()
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, dict) and tag.get("Name") == "booking_reference":
                return str(tag.get("Value") or "").strip().upper()
    return ""


def _parse_generic(payload: dict) -> EmailWebhookEvent | None:
    status = _normalise_status(payload.get("event") or payload.get("type"))
    if not status:
        return None
    reference = str(payload.get("reference") or payload.get("booking_reference") or "").strip().upper()
    message_id = str(payload.get("message_id") or payload.get("messageId") or "").strip()
    reason = str(payload.get("reason") or payload.get("message") or "").strip()
    return EmailWebhookEvent(status=status, reference=reference, message_id=message_id, reason=reason)


def _parse_ses(payload: dict) -> EmailWebhookEvent | None:
    payload = _unwrap_sns(payload)
    raw_status = payload.get("notificationType") or payload.get("eventType")
    if not raw_status:
        return _parse_generic(payload)

    status = _normalise_status(raw_status)
    if not status:
        return None

    mail = _as_dict(payload.get("mail"))
    message_id = str(mail.get("messageId") or payload.get("messageId") or "").strip()
    reference = _reference_from_tags(mail.get("tags")) or _reference_from_tags(payload.get("tags"))

    reason = ""
    if status == "bounced":
        bounce = _as_dict(payload.get("bounce"))
        recipients = bounce.get("bouncedRecipients") or []
        if recipients and isinstance(recipients[0], dict):
            reason = str(recipients[0].get("diagnosticCode") or "")
        reason = reason or str(bounce.get("bounceType") or "bounced")
    elif status == "complained":
        complaint = _as_dict(payload.get("complaint"))
        reason = str(complaint.get("complaintFeedbackType") or "complained")

    return EmailWebhookEvent(status=status, reference=reference, message_id=message_id, reason=reason)


def _parse_postmark(payload: dict) -> EmailWebhookEvent | None:
    raw_status = payload.get("RecordType") or payload.get("record_type")
    if not raw_status:
        return _parse_generic(payload)

    status = _normalise_status(raw_status)
    if not status:
        return None

    message_id = str(payload.get("MessageID") or payload.get("MessageId") or "").strip()
    metadata = _as_dict(payload.get("Metadata") or payload.get("metadata"))
    reference = str(
        metadata.get("booking_reference") or payload.get("reference") or ""
    ).strip().upper()

    reason = str(payload.get("Description") or payload.get("Details") or "").strip()
    if status == "bounced" and not reason:
        reason = str(payload.get("Type") or "bounced")
    elif status == "complained" and not reason:
        reason = "complained"

    return EmailWebhookEvent(status=status, reference=reference, message_id=message_id, reason=reason)


def parse_email_webhook(provider: str, payload) -> EmailWebhookEvent | None:
    """Return a normalised event, or ``None`` when the payload is not actionable."""
    data = _as_dict(payload)
    provider = (provider or "").strip().lower()
    if provider in SES_PROVIDERS:
        return _parse_ses(data)
    if provider == "postmark":
        return _parse_postmark(data)
    return _parse_generic(data)
