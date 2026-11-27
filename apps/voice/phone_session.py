"""Phone call voice session: STT -> AgentEngine -> TTS."""

from __future__ import annotations

import io
import logging
import re
import wave
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from apps.agents.engine import AgentEngine
from apps.agents.models import AgentSettings, Conversation

from .audio import SAMPLE_RATE_16K, resample_pcm16
from .models import PhoneCall

logger = logging.getLogger(__name__)


@dataclass
class PhoneVoiceSession:
    phone_call: PhoneCall
    conversation: Conversation | None
    settings_obj: AgentSettings = field(default_factory=AgentSettings.load)
    silence_prompts: int = 0
    max_silence_prompts: int = 2

    def initial_greeting(self) -> str:
        if self.conversation is None:
            return ""
        # Conversation already has a greeting message from start_conversation; speak it.
        last = self.conversation.messages.filter(role="assistant").order_by("-created_at", "-pk").first()
        return (last.content if last else "").strip()

    def transcribe_pcm16(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE_16K) -> str:
        if not pcm16:
            return ""
        if sample_rate != SAMPLE_RATE_16K:
            pcm16, _ = resample_pcm16(pcm16, sample_rate, SAMPLE_RATE_16K)
        wav_bytes = _pcm16_to_wav(pcm16, SAMPLE_RATE_16K)
        provider = (getattr(settings, "VOICE_STT_PROVIDER", "") or "openai").strip().lower()
        if provider in {"openai", ""}:
            return self._transcribe_openai(wav_bytes)
        # Fallback to OpenAI for phone calls; browser STT is not usable.
        if provider in {"browser", "webspeech", "web"}:
            return self._transcribe_openai(wav_bytes)
        raise RuntimeError(f"Unsupported phone STT provider: {provider}")

    def _transcribe_openai(self, wav_bytes: bytes) -> str:
        from apps.voice.providers import OpenAISTTProvider, SpeechProviderConfig

        api_key = (getattr(settings, "OPENAI_API_KEY", "") or "").strip()
        if not api_key:
            # Try env via provider default; SpeechProviderConfig may read env itself.
            pass
        model = (getattr(settings, "VOICE_STT_MODEL", "") or getattr(settings, "OPENAI_STT_MODEL", "") or "whisper-1").strip()
        config = SpeechProviderConfig(
            api_key=api_key or __import__("os").getenv("OPENAI_API_KEY", "").strip(),
            base_url=(__import__("os").getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")),
            model=model,
            timeout=float(getattr(settings, "AI_TIMEOUT_SECONDS", 60)),
        )
        provider = OpenAISTTProvider(config=config)
        language = (self.conversation.language if self.conversation else "pl") or "pl"
        return provider.transcribe(wav_bytes, language=language, filename="utterance.wav", content_type="audio/wav")

    _OPTOUT_RE = re.compile(
        r"\b(do not record|don't record|no recording|opt out|without recording|"
        r"nie nagrywaj|bez nagrywania|nie chc[eę] nagrania)\b",
        re.IGNORECASE,
    )

    def handle_text(self, text: str) -> str:
        if self.conversation is None:
            return self.fallback_reply()
        self.silence_prompts = 0
        if self._OPTOUT_RE.search(text or ""):
            metadata = dict(self.phone_call.metadata or {})
            metadata["recording_opt_out"] = True
            metadata["recording_opt_out_at"] = timezone.now().isoformat()
            self.phone_call.metadata = metadata
            self.phone_call.save(update_fields=["metadata", "updated_at"])
        engine = AgentEngine(self.conversation, settings_obj=self.settings_obj)
        turn = engine.handle_user_message(text)
        opt_out = bool((self.phone_call.metadata or {}).get("recording_opt_out"))
        self._append_transcript("user", "[opt-out]" if opt_out else text)
        self._append_transcript("assistant", "[opt-out]" if opt_out else turn.content, tool_calls=None if opt_out else turn.tool_calls)
        self._persist_phone_state(turn)
        return (turn.content or "").strip()

    def synthesize_pcm16(self, text: str) -> bytes:
        if not text.strip():
            return b""
        provider = (getattr(settings, "VOICE_TTS_PROVIDER", "") or "openai").strip().lower()
        if provider in {"openai", "browser", "webspeech", "web", ""}:
            return self._synthesize_openai(text)
        raise RuntimeError(f"Unsupported phone TTS provider: {provider}")

    def _synthesize_openai(self, text: str) -> bytes:
        from apps.voice.providers import SpeechProviderConfig

        api_key = (getattr(settings, "OPENAI_API_KEY", "") or "").strip() or __import__("os").getenv("OPENAI_API_KEY", "").strip()
        base_url = __import__("os").getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        model = (getattr(settings, "VOICE_TTS_MODEL", "") or __import__("os").getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")).strip() or "gpt-4o-mini-tts"
        config = SpeechProviderConfig(api_key=api_key, base_url=base_url, model=model, timeout=float(getattr(settings, "AI_TIMEOUT_SECONDS", 60)))
        language = (self.conversation.language if self.conversation else "pl") or "pl"
        voice = (getattr(settings, "VOICE_TTS_VOICE", "") or "").strip()
        if not voice:
            voice = self.settings_obj.voice_name_pl if language == "pl" else self.settings_obj.voice_name_en
        if not voice:
            voice = "alloy"

        # Use OpenAI speech endpoint directly for PCM format when possible.
        import httpx

        headers = {"Authorization": f"Bearer {config.api_key}"}
        payload = {
            "model": config.model,
            "input": text,
            "voice": voice,
            "response_format": "pcm",
        }
        try:
            with httpx.Client(timeout=config.timeout) as client:
                response = client.post(f"{config.base_url}/audio/speech", headers=headers, json=payload)
        except httpx.RequestError as exc:
            raise RuntimeError(f"TTS request failed: {exc}") from exc
        if response.status_code >= 400:
            # Fallback to mp3 via existing provider, then decode? We cannot decode mp3 without deps.
            # Try wav format as another fallback.
            payload["response_format"] = "wav"
            with httpx.Client(timeout=config.timeout) as client:
                response = client.post(f"{config.base_url}/audio/speech", headers=headers, json=payload)
            if response.status_code >= 400:
                raise RuntimeError(f"TTS failed ({response.status_code})")
            return _wav_to_pcm16(response.content)
        # OpenAI pcm is 24kHz 16-bit mono LE.
        pcm24 = response.content
        pcm16, _ = resample_pcm16(pcm24, 24000, SAMPLE_RATE_16K)
        return pcm16

    def fallback_reply(self) -> str:
        lang = (self.conversation.language if self.conversation else "pl") or "pl"
        return self.settings_obj.fallback_for(lang)

    def silence_prompt(self) -> str:
        self.silence_prompts += 1
        lang = (self.conversation.language if self.conversation else "pl") or "pl"
        if lang == "pl":
            return "Czy nadal jesteś na linii? W czym mogę pomóc?"
        return "Are you still there? How can I help you?"

    def silence_exceeded(self) -> bool:
        return self.silence_prompts > self.max_silence_prompts

    def should_transfer(self) -> bool:
        if self.conversation is None:
            return False
        state = self.conversation.state or {}
        handover = state.get("handover_requested") or {}
        return bool(handover) and handover.get("channel") == Conversation.Channel.PHONE

    def transfer_number(self) -> str:
        if self.conversation is None:
            return ""
        handover = (self.conversation.state or {}).get("handover_requested") or {}
        return (handover.get("transfer_number") or "").strip()

    def mark_transferred(self, reason: str = "") -> None:
        self.phone_call.status = PhoneCall.Status.TRANSFERRED
        self.phone_call.ended_at = self.phone_call.ended_at or timezone.now()
        if self.phone_call.answered_at and self.phone_call.duration_seconds is None:
            self.phone_call.duration_seconds = max(
                int((self.phone_call.ended_at - self.phone_call.answered_at).total_seconds()), 0
            )
        metadata = dict(self.phone_call.metadata or {})
        metadata["transfer_requested_at"] = timezone.now().isoformat()
        if reason:
            metadata["transfer_result"] = reason
        if self.conversation:
            handover = (self.conversation.state or {}).get("handover_requested") or {}
            metadata["transfer_reason"] = handover.get("reason", "")
            metadata["transfer_number"] = handover.get("transfer_number", "")
        self.phone_call.metadata = metadata
        self.phone_call.save(
            update_fields=["status", "ended_at", "duration_seconds", "metadata", "updated_at"]
        )

    def _append_transcript(self, role: str, text: str, tool_calls=None) -> None:
        transcript = list(self.phone_call.transcript or [])
        entry = {
            "role": role,
            "text": text,
            "at": timezone.now().isoformat(),
        }
        if tool_calls:
            entry["tool_calls"] = tool_calls
        transcript.append(entry)
        self.phone_call.transcript = transcript
        update_fields = ["transcript", "updated_at"]
        if self.conversation and self.phone_call.conversation_id != self.conversation.pk:
            self.phone_call.conversation = self.conversation
            update_fields.append("conversation")
        self.phone_call.save(update_fields=update_fields)

    def _persist_phone_state(self, turn) -> None:
        metadata = dict(self.phone_call.metadata or {})
        metadata["last_turn_latency_ms"] = turn.latency_ms
        latencies = list(metadata.get("turn_latencies_ms") or [])
        latencies.append(int(turn.latency_ms))
        metadata["turn_latencies_ms"] = latencies[-200:]
        metadata["last_provider"] = turn.provider
        metadata["last_model"] = turn.model
        if turn.booking_reference:
            metadata["last_booking_reference"] = turn.booking_reference
        self.phone_call.metadata = metadata
        self.phone_call.save(update_fields=["metadata", "updated_at"])


def _pcm16_to_wav(pcm16: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16)
    return buf.getvalue()


def _wav_to_pcm16(wav_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        pcm = wf.readframes(wf.getnframes())
        width = wf.getsampwidth()
        channels = wf.getnchannels()
        rate = wf.getframerate()
    if channels > 1:
        import audioop

        pcm = audioop.tomono(pcm, width, 0.5, 0.5)
    if width != 2:
        import audioop

        pcm = audioop.lin2lin(pcm, width, 2)
    if rate != SAMPLE_RATE_16K:
        pcm, _ = resample_pcm16(pcm, rate, SAMPLE_RATE_16K)
    return pcm
