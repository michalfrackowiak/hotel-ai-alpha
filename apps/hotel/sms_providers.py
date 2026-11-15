"""Pluggable SMS delivery providers.

The default provider is ``console``: it records the message and logs it, so the
booking scenario works offline and in the test suite. Set ``SMS_PROVIDER=twilio``
together with the Twilio credentials for real delivery. Every provider accepts a
provider-agnostic :class:`SmsMessage` and returns an :class:`SmsDeliveryResult`,
so callers never need to know which backend ran.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass
class SmsMessage:
    """A single outbound SMS, independent of the delivery backend."""

    to: str
    body: str
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class SmsDeliveryResult:
    sent: bool
    provider_message_id: str = ""
    error: str = ""


class SmsProvider(Protocol):
    name: str

    def send(self, message: SmsMessage) -> SmsDeliveryResult:
        ...


class ConsoleSmsProvider:
    """Offline provider: records messages in memory and writes them to the log."""

    name = "console"
    outbox: list[SmsMessage] = []

    def send(self, message: SmsMessage) -> SmsDeliveryResult:
        to = (message.to or "").strip()
        if not to:
            return SmsDeliveryResult(sent=False, error="no phone number")
        self.__class__.outbox.append(message)
        logger.info("SMS to %s: %s", to, message.body)
        return SmsDeliveryResult(sent=True, provider_message_id=f"console-{uuid.uuid4().hex[:12]}")

    @classmethod
    def reset(cls) -> None:
        cls.outbox.clear()


class TwilioSmsProvider:
    name = "twilio"

    def send(self, message: SmsMessage) -> SmsDeliveryResult:
        account_sid = getattr(settings, "TWILIO_ACCOUNT_SID", "")
        auth_token = getattr(settings, "TWILIO_AUTH_TOKEN", "")
        from_number = getattr(settings, "TWILIO_FROM_NUMBER", "")
        if not (account_sid and auth_token and from_number):
            return SmsDeliveryResult(sent=False, error="Twilio credentials are not configured")

        to = (message.to or "").strip()
        if not to:
            return SmsDeliveryResult(sent=False, error="no phone number")

        endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
        payload = urllib.parse.urlencode(
            {"From": from_number, "To": to, "Body": message.body}
        ).encode()
        request = urllib.request.Request(endpoint, data=payload, method="POST")
        credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
        request.add_header("Authorization", f"Basic {credentials}")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            return SmsDeliveryResult(
                sent=False,
                error=f"Twilio HTTP {exc.code}: {exc.read().decode()[:200]}",
            )
        except Exception as exc:  # noqa: BLE001 - one failure must not break the booking
            return SmsDeliveryResult(sent=False, error=str(exc))

        message_id = ""
        try:
            message_id = json.loads(raw).get("sid", "")
        except Exception:  # noqa: BLE001
            pass
        return SmsDeliveryResult(sent=True, provider_message_id=message_id)


def get_sms_provider() -> SmsProvider:
    name = getattr(settings, "SMS_PROVIDER", "console").lower().strip()
    if name == "twilio":
        return TwilioSmsProvider()
    return ConsoleSmsProvider()
