"""Speech provider layer.

The alpha ships with the browser Web Speech API (zero infrastructure, works in
Chrome/Edge). The same interface supports server-side OpenAI transcription and
speech synthesis when an API key is configured.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx
from django.conf import settings

SPEECH_LANGUAGES: dict[str, dict[str, str]] = {
    "en": {"label": "English", "speech": "en-US"},
    "pl": {"label": "Polski", "speech": "pl-PL"},
}


class SpeechProviderError(Exception):
    pass


@dataclass(slots=True)
class SpeechProviderConfig:
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    timeout: float = 60.0


def _openai_config(default_model: str) -> SpeechProviderConfig:
    return SpeechProviderConfig(
        api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        model=os.getenv("OPENAI_STT_MODEL", default_model) if default_model == "whisper-1" else os.getenv(
            "OPENAI_TTS_MODEL", default_model
        ),
        timeout=float(os.getenv("AI_TIMEOUT_SECONDS", "60")),
    )


class BaseSTTProvider:
    name = "base"
    mode = "browser"

    def __init__(self, config: SpeechProviderConfig | None = None):
        self.config = config or SpeechProviderConfig()

    def client_config(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "mode": self.mode,
            "languages": SPEECH_LANGUAGES,
        }

    def transcribe(
        self,
        audio: bytes,
        *,
        language: str = "en",
        filename: str = "audio.webm",
        content_type: str = "audio/webm",
    ) -> str:
        raise SpeechProviderError("This speech provider runs in the browser.")


class BrowserSTTProvider(BaseSTTProvider):
    name = "browser"
    mode = "browser"


class OpenAISTTProvider(BaseSTTProvider):
    name = "openai"
    mode = "server"

    def __init__(self, config: SpeechProviderConfig | None = None):
        super().__init__(config or _openai_config("whisper-1"))
        if not self.config.api_key:
            raise SpeechProviderError("OPENAI_API_KEY is required for the OpenAI speech provider.")

    def client_config(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "mode": self.mode,
            "languages": SPEECH_LANGUAGES,
            "transcribe_url": "/api/v1/voice/transcribe/",
        }

    def transcribe(
        self,
        audio: bytes,
        *,
        language: str = "en",
        filename: str = "audio.webm",
        content_type: str = "audio/webm",
    ) -> str:
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        files = {"file": (filename, audio, content_type or "audio/webm")}
        data = {"model": self.config.model or "whisper-1", "language": language}
        try:
            with httpx.Client(timeout=self.config.timeout) as client:
                response = client.post(
                    f"{self.config.base_url}/audio/transcriptions",
                    headers=headers,
                    files=files,
                    data=data,
                )
        except httpx.RequestError as exc:
            raise SpeechProviderError("Could not reach the speech-to-text provider.") from exc
        if response.status_code >= 400:
            raise SpeechProviderError(f"Transcription failed ({response.status_code}).")
        try:
            return (response.json().get("text") or "").strip()
        except ValueError as exc:
            raise SpeechProviderError("The speech-to-text provider returned an invalid response.") from exc


class BaseTTSProvider:
    name = "base"
    mode = "browser"

    def __init__(self, config: SpeechProviderConfig | None = None):
        self.config = config or SpeechProviderConfig()

    def client_config(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "mode": self.mode,
            "languages": SPEECH_LANGUAGES,
        }

    def synthesize(self, text: str, *, language: str = "en", voice: str = "") -> tuple[bytes, str]:
        raise SpeechProviderError("This speech provider runs in the browser.")


class BrowserTTSProvider(BaseTTSProvider):
    name = "browser"
    mode = "browser"


class OpenAITTSProvider(BaseTTSProvider):
    name = "openai"
    mode = "server"

    def __init__(self, config: SpeechProviderConfig | None = None):
        super().__init__(config or _openai_config("tts-1"))
        if not self.config.api_key:
            raise SpeechProviderError("OPENAI_API_KEY is required for the OpenAI speech provider.")

    def client_config(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "mode": self.mode,
            "languages": SPEECH_LANGUAGES,
            "speak_url": "/api/v1/voice/speak/",
        }

    def synthesize(self, text: str, *, language: str = "en", voice: str = "") -> tuple[bytes, str]:
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        payload = {
            "model": self.config.model or "tts-1",
            "input": text,
            "voice": voice or "alloy",
            "response_format": "mp3",
        }
        try:
            with httpx.Client(timeout=self.config.timeout) as client:
                response = client.post(
                    f"{self.config.base_url}/audio/speech", headers=headers, json=payload
                )
        except httpx.RequestError as exc:
            raise SpeechProviderError("Could not reach the text-to-speech provider.") from exc
        if response.status_code >= 400:
            raise SpeechProviderError(f"Speech synthesis failed ({response.status_code}).")
        return response.content, "audio/mpeg"


def get_stt_provider(name: str) -> BaseSTTProvider:
    name = (name or "browser").lower()
    if name in {"browser", "web", "webspeech"}:
        return BrowserSTTProvider()
    if name == "openai":
        return OpenAISTTProvider()
    raise SpeechProviderError(f"Unknown speech-to-text provider {name!r}.")


def get_tts_provider(name: str) -> BaseTTSProvider:
    name = (name or "browser").lower()
    if name in {"browser", "web", "webspeech"}:
        return BrowserTTSProvider()
    if name == "openai":
        return OpenAITTSProvider()
    raise SpeechProviderError(f"Unknown text-to-speech provider {name!r}.")


def voice_client_config(settings_obj) -> dict[str, Any]:
    """Payload consumed by the browser call UI."""
    language = settings_obj.enabled_languages or ["en", "pl"]
    try:
        stt = get_stt_provider(settings_obj.voice_stt_provider)
    except SpeechProviderError:
        stt = BrowserSTTProvider()
    try:
        tts = get_tts_provider(settings_obj.voice_tts_provider)
    except SpeechProviderError:
        tts = BrowserTTSProvider()

    return {
        "enabled": settings_obj.is_enabled and settings_obj.voice_enabled,
        "languages": [
            {
                "code": code,
                "label": SPEECH_LANGUAGES.get(code, {}).get("label", code.upper()),
                "speech": SPEECH_LANGUAGES.get(code, {}).get("speech", "en-US"),
            }
            for code in language
            if code in SPEECH_LANGUAGES
        ],
        "stt": stt.client_config(),
        "tts": tts.client_config(),
        "voice_names": {"en": settings_obj.voice_name_en, "pl": settings_obj.voice_name_pl},
        "greeting": {
            code: settings_obj.greeting_for(code, settings.HOTEL["name"])
            for code in language
            if code in SPEECH_LANGUAGES
        },
    }
