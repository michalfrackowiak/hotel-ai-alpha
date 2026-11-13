"""Calendar provider adapters (Google Calendar + a fake provider for tests)."""

from __future__ import annotations

import json
import logging
import uuid
from abc import ABC, abstractmethod
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


class CalendarError(Exception):
    """A calendar operation failed but the booking itself is unaffected."""


class CalendarConfigError(CalendarError):
    """Credentials or calendar configuration are missing/invalid."""


class BaseCalendarProvider(ABC):
    def __init__(self, connection):
        self.connection = connection

    @abstractmethod
    def create_event(self, calendar_id: str, event: dict[str, Any]) -> str: ...

    @abstractmethod
    def update_event(self, calendar_id: str, event_id: str, event: dict[str, Any]) -> str: ...

    @abstractmethod
    def delete_event(self, calendar_id: str, event_id: str) -> None: ...

    def health_check(self) -> tuple[bool, str]:
        try:
            self.create_event(self.connection.calendar_id or "primary", {"summary": "Hotel AI health check"})
        except CalendarError as exc:
            return False, str(exc)
        return True, "Calendar reachable"


class FakeCalendarProvider(BaseCalendarProvider):
    """In-memory provider used by the demo and the test suite."""

    def create_event(self, calendar_id: str, event: dict[str, Any]) -> str:
        return f"fake-{uuid.uuid4().hex[:12]}"

    def update_event(self, calendar_id: str, event_id: str, event: dict[str, Any]) -> str:
        return event_id

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        return None

    def health_check(self) -> tuple[bool, str]:
        return True, "Fake calendar ready"


class GoogleCalendarProvider(BaseCalendarProvider):
    """Google Calendar adapter.

    Accepts a service-account key or an authorised-user token in
    ``CalendarConnection.credentials_json``. The legacy env var
    ``GOOGLE_CALENDAR_CREDENTIALS_FILE`` is used as a fallback path.
    """

    def _credentials(self):
        scopes = settings.GOOGLE_CALENDAR_SCOPES
        raw = (self.connection.credentials_json or "").strip()
        source = "database"
        if raw:
            try:
                info = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise CalendarConfigError("The credentials JSON is not valid JSON.") from exc
        else:
            path = settings.GOOGLE_CALENDAR_CREDENTIALS_FILE
            if not path:
                raise CalendarConfigError(
                    "No Google credentials configured. Add a service account key in the calendar "
                    "connection or set GOOGLE_CALENDAR_CREDENTIALS_FILE."
                )
            try:
                with open(path, encoding="utf-8") as handle:
                    info = json.load(handle)
            except OSError as exc:
                raise CalendarConfigError(f"Could not read the credentials file: {exc}") from exc
            source = path

        try:
            from google.oauth2 import service_account
            from google.oauth2.credentials import Credentials

            if info.get("type") == "service_account" or "private_key" in info:
                return service_account.Credentials.from_service_account_info(info, scopes=scopes)
            if info.get("type") == "authorized_user" or "refresh_token" in info:
                return Credentials.from_authorized_user_info(info, scopes=scopes)
        except ImportError as exc:
            raise CalendarConfigError(
                "Google client libraries are not installed. Rebuild the image from requirements.txt."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise CalendarConfigError(f"Invalid Google credentials ({source}): {exc}") from exc

        raise CalendarConfigError(
            "Credentials must be a service account key or an authorised-user token. "
            "Run `python manage.py google_calendar_authorize` to create a token."
        )

    def _service(self):
        try:
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise CalendarConfigError(
                "google-api-python-client is not installed. Rebuild the image from requirements.txt."
            ) from exc
        try:
            return build("calendar", "v3", credentials=self._credentials(), cache_discovery=False)
        except CalendarError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CalendarConfigError(f"Could not initialise the Google Calendar client: {exc}") from exc

    def create_event(self, calendar_id: str, event: dict[str, Any]) -> str:
        return self._insert(calendar_id, event)

    def update_event(self, calendar_id: str, event_id: str, event: dict[str, Any]) -> str:
        return self._update(calendar_id, event_id, event)

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        self._delete(calendar_id, event_id)

    def health_check(self) -> tuple[bool, str]:
        try:
            self._service().calendars().get(calendarId=self.connection.calendar_id or "primary").execute()
        except CalendarError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"Google Calendar error: {exc}"
        return True, "Google Calendar reachable"

    # ---------------------------------------------------------------- helpers
    def _insert(self, calendar_id: str, event: dict[str, Any]) -> str:
        try:
            created = (
                self._service().events().insert(calendarId=calendar_id, body=event).execute()
            )
        except CalendarError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CalendarError(f"Google Calendar insert failed: {_http_message(exc)}") from exc
        event_id = created.get("id")
        if not event_id:
            raise CalendarError("Google Calendar did not return an event id.")
        return event_id

    def _update(self, calendar_id: str, event_id: str, event: dict[str, Any]) -> str:
        try:
            updated = (
                self._service()
                .events()
                .update(calendarId=calendar_id, eventId=event_id, body=event)
                .execute()
            )
        except CalendarError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CalendarError(f"Google Calendar update failed: {_http_message(exc)}") from exc
        return updated.get("id", event_id)

    def _delete(self, calendar_id: str, event_id: str) -> None:
        try:
            self._service().events().delete(calendarId=calendar_id, eventId=event_id).execute()
        except CalendarError:
            raise
        except Exception as exc:  # noqa: BLE001
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status in (404, 410):
                return
            raise CalendarError(f"Google Calendar delete failed: {_http_message(exc)}") from exc


def _http_message(exc: Exception) -> str:
    content = getattr(exc, "content", b"")
    if isinstance(content, bytes) and content:
        try:
            data = json.loads(content)
            return str(data.get("error", {}).get("message") or data)[:300]
        except (ValueError, AttributeError):
            return content.decode("utf-8", "replace")[:300]
    return str(exc)[:300]


def get_calendar_provider(connection) -> BaseCalendarProvider:
    from .models import CalendarConnection

    if connection.provider == CalendarConnection.Provider.FAKE:
        return FakeCalendarProvider(connection)
    if connection.provider == CalendarConnection.Provider.GOOGLE:
        return GoogleCalendarProvider(connection)
    raise CalendarConfigError(f"Unsupported calendar provider {connection.provider!r}.")
