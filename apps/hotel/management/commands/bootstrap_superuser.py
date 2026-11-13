"""Create the initial Django superuser from environment variables."""

from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create the initial superuser from DJANGO_SUPERUSER_* environment variables."

    def add_arguments(self, parser):
        parser.add_argument("--username", default=os.getenv("DJANGO_SUPERUSER_USERNAME", "admin"))
        parser.add_argument("--email", default=os.getenv("DJANGO_SUPERUSER_EMAIL", "admin@example.com"))
        parser.add_argument("--password", default=os.getenv("DJANGO_SUPERUSER_PASSWORD"))

    def handle(self, *args, **options):
        username = (options["username"] or "").strip()
        password = options["password"]
        if not username or not password:
            self.stdout.write("Superuser bootstrap skipped (no username/password configured).")
            return

        User = get_user_model()
        user = User.objects.filter(username=username).first()
        if user:
            if not user.is_superuser:
                user.is_superuser = True
                user.is_staff = True
                user.save(update_fields=["is_superuser", "is_staff"])
                self.stdout.write(f"Promoted existing user {username!r} to superuser.")
            else:
                self.stdout.write(f"Superuser {username!r} already exists.")
            return

        User.objects.create_superuser(username=username, email=options["email"], password=password)
        self.stdout.write(self.style.SUCCESS(f"Created superuser {username!r}."))
