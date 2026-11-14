"""Pluggable email delivery providers.

The default provider is SMTP (Django's email backend). Optional transactional
providers (Amazon SES, Postmark) are used only when ``EMAIL_PROVIDER`` is set
to ``ses`` or ``postmark`` and their credentials are present. Every provider
accepts a provider-agnostic :class:`EmailMessage` and returns a
:class:`DeliveryResult`, so callers never need to know which backend ran.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Protocol

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger(__name__)


@dataclass
class Attachment:
    filename: str
    content: bytes
    mimetype: str = "application/octet-stream"


@dataclass
class EmailMessage:
    """A single outbound email, independent of the delivery backend."""

    to: str
    subject: str
    text_body: str
    html_body: str = ""
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class DeliveryResult:
    sent: bool
    provider_message_id: str = ""
    error: str = ""


class EmailProvider(Protocol):
    name: str

    def send(self, message: EmailMessage) -> DeliveryResult:
        ...


class SmtpProvider:
    name = "smtp"

    def send(self, message: EmailMessage) -> DeliveryResult:
        to = (message.to or "").strip()
        if not to:
            return DeliveryResult(sent=False, error="no email address")
        email = EmailMultiAlternatives(
            subject=message.subject,
            body=message.text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[to],
        )
        if message.html_body:
            email.attach_alternative(message.html_body, "text/html")
        for attachment in message.attachments:
            email.attach(attachment.filename, attachment.content, attachment.mimetype)
        try:
            email.send(fail_silently=False)
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(sent=False, error=str(exc))
        return DeliveryResult(sent=True)


def _ses_credentials() -> tuple[str, str, str]:
    return (
        getattr(settings, "SES_ACCESS_KEY_ID", ""),
        getattr(settings, "SES_SECRET_ACCESS_KEY", ""),
        getattr(settings, "SES_REGION", "eu-central-1"),
    )


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


class SesProvider:
    name = "ses"

    def send(self, message: EmailMessage) -> DeliveryResult:
        access_key, secret_key, region = _ses_credentials()
        if not access_key or not secret_key:
            return DeliveryResult(sent=False, error="SES credentials are not configured")

        to = (message.to or "").strip()
        if not to:
            return DeliveryResult(sent=False, error="no email address")
        if message.attachments:
            # SES v1 SendEmail cannot carry attachments; use SendRawEmail if that
            # becomes necessary. The magic-link email never has attachments.
            logger.warning("SES provider dropped %s attachment(s)", len(message.attachments))

        # Minimal SES v1 SendEmail request (form-encoded, signed with SigV4).
        host = f"email.{region}.amazonaws.com"
        endpoint = f"https://{host}/"
        params = {
            "Action": "SendEmail",
            "Version": "2010-12-01",
            "Source": settings.DEFAULT_FROM_EMAIL,
            "Destination.ToAddresses.member.1": to,
            "Message.Subject.Data": message.subject,
            "Message.Subject.Charset": "UTF-8",
            "Message.Body.Text.Data": message.text_body,
            "Message.Body.Text.Charset": "UTF-8",
        }
        if message.html_body:
            params["Message.Body.Html.Data"] = message.html_body
            params["Message.Body.Html.Charset"] = "UTF-8"
        body = urllib.parse.urlencode(params).encode()

        now = dt.datetime.now(dt.UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        service = "ses"
        credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
        payload_hash = hashlib.sha256(body).hexdigest()
        canonical_headers = f"host:{host}\nx-amz-date:{amz_date}\n"
        signed_headers = "host;x-amz-date"
        canonical_request = "\n".join(
            ["POST", "/", "", canonical_headers, signed_headers, payload_hash]
        )
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        k_date = _sign(("AWS4" + secret_key).encode(), date_stamp)
        k_region = _sign(k_date, region)
        k_service = _sign(k_region, service)
        k_signing = _sign(k_service, "aws4_request")
        signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
        authorization = (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        request = urllib.request.Request(endpoint, data=body, method="POST")
        request.add_header("Host", host)
        request.add_header("X-Amz-Date", amz_date)
        request.add_header("Authorization", authorization)
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            return DeliveryResult(sent=False, error=f"SES HTTP {exc.code}: {exc.read().decode()[:200]}")
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(sent=False, error=str(exc))

        message_id = ""
        try:
            root = ET.fromstring(raw)
            for elem in root.iter():
                if elem.tag.endswith("MessageId"):
                    message_id = elem.text or ""
        except Exception:  # noqa: BLE001
            pass
        return DeliveryResult(sent=True, provider_message_id=message_id)


class PostmarkProvider:
    name = "postmark"

    def send(self, message: EmailMessage) -> DeliveryResult:
        token = getattr(settings, "POSTMARK_SERVER_TOKEN", "")
        if not token:
            return DeliveryResult(sent=False, error="Postmark server token is not configured")

        to = (message.to or "").strip()
        if not to:
            return DeliveryResult(sent=False, error="no email address")

        payload = {
            "From": settings.DEFAULT_FROM_EMAIL,
            "To": to,
            "Subject": message.subject,
            "TextBody": message.text_body,
            "HtmlBody": message.html_body,
            "Attachments": [
                {
                    "Name": attachment.filename,
                    "Content": base64.b64encode(attachment.content).decode(),
                    "ContentType": attachment.mimetype,
                }
                for attachment in message.attachments
            ],
        }
        request = urllib.request.Request(
            "https://api.postmarkapp.com/email",
            data=json.dumps(payload).encode(),
            method="POST",
        )
        request.add_header("X-Postmark-Server-Token", token)
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            return DeliveryResult(sent=False, error=f"Postmark HTTP {exc.code}: {exc.read().decode()[:200]}")
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(sent=False, error=str(exc))

        message_id = ""
        try:
            message_id = json.loads(raw).get("MessageID", "")
        except Exception:  # noqa: BLE001
            pass
        return DeliveryResult(sent=True, provider_message_id=message_id)


def get_email_provider() -> EmailProvider:
    name = getattr(settings, "EMAIL_PROVIDER", "smtp").lower().strip()
    if name == "ses":
        return SesProvider()
    if name == "postmark":
        return PostmarkProvider()
    return SmtpProvider()
