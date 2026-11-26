"""Utilities for simulating Twilio-format OpenLine media streams in tests/dev."""

from __future__ import annotations

import base64
import json
import uuid

from .audio import BYTES_PER_8K_FRAME, pcm16_to_mulaw


def twilio_connected_event(*, protocol: str = "Call") -> str:
    return json.dumps({"event": "connected", "protocol": protocol, "version": "1.0.0"})


def twilio_start_event(*, call_id: str, stream_sid: str | None = None, custom_parameters: dict | None = None) -> str:
    stream_sid = stream_sid or f"MZ{uuid.uuid4().hex[:32]}"
    start = {
        "event": "start",
        "sequenceNumber": "1",
        "start": {
            "streamSid": stream_sid,
            "callSid": call_id,
            "customParameters": custom_parameters or {},
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        },
        "streamSid": stream_sid,
    }
    return json.dumps(start)


def twilio_mark_event(*, stream_sid: str, name: str, sequence: int = 3) -> str:
    return json.dumps(
        {"event": "mark", "sequenceNumber": str(sequence), "streamSid": stream_sid, "mark": {"name": name}}
    )


def twilio_media_event(*, stream_sid: str, pcm16_8k: bytes, sequence: int = 2) -> str:
    mulaw = pcm16_to_mulaw(pcm16_8k)
    payload = base64.b64encode(mulaw).decode("ascii")
    return json.dumps(
        {
            "event": "media",
            "sequenceNumber": str(sequence),
            "streamSid": stream_sid,
            "media": {"track": "inbound", "chunk": str(sequence - 1), "payload": payload},
        }
    )


def twilio_stop_event(*, stream_sid: str, sequence: int = 999) -> str:
    return json.dumps({"event": "stop", "sequenceNumber": str(sequence), "streamSid": stream_sid})


def pcm16_silence(frame_bytes: int = BYTES_PER_8K_FRAME) -> bytes:
    return b"\x00\x00" * (frame_bytes // 2)


def twilio_media_sequence(
    *,
    call_id: str,
    stream_sid: str | None = None,
    media_frames: list[bytes] | None = None,
    custom_parameters: dict | None = None,
) -> list[str]:
    """Build a minimal connected/start/media*/stop Twilio stream sequence."""
    stream_sid = stream_sid or f"MZ{uuid.uuid4().hex[:32]}"
    frames = media_frames if media_frames is not None else [pcm16_silence() for _ in range(3)]
    events = [twilio_connected_event(), twilio_start_event(call_id=call_id, stream_sid=stream_sid, custom_parameters=custom_parameters)]
    seq = 2
    for frame in frames:
        events.append(twilio_media_event(stream_sid=stream_sid, pcm16_8k=frame, sequence=seq))
        seq += 1
    events.append(twilio_stop_event(stream_sid=stream_sid, sequence=seq))
    return events