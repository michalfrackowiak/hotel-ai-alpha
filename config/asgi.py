"""ASGI entrypoint with OpenLine phone voice websocket routing."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

django_asgi_app = get_asgi_application()


async def application(scope, receive, send):
    if scope.get("type") == "websocket":
        path = (scope.get("path") or "").rstrip("/")
        if path == "/ws/voice/openline":
            from apps.voice.bridge import openline_media_bridge

            await openline_media_bridge(scope, receive, send)
            return
    await django_asgi_app(scope, receive, send)
