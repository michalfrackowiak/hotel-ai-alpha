"""Pluggable confirmation-email delivery providers.

The default provider is SMTP (Django's email backend). Optional transactional
providers (Amazon SES, Postmark) are used only when ``EMAIL_PROVIDER`` is set
to ``ses`` or ``postmark`` and their credentials are present. Every provider
returns a ``(sent, provider_message_id, error)`` triple so the caller can
record delivery status without knowing which backend ran.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from .ics import build_booking_ics
from .models import Booking

logger = logging.getLogger(__name__)


@dataclass
class DeliveryResult:
    sent: bool
    provider_message_id: str = ""
    error: str = ""


class EmailProvider(Protocol):
    name: str

    def send(self, booking: Booking, *, subject: str, text_body: str, html_body: str) -> DeliveryResult:
        ...


class SmtpProvider:
    name = "smtp"

    def send(self, booking, *, subject, text_body, html_body):
        email = (booking.client.email or "").strip()
        if not email:
            return DeliveryResult(sent=False, error="no email address")
        message = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[email],
        )
        message.attach_alternative(html_body, "text/html")
        message.attach(f"{booking.reference}.ics", build_booking_ics(booking), "text/calendar")
        try:
            message.send(fail_silently=False)
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(sent=False, error=str(exc))
        return DeliveryResult(sent=True)


def _ses_credentials():
    return getattr(settings, "SES_ACCESS_KEY_ID", ""), getattr(settings, "SES_SECRET_ACCESS_KEY", ""), getattr(settings, "SES_REGION", "eu-central-1")


class SesProvider:
    name = "ses"

    def send(self, booking, *, subject, text_body, html_body):
        import hashlib
        import hmac
        import urllib.parse
        import urllib.request
        import xml.etree.ElementTree as ET

        access_key, secret_key, region = _ses_credentials()
        if not access_key or not secret_key:
            return DeliveryResult(sent=False, error="SES credentials are not configured")

        email = (booking.client.email or "").strip()
        if not email:
            return DeliveryResult(sent=False, error="no email address")

        # Minimal SES v1 SendEmail request (form-encoded, signed with SigV4).
        host = f"email.{region}.amazonaws.com"
        endpoint = f"https://{host}/"
        params = {
            "Action": "SendEmail",
            "Version": "2010-12-01",
            "Source": settings.DEFAULT_FROM_EMAIL,
            "Destination.ToAddresses.member.1": email,
            "Message.Subject.Data": subject,
            "Message.Subject.Charset": "UTF-8",
            "Message.Body.Text.Data": text_body,
            "Message.Body.Text.Charset": "UTF-8",
            "Message.Body.Html.Data": html_body,
            "Message.Body.Html.Charset": "UTF-8",
        }
        body = urllib.parse.urlencode(params).encode()
        # SigV4 signing
        import datetime as _dt

        now = _dt.datetime.now(_dt.UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        service = "ses"
        credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
        payload_hash = hashlib.sha256(body).hexdigest()
        canonical_headers = f"host:{host}\nx-amz-date:{amz_date}\n"
        signed_headers = "host;x-amz-date"
        canonical_request = "\n".join(
            [
                "POST",
                "/",
                "",
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        def _sign(key, msg):
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        k_date = _sign(("AWS4" + secret_key).encode(), date_stamp)
        k_region = _sign(k_date, region)
        k_service = _sign(k_region, service)
        k_signing = _sign(k_service, "aws4_request")
        signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
        authorization = (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        req = urllib.request.Request(endpoint, data=body, method="POST")
        req.add_header("Host", host)
        req.add_header("X-Amz-Date", amz_date)
        req.add_header("Authorization", authorization)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            return DeliveryResult(sent=False, error=f"SES HTTP {exc.code}: {exc.read().decode()[:200]}")
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(sent=False, error=str(exc))
        # Parse message id
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

    def send(self, booking, *, subject, text_body, html_body):
        import json as _json
        import urllib.request

        token = getattr(settings, "POSTMARK_SERVER_TOKEN", "")
        if not token:
            return DeliveryResult(sent=False, error="Postmark server token is not configured")

        email = (booking.client.email or "").strip()
        if not email:
            return DeliveryResult(sent=False, error="no email address")

        payload = {
            "From": settings.DEFAULT_FROM_EMAIL,
            "To": email,
            "Subject": subject,
            "TextBody": text_body,
            "HtmlBody": html_body,
            "Attachments": [
                {
                    "Name": f"{booking.reference}.ics",
                    "Content": __import__("base64").b64encode(build_booking_ics(booking).encode()).decode(),
                    "ContentType": "text/calendar",
                }
            ],
        }
        req = urllib.request.Request(
            "https://api.postmarkapp.com/email",
            data=_json.dumps(payload).encode(),
            method="POST",
        )
        req.add_header("X-Postmark-Server-Token", token)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            return DeliveryResult(sent=False, error=f"Postmark HTTP {exc.code}: {exc.read().decode()[:200]}")
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(sent=False, error=str(exc))
        message_id = ""
        try:
            message_id = _json.loads(raw).get("MessageID", "")
        except Exception:  # noqa: BLE001
            pass
        return DeliveryResult(sent=True, provider_message_id=message_id)


def get_email_provider():
    name = getattr(settings, "EMAIL_PROVIDER", "smtp").lower().strip()
    if name == "ses":
        return SesProvider()
    if name == "postmark":
        return PostmarkProvider()
    return SmtpProvider()