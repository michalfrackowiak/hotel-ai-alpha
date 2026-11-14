"""Template context shared by every page."""

from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone, translation

from apps.hotel.guest_access import GUEST_SESSION_KEY, normalise_email

logger = logging.getLogger(__name__)


def site_context(request) -> dict:
    hotel = settings.HOTEL
    assistant = {
        "name": "Aurora",
        "chat_enabled": True,
        "voice_enabled": True,
    }
    try:
        from apps.agents.models import AgentSettings

        agent_settings = AgentSettings.load()
        assistant = {
            "name": agent_settings.name,
            "chat_enabled": agent_settings.is_enabled and agent_settings.chat_enabled,
            "voice_enabled": agent_settings.is_enabled and agent_settings.voice_enabled,
        }
    except Exception:  # noqa: BLE001 - the site must render before migrations/seed
        logger.debug("Agent settings unavailable in template context", exc_info=True)

    return {
        "hotel": hotel,
        "assistant": assistant,
        "guest_email": normalise_email(request.session.get(GUEST_SESSION_KEY)),
        "site_languages": [
            {"code": code, "label": label} for code, label in settings.LANGUAGES
        ],
        "current_language": translation.get_language(),
        "current_year": timezone.now().year,
    }
