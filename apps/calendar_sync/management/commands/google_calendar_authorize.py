"""Create an authorised-user token for Google Calendar (run outside Docker)."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Run the Google OAuth desktop flow and print an authorised-user token. "
        "Paste the JSON into the calendar connection's credentials field."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--client-secret",
            default=settings.GOOGLE_CALENDAR_CREDENTIALS_FILE,
            help="Path to the OAuth client secret JSON downloaded from Google Cloud.",
        )
        parser.add_argument("--output", help="Optional path to write the token JSON to.")

    def handle(self, *args, **options):
        client_secret = options["client_secret"]
        if not client_secret:
            raise CommandError(
                "Provide --client-secret /path/to/client_secret.json "
                "(or set GOOGLE_CALENDAR_CREDENTIALS_FILE)."
            )
        if not Path(client_secret).exists():
            raise CommandError(f"Client secret file not found: {client_secret}")

        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:  # pragma: no cover
            raise CommandError("google-auth-oauthlib is not installed.") from exc

        flow = InstalledAppFlow.from_client_secrets_file(client_secret, scopes=settings.GOOGLE_CALENDAR_SCOPES)
        credentials = flow.run_local_server(port=0)
        token_json = credentials.to_json()

        if options.get("output"):
            Path(options["output"]).write_text(token_json, encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Token written to {options['output']}"))
        else:
            self.stdout.write(token_json)
