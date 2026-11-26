"""ASGI WebSocket bridge for OpenLine Twilio-format media streams."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import parse_qs

from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils import timezone

from apps.agents.models import AgentSettings, Conversation

from .audio import (
    BYTES_PER_8K_FRAME,
    SAMPLE_RATE_16K,
    chunk_pcm16,
    decode_twilio_media_payload,
    encode_twilio_media_payload,
    pcm16_8k_to_16k,
    pcm16_16k_to_8k,
    rms_pcm16,
)
from .models import PhoneCall
from .phone_session import PhoneVoiceSession
from .telephony import redact_pii, verify_stream_token

logger = logging.getLogger(__name__)


@dataclass
class BridgeState:
    call_id: str = ""
    stream_sid: str = ""
    phone_call: PhoneCall | None = None
    conversation: Conversation | None = None
    session: PhoneVoiceSession | None = None
    started: bool = False
    closed: bool = False
    caller_speaking: bool = False
    bot_speaking: bool = False
    speech_buffer: bytearray = field(default_factory=bytearray)
    speech_started_at: float | None = None
    last_voice_at: float = 0.0
    last_silence_at: float = 0.0
    playback_task: asyncio.Task | None = None
    playback_queue: asyncio.Queue[bytes | None] = field(default_factory=asyncio.Queue)
    marks: dict[str, float] = field(default_factory=dict)
    pending_marks: list[str] = field(default_factory=list)
    mark_sent_at: float = 0.0
    last_activity_at: float = field(default_factory=time.monotonic)
    call_started_at: float = field(default_factory=time.monotonic)
    turn_count: int = 0
    silence_prompted: bool = False
    dtmf_buffer: str = ""
    dtmf_last_at: float = 0.0
    stt_failures: int = 0
    llm_failures: int = 0
    tts_failures: int = 0
    utterance_task: asyncio.Task | None = None


class OpenLineMediaBridge:
    """Minimal Twilio Media Streams server for OpenLine."""

    path = "/ws/voice/openline/"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "websocket":
            return
        await send({"type": "websocket.accept"})
        state = BridgeState()
        playback_sender = asyncio.create_task(self._playback_loop(send, state))
        try:
            await self._run(scope, receive, send, state)
        finally:
            if state.started and state.phone_call and state.phone_call.status not in {
                PhoneCall.Status.ENDED, PhoneCall.Status.FAILED, PhoneCall.Status.TRANSFERRED
            }:
                try:
                    await self._finalize_call(state, status=PhoneCall.Status.ENDED, reason="ws_disconnect")
                except Exception:
                    logger.exception("Failed to finalize phone call %s", state.call_id)
            state.closed = True
            await self._cancel_playback(state)
            if state.utterance_task and not state.utterance_task.done():
                state.utterance_task.cancel()
                try:
                    await state.utterance_task
                except (Exception, asyncio.CancelledError):
                    pass
            playback_sender.cancel()
            try:
                await playback_sender
            except (Exception, asyncio.CancelledError):
                pass
            try:
                await send({"type": "websocket.close", "code": 1000})
            except Exception:
                pass

    async def _run(self, scope, receive, send, state: BridgeState) -> None:
        while not state.closed:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                return
            if message["type"] != "websocket.receive":
                continue
            if "text" in message and message["text"]:
                await self._handle_text(message["text"], scope, send, state)
            elif "bytes" in message and message["bytes"]:
                # Raw format not used by default OpenLine integration; ignore.
                logger.debug("Ignoring binary websocket frame on OpenLine bridge")

    async def _handle_text(self, text: str, scope, send, state: BridgeState) -> None:
        try:
            payload = json.loads(text)
        except Exception:
            logger.warning("Invalid JSON on OpenLine media stream")
            return
        event = (payload.get("event") or payload.get("type") or "").lower()
        if event == "connected":
            return
        if event == "start":
            await self._handle_start(payload, scope, send, state)
            return
        if event == "media":
            await self._handle_media(payload, send, state)
            return
        if event == "mark":
            mark_name = ((payload.get("mark") or {}).get("name") or "").strip()
            if mark_name and mark_name in state.pending_marks:
                state.pending_marks = [m for m in state.pending_marks if m != mark_name]
                if not state.pending_marks:
                    state.bot_speaking = False
                    state.mark_sent_at = 0.0
                    state.last_activity_at = time.monotonic()
            return
        if event == "dtmf":
            await self._handle_dtmf(payload, send, state)
            return
        if event == "stop":
            state.closed = True
            return

    async def _handle_start(self, payload: dict, scope, send, state: BridgeState) -> None:
        start = payload.get("start") or {}
        state.stream_sid = (start.get("streamSid") or payload.get("streamSid") or "").strip()
        state.call_id = (start.get("callSid") or start.get("callId") or "").strip()
        custom = start.get("customParameters") or {}
        if not state.call_id:
            state.call_id = str(custom.get("callId") or custom.get("call_id") or "").strip()

        provided_token = str(
            custom.get("streamToken")
            or custom.get("stream_token")
            or custom.get("token")
            or ""
        ).strip()
        if not provided_token:
            query = parse_qs((scope.get("query_string") or b"").decode("utf-8", errors="ignore"))
            provided_token = str((query.get("token") or query.get("streamToken") or [""])[0]).strip()

        phone_call = await self._resolve_phone_call(state.call_id, custom)
        if phone_call is None or not await self._call_allowed(phone_call):
            logger.warning("Rejecting OpenLine stream for unknown/disabled call %s", state.call_id)
            state.closed = True
            return
        if not await self._stream_token_valid(phone_call, provided_token):
            logger.warning("Rejecting OpenLine stream for invalid token call=%s", state.call_id)
            state.closed = True
            return

        state.phone_call = phone_call
        state.conversation = await self._ensure_conversation(phone_call)
        settings_obj = await sync_to_async(AgentSettings.load)()
        state.session = PhoneVoiceSession(
            phone_call=phone_call, conversation=state.conversation, settings_obj=settings_obj
        )
        state.started = True
        state.call_started_at = time.monotonic()
        state.last_activity_at = time.monotonic()
        await self._mark_call_active(state)
        logger.info("OpenLine media stream started call=%s stream=%s", state.call_id, state.stream_sid)

        greeting = await sync_to_async(state.session.initial_greeting)()
        if greeting:
            await self._speak(send, state, greeting)

    async def _handle_media(self, payload: dict, send, state: BridgeState) -> None:
        if not state.started or state.session is None or state.phone_call is None:
            return
        self._expire_stale_marks(state)
        if await self._max_duration_exceeded(state):
            await self._end_call(send, state, reason="max_duration")
            return
        if state.dtmf_buffer and state.dtmf_last_at and (time.monotonic() - state.dtmf_last_at) > 3.0:
            await self._flush_dtmf(send, state)
        media = payload.get("media") or {}
        raw_payload = media.get("payload") or ""
        if not raw_payload:
            return
        try:
            pcm8k = decode_twilio_media_payload(raw_payload)
        except Exception:
            return
        pcm16k, _ = pcm16_8k_to_16k(pcm8k)
        await self._process_audio(pcm16k, send, state)

    async def _process_audio(self, pcm16k: bytes, send, state: BridgeState) -> None:
        if not pcm16k:
            return
        rms = rms_pcm16(pcm16k)
        threshold = 350
        silence_ms = int(getattr(settings, "VOICE_PHONE_SILENCE_TIMEOUT_SECONDS", 20) * 1000)
        utterance_silence_ms = 900
        now = time.monotonic()

        if rms >= threshold:
            if state.bot_speaking:
                await self._barge_in(send, state)
            state.caller_speaking = True
            state.speech_started_at = state.speech_started_at or now
            state.last_voice_at = now
            state.last_activity_at = now
            state.speech_buffer.extend(pcm16k)
            return

        if not state.caller_speaking:
            # Idle silence timeout while waiting for the caller.
            if (
                state.session
                and not state.bot_speaking
                and (now - state.last_activity_at) * 1000 > silence_ms
            ):
                await self._handle_silence_timeout(send, state)
            return

        # Caller was speaking; accumulate a bit of trailing silence.
        state.speech_buffer.extend(pcm16k)
        if state.last_voice_at and (now - state.last_voice_at) * 1000 < utterance_silence_ms:
            return

        utterance = bytes(state.speech_buffer)
        state.speech_buffer.clear()
        state.caller_speaking = False
        state.speech_started_at = None
        if len(utterance) < SAMPLE_RATE_16K // 2:  # < 0.5s
            return
        self._schedule_utterance(state, self._handle_utterance(utterance, send, state))

    def _schedule_utterance(self, state: BridgeState, coro) -> None:
        if state.utterance_task and not state.utterance_task.done():
            logger.debug("Dropping overlapping utterance for %s", state.call_id)
            try:
                coro.close()
            except Exception:
                pass
            return

        async def _runner():
            try:
                await coro
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Unhandled utterance task error for %s", state.call_id)

        state.utterance_task = asyncio.create_task(_runner())

    async def _handle_utterance(self, pcm16k: bytes, send, state: BridgeState) -> None:
        if state.session is None:
            return
        state.turn_count += 1
        turn_started = time.perf_counter()
        done = asyncio.Event()
        filler_task = asyncio.create_task(self._delayed_filler(send, state, done))
        try:
            text = await sync_to_async(state.session.transcribe_pcm16)(pcm16k, SAMPLE_RATE_16K)
            state.stt_failures = 0
        except Exception:
            state.stt_failures += 1
            logger.exception("Phone STT failed for %s", state.call_id)
            done.set()
            filler_task.cancel()
            if state.stt_failures >= 2:
                await self._speak_safe(send, state, self._failure_message(state, "stt"))
                await self._perform_transfer(send, state)
            return
        if not text.strip():
            done.set()
            filler_task.cancel()
            return
        logger.info("Phone call %s user: %s", state.call_id, redact_pii(text[:200]))
        try:
            reply = await sync_to_async(state.session.handle_text)(text)
            state.llm_failures = 0
        except Exception:
            state.llm_failures += 1
            logger.exception("Phone agent turn failed for %s", state.call_id)
            reply = await sync_to_async(state.session.fallback_reply)()
            if state.llm_failures >= 2:
                done.set()
                filler_task.cancel()
                await self._speak_safe(send, state, self._failure_message(state, "llm"))
                await self._perform_transfer(send, state)
                return
        finally:
            done.set()
            filler_task.cancel()
        latency_ms = int((time.perf_counter() - turn_started) * 1000)
        logger.info("Phone call %s turn_latency_ms=%s", state.call_id, latency_ms)
        if reply:
            await self._speak(send, state, reply)
        if await sync_to_async(state.session.should_transfer)():
            await self._perform_transfer(send, state)

    async def _delayed_filler(self, send, state: BridgeState, done: asyncio.Event) -> None:
        try:
            await asyncio.sleep(1.5)
            if done.is_set() or state.closed or state.bot_speaking:
                return
            lang = (state.conversation.language if state.conversation else "pl") or "pl"
            filler = "Chwileczkę, już sprawdzam." if lang == "pl" else "One moment while I check that."
            await self._speak(send, state, filler)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.debug("Filler playback skipped for %s", state.call_id)

    async def _handle_dtmf(self, payload: dict, send, state: BridgeState) -> None:
        if not state.started or state.session is None:
            return
        dtmf = payload.get("dtmf") or {}
        digit = str(dtmf.get("digit") or payload.get("digit") or "").strip()
        if not digit:
            return
        state.dtmf_last_at = time.monotonic()
        state.last_activity_at = time.monotonic()
        if digit in {"#", "*"}:
            await self._flush_dtmf(send, state)
            return
        if digit.isdigit() or digit in {"A", "B", "C", "D"}:
            state.dtmf_buffer += digit
            # Flush if it looks like a complete reference/phone-ish length or after pause.
            if len(state.dtmf_buffer) >= 16:
                await self._flush_dtmf(send, state)

    async def _flush_dtmf(self, send, state: BridgeState) -> None:
        text = state.dtmf_buffer.strip()
        state.dtmf_buffer = ""
        if not text:
            return
        # Treat as user input; include a hint for the agent.
        logger.info("Phone call %s DTMF: %s", state.call_id, redact_pii(text))
        self._schedule_utterance(state, self._handle_utterance_text(text, send, state, source="dtmf"))

    async def _handle_utterance_text(self, text: str, send, state: BridgeState, *, source: str = "text") -> None:
        if state.session is None or not text.strip():
            return
        state.turn_count += 1
        turn_started = time.perf_counter()
        done = asyncio.Event()
        filler_task = asyncio.create_task(self._delayed_filler(send, state, done))
        try:
            reply = await sync_to_async(state.session.handle_text)(text)
            state.llm_failures = 0
        except Exception:
            state.llm_failures += 1
            logger.exception("Phone agent turn failed for %s (%s)", state.call_id, source)
            reply = await sync_to_async(state.session.fallback_reply)()
            if state.llm_failures >= 2:
                done.set()
                filler_task.cancel()
                await self._speak_safe(send, state, self._failure_message(state, "llm"))
                await self._perform_transfer(send, state)
                return
        finally:
            done.set()
            filler_task.cancel()
        latency_ms = int((time.perf_counter() - turn_started) * 1000)
        logger.info("Phone call %s turn_latency_ms=%s source=%s", state.call_id, latency_ms, source)
        if reply:
            await self._speak(send, state, reply)
        if await sync_to_async(state.session.should_transfer)():
            await self._perform_transfer(send, state)

    async def _handle_silence_timeout(self, send, state: BridgeState) -> None:
        if state.session is None:
            return
        state.last_activity_at = time.monotonic()
        prompt = await sync_to_async(state.session.silence_prompt)()
        if prompt:
            await self._speak(send, state, prompt)
        if await sync_to_async(state.session.silence_exceeded)():
            await self._perform_transfer(send, state)

    async def _speak(self, send, state: BridgeState, text: str) -> None:
        if state.session is None or not text.strip():
            return
        chunks = self._split_tts_chunks(text)
        if not chunks:
            return
        state.bot_speaking = True
        for chunk in chunks:
            if state.closed or not state.bot_speaking:
                break
            mark_name = f"mark-{uuid.uuid4().hex[:12]}"
            state.pending_marks.append(mark_name)
            try:
                pcm16 = await sync_to_async(state.session.synthesize_pcm16)(chunk)
                state.tts_failures = 0
            except Exception:
                state.tts_failures += 1
                logger.exception("Phone TTS failed for %s", state.call_id)
                if mark_name in state.pending_marks:
                    state.pending_marks.remove(mark_name)
                if state.tts_failures >= 3:
                    await self._finalize_call(state, status=PhoneCall.Status.FAILED, reason="tts_failed")
                    await self._hangup_provider_call(state)
                    state.closed = True
                    return
                continue
            if state.closed or not state.bot_speaking or mark_name not in state.pending_marks:
                if mark_name in state.pending_marks:
                    state.pending_marks.remove(mark_name)
                break
            pcm8k, _ = pcm16_16k_to_8k(pcm16)
            frames = chunk_pcm16(pcm8k, BYTES_PER_8K_FRAME)
            if not frames:
                if mark_name in state.pending_marks:
                    state.pending_marks.remove(mark_name)
                continue
            for frame in frames:
                if state.closed or not state.bot_speaking:
                    break
                await state.playback_queue.put(frame)
            if mark_name in state.pending_marks and state.bot_speaking and not state.closed:
                await state.playback_queue.put(("mark", mark_name))
            elif mark_name in state.pending_marks:
                state.pending_marks.remove(mark_name)
        if not state.pending_marks and state.playback_queue.empty():
            state.bot_speaking = False

    @staticmethod
    def _split_tts_chunks(text: str, max_chars: int = 220) -> list[str]:
        cleaned = " ".join((text or "").split())
        if not cleaned:
            return []
        # Split on sentence boundaries first.
        import re

        parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", cleaned) if p.strip()]
        if not parts:
            parts = [cleaned]
        chunks: list[str] = []
        current = ""
        for part in parts:
            if len(part) > max_chars:
                if current:
                    chunks.append(current)
                    current = ""
                for i in range(0, len(part), max_chars):
                    piece = part[i : i + max_chars].strip()
                    if piece:
                        chunks.append(piece)
                continue
            candidate = f"{current} {part}".strip() if current else part
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = part
        if current:
            chunks.append(current)
        return chunks

    async def _playback_loop(self, send, state: BridgeState) -> None:
        try:
            while not state.closed:
                try:
                    item = await asyncio.wait_for(state.playback_queue.get(), timeout=1.0)
                except TimeoutError:
                    self._expire_stale_marks(state)
                    continue
                if item is None:
                    return
                if isinstance(item, tuple) and item[0] == "mark":
                    _, mark_name = item
                    if mark_name not in state.pending_marks:
                        if not state.pending_marks and state.playback_queue.empty():
                            state.bot_speaking = False
                        continue
                    if not state.bot_speaking:
                        continue
                    if state.stream_sid:
                        msg = {
                            "event": "mark",
                            "streamSid": state.stream_sid,
                            "mark": {"name": mark_name},
                        }
                        await send({"type": "websocket.send", "text": json.dumps(msg)})
                    state.mark_sent_at = time.monotonic()
                    # Do not clear bot_speaking on send; wait for client mark echo.
                    continue
                if not state.bot_speaking:
                    continue
                payload = encode_twilio_media_payload(item)
                message = {
                    "event": "media",
                    "streamSid": state.stream_sid,
                    "media": {"payload": payload},
                }
                await send({"type": "websocket.send", "text": json.dumps(message)})
                if not state.pending_marks and state.playback_queue.empty():
                    state.bot_speaking = False
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Playback loop failed for %s", state.call_id)

    async def _barge_in(self, send, state: BridgeState) -> None:
        state.bot_speaking = False
        state.pending_marks.clear()
        state.mark_sent_at = 0.0
        while not state.playback_queue.empty():
            try:
                state.playback_queue.get_nowait()
            except Exception:
                break
        if state.utterance_task and not state.utterance_task.done():
            state.utterance_task.cancel()
        if state.stream_sid:
            await send(
                {
                    "type": "websocket.send",
                    "text": json.dumps({"event": "clear", "streamSid": state.stream_sid}),
                }
            )

    async def _cancel_playback(self, state: BridgeState) -> None:
        state.bot_speaking = False
        if state.playback_task and not state.playback_task.done():
            state.playback_task.cancel()

    async def _perform_transfer(self, send, state: BridgeState) -> None:
        if state.session is None or state.phone_call is None or state.closed:
            return
        logger.info("Phone transfer requested for %s", state.call_id)
        transfer_number = await sync_to_async(state.session.transfer_number)()
        # Give the agent's spoken reply time to play out.
        await self._drain_playback(state, timeout=8.0)
        transferred = False
        call_id = state.call_id or state.phone_call.external_id
        if transfer_number:
            transferred = await self._try_provider_transfer(call_id, transfer_number)
        reason = "provider_transfer" if transferred else "transfer_fallback_hangup"
        await sync_to_async(state.session.mark_transferred)(reason=reason)
        if not transferred:
            await self._notify_staff_transfer_fallback(state)
            await self._hangup_provider_call(state)
        state.closed = True

    @sync_to_async
    def _resolve_phone_call(self, call_id: str, custom: dict) -> PhoneCall | None:
        if call_id:
            call = PhoneCall.objects.filter(external_id=call_id).select_related("conversation").first()
            if call:
                return call
        phone_call_id = str(custom.get("phoneCallId") or custom.get("phone_call_id") or "").strip()
        if phone_call_id.isdigit():
            return PhoneCall.objects.filter(pk=int(phone_call_id)).select_related("conversation").first()
        return None

    @sync_to_async
    def _stream_token_valid(self, phone_call: PhoneCall, provided: str | None) -> bool:
        return verify_stream_token(phone_call, provided)

    @sync_to_async
    def _call_allowed(self, phone_call: PhoneCall) -> bool:
        if not bool(getattr(settings, "VOICE_PHONE_ENABLED", False)):
            return False
        try:
            if not AgentSettings.load().phone_enabled:
                return False
        except Exception:
            logger.warning("Could not load AgentSettings for phone admission; rejecting call %s", phone_call.external_id)
            return False
        if phone_call.status in {
            PhoneCall.Status.ENDED,
            PhoneCall.Status.FAILED,
            PhoneCall.Status.TRANSFERRED,
        }:
            return False

        max_concurrent = int(getattr(settings, "VOICE_MAX_CONCURRENT_CALLS", 2))
        active_count = PhoneCall.objects.filter(
            status__in=[PhoneCall.Status.RINGING, PhoneCall.Status.ANSWERED, PhoneCall.Status.ACTIVE]
        ).exclude(pk=phone_call.pk).count()
        if active_count >= max_concurrent:
            logger.warning("Phone call %s rejected: concurrent limit reached", phone_call.external_id)
            return False

        max_minutes = int(getattr(settings, "VOICE_MAX_MINUTES_PER_DAY", 120))
        if max_minutes > 0:
            today = timezone.localdate()
            qs = PhoneCall.objects.filter(created_at__date=today).exclude(duration_seconds__isnull=True)
            used_seconds = sum(qs.values_list("duration_seconds", flat=True))
            # Count active calls as at least started minutes.
            active_today = PhoneCall.objects.filter(
                created_at__date=today,
                status__in=[PhoneCall.Status.RINGING, PhoneCall.Status.ANSWERED, PhoneCall.Status.ACTIVE],
            ).exclude(pk=phone_call.pk)
            for c in active_today:
                if c.answered_at:
                    used_seconds += max(int((timezone.now() - c.answered_at).total_seconds()), 0)
            if used_seconds >= max_minutes * 60:
                logger.warning("Phone call %s rejected: daily minutes cap reached", phone_call.external_id)
                return False
            if used_seconds >= max_minutes * 60 * 0.8:
                logger.warning(
                    "Phone daily minutes at %ss/%ss (80%% threshold)",
                    used_seconds,
                    max_minutes * 60,
                )

        per_number_limit = int(getattr(settings, "VOICE_MAX_CALLS_PER_NUMBER_PER_HOUR", 6))
        from_number = (phone_call.from_number or "").strip()
        if per_number_limit > 0 and from_number:
            since = timezone.now() - timezone.timedelta(hours=1)
            recent_count = PhoneCall.objects.filter(
                from_number=from_number, created_at__gte=since
            ).exclude(pk=phone_call.pk).count()
            if recent_count >= per_number_limit:
                logger.warning(
                    "Phone call %s rejected: per-number hourly limit reached", phone_call.external_id
                )
                return False
        return True

    @sync_to_async
    def _ensure_conversation(self, phone_call: PhoneCall) -> Conversation | None:
        from django.db import transaction

        with transaction.atomic():
            locked = PhoneCall.objects.select_for_update().select_related("conversation").get(pk=phone_call.pk)
            if locked.conversation_id:
                return locked.conversation
            from apps.agents.engine import AgentUnavailableError, start_conversation

            try:
                conversation = start_conversation(channel=Conversation.Channel.PHONE, language="pl")
            except AgentUnavailableError:
                return None
            except Exception:
                logger.exception("Failed to create conversation for phone call %s", locked.external_id)
                return None
            from .telephony import resolve_client_by_phone

            client = resolve_client_by_phone(locked.from_number)
            if client is not None:
                conversation.client = client
                conversation.save(update_fields=["client", "updated_at"])
            locked.conversation = conversation
            locked.save(update_fields=["conversation", "updated_at"])
            # keep in-memory state in sync
            phone_call.conversation = conversation
            phone_call.conversation_id = conversation.pk
            return conversation

    @sync_to_async
    def _mark_call_active(self, state: BridgeState) -> None:
        call = state.phone_call
        if call is None:
            return
        updates = []
        if call.status in {PhoneCall.Status.RINGING, PhoneCall.Status.ANSWERED}:
            call.status = PhoneCall.Status.ACTIVE
            updates.append("status")
        if call.answered_at is None:
            call.answered_at = timezone.now()
            updates.append("answered_at")
        if call.started_at is None:
            call.started_at = call.answered_at or timezone.now()
            updates.append("started_at")
        if updates:
            updates.append("updated_at")
            call.save(update_fields=list(dict.fromkeys(updates)))

    async def _max_duration_exceeded(self, state: BridgeState) -> bool:
        max_seconds = int(getattr(settings, "VOICE_PHONE_MAX_SECONDS", 900))
        if state.session and state.session.settings_obj.phone_max_seconds:
            max_seconds = int(state.session.settings_obj.phone_max_seconds)
        return (time.monotonic() - state.call_started_at) >= max_seconds

    async def _end_call(self, send, state: BridgeState, *, reason: str) -> None:
        if state.closed:
            return
        logger.info("Ending phone call %s reason=%s", state.call_id, reason)
        if state.session:
            try:
                if reason == "max_duration":
                    text = (
                        "Przepraszam, osiągnęliśmy limit czasu rozmowy. Dziękuję za kontakt."
                        if (state.conversation and state.conversation.language == "pl")
                        else "Sorry, we've reached the call time limit. Thank you for calling."
                    )
                else:
                    text = await sync_to_async(state.session.fallback_reply)()
                if text:
                    await self._speak(send, state, text)
                    await self._drain_playback(state, timeout=3.0)
            except Exception:
                logger.exception("Failed to play end-of-call message for %s", state.call_id)
        self._expire_stale_marks(state, max_age=0.0)
        await self._finalize_call(state, status=PhoneCall.Status.ENDED, reason=reason)
        await self._hangup_provider_call(state)
        state.closed = True

    def _expire_stale_marks(self, state: BridgeState, max_age: float = 15.0) -> None:
        if state.pending_marks and state.mark_sent_at:
            if (time.monotonic() - state.mark_sent_at) > max_age:
                logger.warning("Expiring stale playback marks for %s", state.call_id)
                state.pending_marks.clear()
                state.mark_sent_at = 0.0
                state.last_activity_at = time.monotonic()
        if not state.pending_marks and state.bot_speaking and state.playback_queue.empty():
            state.bot_speaking = False
            state.mark_sent_at = 0.0

    async def _drain_playback(self, state: BridgeState, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._expire_stale_marks(state, max_age=2.0)
            if not state.bot_speaking and state.playback_queue.empty() and not state.pending_marks:
                return
            await asyncio.sleep(0.05)
        self._expire_stale_marks(state, max_age=0.0)

    async def _finalize_call(self, state: BridgeState, *, status: str, reason: str = "") -> None:
        await sync_to_async(self._finalize_call_sync)(state, status, reason)

    @staticmethod
    def _finalize_call_sync(state: BridgeState, status: str, reason: str = "") -> None:
        call = state.phone_call
        if call is None:
            return
        updates = []
        if call.status not in {PhoneCall.Status.ENDED, PhoneCall.Status.FAILED, PhoneCall.Status.TRANSFERRED} or status == PhoneCall.Status.TRANSFERRED:
            call.status = status
            updates.append("status")
        if call.ended_at is None:
            call.ended_at = timezone.now()
            updates.append("ended_at")
        if call.answered_at and call.duration_seconds is None:
            call.duration_seconds = max(int((call.ended_at - call.answered_at).total_seconds()), 0)
            updates.append("duration_seconds")
        metadata = dict(call.metadata or {})
        if reason:
            metadata["end_reason"] = reason
        metadata["bridge_closed_at"] = timezone.now().isoformat()
        rate = float(getattr(settings, "VOICE_ESTIMATED_COST_PER_MINUTE_USD", 0) or 0)
        if rate > 0 and call.duration_seconds:
            metadata["estimated_cost_usd"] = round((call.duration_seconds / 60.0) * rate, 4)
        if metadata != (call.metadata or {}):
            call.metadata = metadata
            updates.append("metadata")
        if not call.summary:
            summary = OpenLineMediaBridge._build_call_summary(call)
            if summary:
                call.summary = summary
                updates.append("summary")
        if updates:
            updates.append("updated_at")
            call.save(update_fields=list(dict.fromkeys(updates)))

    @staticmethod
    def _build_call_summary(call: PhoneCall) -> str:
        transcript = call.transcript or []
        if not transcript:
            return ""
        user_turns = [t.get("text", "").strip() for t in transcript if t.get("role") == "user" and t.get("text")]
        assistant_turns = [
            t.get("text", "").strip() for t in transcript if t.get("role") == "assistant" and t.get("text")
        ]
        parts = []
        if user_turns:
            parts.append("Caller: " + " / ".join(user_turns[:2])[:180])
        if assistant_turns:
            parts.append("Agent: " + assistant_turns[-1][:180])
        booking_ref = (call.metadata or {}).get("last_booking_reference")
        if booking_ref:
            parts.append(f"Booking: {booking_ref}")
        parts.append(f"Status: {call.status}")
        return " | ".join(parts)[:500]

    async def _try_provider_transfer(self, call_id: str, transfer_number: str) -> bool:
        """Best-effort transfer; OpenLine transfer API is not yet confirmed."""
        if not call_id or not transfer_number:
            return False

        def _call():
            from django_openline import services as openline_services

            client = openline_services.get_client()
            # Try a conventional transfer endpoint if the server supports it.
            # This is best-effort and intentionally guarded; failure falls back to hangup.
            for op_name, payload in (
                ("transfer", {"to": transfer_number}),
                ("transfer_call", {"to": transfer_number}),
            ):
                try:
                    client.request(op_name, path_params={"call_id": call_id}, json_body=payload)
                    return True
                except Exception:
                    continue
            return False

        try:
            return await sync_to_async(_call)()
        except Exception:
            logger.info("Provider transfer unavailable for %s", call_id)
            return False

    async def _speak_safe(self, send, state: BridgeState, text: str) -> None:
        try:
            await self._speak(send, state, text)
            await self._drain_playback(state, timeout=5.0)
        except Exception:
            logger.debug("Safe speak failed for %s", state.call_id)

    @staticmethod
    def _failure_message(state: BridgeState, kind: str) -> str:
        lang = (state.conversation.language if state.conversation else "pl") or "pl"
        if lang == "pl":
            if kind == "stt":
                return "Przepraszam, mam problem ze zrozumieniem. Spróbuję połączyć Cię z recepcją."
            return "Przepraszam, mam chwilowy problem techniczny. Spróbuję połączyć Cię z recepcją."
        if kind == "stt":
            return "Sorry, I'm having trouble understanding. I'll try to connect you with reception."
        return "Sorry, I'm having a technical problem. I'll try to connect you with reception."

    async def _notify_staff_transfer_fallback(self, state: BridgeState) -> None:
        def _send():
            from django.core.mail import send_mail

            call = state.phone_call
            if call is None:
                return
            to_email = (getattr(settings, "HOTEL", {}) or {}).get("email") or getattr(settings, "DEFAULT_FROM_EMAIL", "")
            if not to_email:
                return
            subject = f"[Hotel Aurora] Phone transfer fallback: {call.external_id}"
            body = (
                f"Call: {call.external_id}\n"
                f"From: {redact_pii(call.from_number)}\n"
                f"To: {redact_pii(call.to_number)}\n"
                f"Status: {call.status}\n"
                f"Conversation: {call.conversation_id}\n"
                f"Reason: transfer requested but provider transfer unavailable; call ended.\n"
            )
            try:
                send_mail(subject, body, getattr(settings, "DEFAULT_FROM_EMAIL", None), [to_email], fail_silently=True)
            except Exception:
                logger.exception("Failed to send transfer fallback notification for %s", call.external_id)

        try:
            await sync_to_async(_send)()
        except Exception:
            logger.debug("Transfer fallback notification skipped for %s", state.call_id)

    async def _hangup_provider_call(self, state: BridgeState) -> None:
        call_id = state.call_id or (state.phone_call.external_id if state.phone_call else "")
        if not call_id:
            return
        try:
            from django_openline import services as openline_services

            await sync_to_async(openline_services.get_client().hangup)(call_id)
        except Exception:
            logger.info("OpenLine hangup failed/ignored for %s", call_id)


openline_media_bridge = OpenLineMediaBridge()
