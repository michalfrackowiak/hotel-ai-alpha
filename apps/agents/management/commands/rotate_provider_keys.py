"""Re-encrypt stored AI provider keys after an encryption-key rotation."""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.agents.crypto import rotate_provider_keys


class Command(BaseCommand):
    help = "Re-encrypt AIProvider.api_key from FIELD_ENCRYPTION_KEY_OLD to FIELD_ENCRYPTION_KEY."

    def add_arguments(self, parser):
        parser.add_argument(
            "--old-key",
            default="",
            help="Key that encrypted the current values. Defaults to FIELD_ENCRYPTION_KEY_OLD.",
        )
        parser.add_argument(
            "--new-key",
            default="",
            help="Replacement key. Defaults to FIELD_ENCRYPTION_KEY (or SECRET_KEY when unset).",
        )

    def handle(self, *args, **options):
        old_key = options["old_key"] or getattr(settings, "FIELD_ENCRYPTION_KEY_OLD", "")
        new_key = options["new_key"] or getattr(settings, "FIELD_ENCRYPTION_KEY", "") or settings.SECRET_KEY
        if not old_key:
            raise CommandError(
                "Provide --old-key or set FIELD_ENCRYPTION_KEY_OLD so existing keys can be decrypted."
            )
        count = rotate_provider_keys(old_key=old_key, new_key=new_key)
        self.stdout.write(self.style.SUCCESS(f"Rotated {count} provider key(s)."))
