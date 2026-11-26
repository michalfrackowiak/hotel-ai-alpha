"""Audio helpers for telephony media streams.

Pure-Python implementation (no audioop dependency) for Python 3.13+ compatibility.
"""

from __future__ import annotations

import base64
import struct

SAMPLE_RATE_8K = 8000
SAMPLE_RATE_16K = 16000
SAMPLE_WIDTH = 2  # 16-bit PCM
CHANNELS = 1
FRAME_MS = 20
BYTES_PER_8K_FRAME = int(SAMPLE_RATE_8K * (FRAME_MS / 1000) * SAMPLE_WIDTH)  # 320
BYTES_PER_16K_FRAME = int(SAMPLE_RATE_16K * (FRAME_MS / 1000) * SAMPLE_WIDTH)  # 640
MULAW_BYTES_PER_FRAME = 160

_BIAS = 0x84
_CLIP = 32635
_SIGN_BIT = 0x80
_QUANT_MASK = 0x0F
_SEG_SHIFT = 4
_SEG_MASK = 0x70
_SEG_END = (0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF)


def _search(val: int, table: tuple[int, ...]) -> int:
    for i, t in enumerate(table):
        if val <= t:
            return i
    return len(table)


def _linear2ulaw_sample(sample: int) -> int:
    sign = 0
    if sample < 0:
        sign = _SIGN_BIT
        sample = -sample
    if sample > _CLIP:
        sample = _CLIP
    sample += _BIAS
    exponent = _search(sample, _SEG_END)
    mantissa = (sample >> (exponent + 3)) & _QUANT_MASK
    return ~(sign | (exponent << _SEG_SHIFT) | mantissa) & 0xFF


def _ulaw2linear_sample(ulaw: int) -> int:
    ulaw = ~ulaw & 0xFF
    sign = ulaw & _SIGN_BIT
    exponent = (ulaw & _SEG_MASK) >> _SEG_SHIFT
    mantissa = ulaw & _QUANT_MASK
    sample = ((mantissa << 3) + _BIAS) << exponent
    sample -= _BIAS
    return -sample if sign else sample


def mulaw_to_pcm16(data: bytes) -> bytes:
    if not data:
        return b""
    return struct.pack("<" + "h" * len(data), *(_ulaw2linear_sample(b) for b in data))


def pcm16_to_mulaw(data: bytes) -> bytes:
    if not data:
        return b""
    n = len(data) // 2
    samples = struct.unpack("<" + "h" * n, data[: n * 2])
    return bytes(_linear2ulaw_sample(s) for s in samples)


def _resample_linear_2x_up(data: bytes) -> bytes:
    n = len(data) // 2
    if n == 0:
        return b""
    samples = struct.unpack("<" + "h" * n, data[: n * 2])
    out: list[int] = []
    for i, s in enumerate(samples):
        out.append(s)
        nxt = samples[i + 1] if i + 1 < n else s
        out.append(int((s + nxt) / 2))
    return struct.pack("<" + "h" * len(out), *out)


def _resample_linear_2x_down(data: bytes) -> bytes:
    n = len(data) // 2
    if n == 0:
        return b""
    samples = struct.unpack("<" + "h" * n, data[: n * 2])
    # Simple decimation by 2; good enough for telephony VAD/STT pipeline.
    out = samples[::2]
    return struct.pack("<" + "h" * len(out), *out)


def resample_pcm16(data: bytes, from_rate: int, to_rate: int, state=None):
    if from_rate == to_rate or not data:
        return data, state
    if from_rate == SAMPLE_RATE_8K and to_rate == SAMPLE_RATE_16K:
        return _resample_linear_2x_up(data), state
    if from_rate == SAMPLE_RATE_16K and to_rate == SAMPLE_RATE_8K:
        return _resample_linear_2x_down(data), state
    # Generic fallback: nearest-neighbor ratio resampling.
    n = len(data) // 2
    samples = struct.unpack("<" + "h" * n, data[: n * 2])
    out_len = max(int(n * to_rate / from_rate), 0)
    out = [samples[min(int(i * from_rate / to_rate), n - 1)] for i in range(out_len)] if n else []
    return struct.pack("<" + "h" * len(out), *out), state


def pcm16_8k_to_16k(data: bytes, state=None):
    return resample_pcm16(data, SAMPLE_RATE_8K, SAMPLE_RATE_16K, state)


def pcm16_16k_to_8k(data: bytes, state=None):
    return resample_pcm16(data, SAMPLE_RATE_16K, SAMPLE_RATE_8K, state)


def decode_twilio_media_payload(payload: str) -> bytes:
    """Base64 mu-law 8kHz -> PCM16 8kHz."""
    mulaw = base64.b64decode(payload or "")
    return mulaw_to_pcm16(mulaw)


def encode_twilio_media_payload(pcm16_8k: bytes) -> str:
    """PCM16 8kHz -> base64 mu-law 8kHz."""
    mulaw = pcm16_to_mulaw(pcm16_8k)
    return base64.b64encode(mulaw).decode("ascii")


def chunk_pcm16(pcm16: bytes, frame_bytes: int) -> list[bytes]:
    return [pcm16[i : i + frame_bytes] for i in range(0, len(pcm16), frame_bytes) if pcm16[i : i + frame_bytes]]


def rms_pcm16(data: bytes) -> int:
    if not data:
        return 0
    n = len(data) // 2
    if n == 0:
        return 0
    samples = struct.unpack("<" + "h" * n, data[: n * 2])
    mean_sq = sum(s * s for s in samples) / n
    return int(mean_sq**0.5)
