"""Redact PII from old terminal phone calls.

Dry-run by default; pass --apply to write changes.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.agents.models import Conversation
from apps.voice.models import PhoneCall
from apps.voice.telephony import redact_pii

TERMINAL_STATUSES = [PhoneCall.Status.ENDED, PhoneCall.Status.FAILED, PhoneCall.Status.TRANSFERRED]
REDACTED = "[redacted]"


def _redacted_phone(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    return redact_pii(text)


class Command(BaseCommand):
    help = "Redact transcript/summary/metadata/recording URL/phone numbers from old terminal phone calls."

    def add_arguments(self, parser):
        default_days = int(getattr(settings, "VOICE_PHONE_RETENTION_DAYS", 30) or 30)
        parser.add_argument(
            "--days",
            type=int,
            default=default_days,
            help=(
                "Redact terminal calls older than this many days. "
                f"Defaults to VOICE_PHONE_RETENTION_DAYS ({default_days})."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply redactions. Without this flag the command is a read-only dry run.",
        )

    def handle(self, *args, **options):
        days = max(int(options["days"]), 0)
        apply = bool(options["apply"])
        cutoff = timezone.now() - timedelta(days=days)
        qs = PhoneCall.objects.filter(status__in=TERMINAL_STATUSES, created_at__lt=cutoff).order_by("created_at")

        candidates = list(qs)
        redacted_count = 0
        conversation_ids: set[int] = set()
        for call in candidates:
            changed = False
            updates: dict[str, object] = {}

            if call.transcript:
                updates["transcript"] = []
                changed = True
            if call.summary:
                updates["summary"] = REDACTED
                changed = True
            if call.metadata:
                updates["metadata"] = {"redacted": True, "redacted_at": timezone.now().isoformat()}
                changed = True
            if call.recording_url:
                updates["recording_url"] = ""
                changed = True

            masked_from = _redacted_phone(call.from_number)
            if masked_from != (call.from_number or ""):
                updates["from_number"] = masked_from
                changed = True
            masked_to = _redacted_phone(call.to_number)
            if masked_to != (call.to_number or ""):
                updates["to_number"] = masked_to
                changed = True

            if not changed:
                continue
            redacted_count += 1
            if call.conversation_id:
                conversation_ids.add(call.conversation_id)
            if apply:
                for field, value in updates.items():
                    setattr(call, field, value)
                call.save(update_fields=[*updates.keys(), "updated_at"])
                self.stdout.write(f"redacted {call.external_id}")
            else:
                self.stdout.write(f"would redact {call.external_id}")

        redacted_messages = 0
        redacted_conversations = 0
        if conversation_ids:
            conversations = Conversation.objects.filter(
                pk__in=conversation_ids, channel=Conversation.Channel.PHONE
            )
            for conversation in conversations:
                # Only redact if all linked calls are terminal and old enough.
                if conversation.phone_calls.exclude(
                    status__in=TERMINAL_STATUSES, created_at__lt=cutoff
                ).exists():
                    continue
                messages = list(conversation.messages.all())
                changed_messages = 0
                for message in messages:
                    msg_updates: dict[str, object] = {}
                    if message.content and message.content != REDACTED:
                        msg_updates["content"] = REDACTED
                    if message.tool_calls:
                        msg_updates["tool_calls"] = []
                    if message.tool_call_id:
                        msg_updates["tool_call_id"] = ""
                    if not msg_updates:
                        continue
                    changed_messages += 1
                    if apply:
                        for field, value in msg_updates.items():
                            setattr(message, field, value)
                        message.save(update_fields=[*msg_updates.keys()])
                if changed_messages:
                    redacted_messages += changed_messages
                    redacted_conversations += 1
                    if apply:
                        self.stdout.write(f"redacted conversation {conversation.pk} messages={changed_messages}")
                    else:
                        self.stdout.write(f"would redact conversation {conversation.pk} messages={changed_messages}")

        mode = "applied" if apply else "dry run"
        self.stdout.write(
            self.style.SUCCESS(
                f"Phone data pruning ({mode}): checked {len(candidates)}, redacted {redacted_count}, "
                f"conversations {redacted_conversations}, messages {redacted_messages}, cutoff {cutoff.date()}."
            )
        )
