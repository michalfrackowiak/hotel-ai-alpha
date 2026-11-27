"""Voice/phone unit tests for OpenLine integration helpers."""

from __future__ import annotations

import json

import pytest

from apps.agents.models import Conversation
from apps.voice.audio import (
    BYTES_PER_8K_FRAME,
    chunk_pcm16,
    decode_twilio_media_payload,
    encode_twilio_media_payload,
    pcm16_8k_to_16k,
    pcm16_16k_to_8k,
    pcm16_to_mulaw,
)
from apps.voice.media_harness import (
    pcm16_silence,
    twilio_media_event,
    twilio_start_event,
    twilio_stop_event,
)
from apps.voice.models import PhoneCall
from apps.voice.telephony import (
    build_stream_url,
    ensure_stream_token,
    handle_openline_event,
    normalize_phone_number,
    redact_pii,
)


class FakeEvent:
    def __init__(self, event_type: str):
        self.event_type = event_type


class FakeCall:
    def __init__(self, openline_id: str, **attrs):
        self.openline_id = openline_id
        for k, v in attrs.items():
            setattr(self, k, v)


def test_pcm16_mulaw_roundtrip_preserves_length() -> None:
    pcm = bytearray(pcm16_silence(BYTES_PER_8K_FRAME))
    pcm[10:12] = (1200).to_bytes(2, "little", signed=True)
    pcm = bytes(pcm)
    mulaw = pcm16_to_mulaw(pcm)
    assert len(mulaw) == BYTES_PER_8K_FRAME // 2
    payload = encode_twilio_media_payload(pcm)
    decoded = decode_twilio_media_payload(payload)
    assert len(decoded) == len(pcm)


def test_resample_8k_16k_roundtrip_length() -> None:
    pcm8 = pcm16_silence(BYTES_PER_8K_FRAME * 2)
    pcm16, _ = pcm16_8k_to_16k(pcm8)
    back, _ = pcm16_16k_to_8k(pcm16)
    assert len(pcm16) == len(pcm8) * 2
    assert abs(len(back) - len(pcm8)) <= 4


def test_chunk_pcm16() -> None:
    pcm = pcm16_silence(BYTES_PER_8K_FRAME * 2)
    chunks = chunk_pcm16(pcm, BYTES_PER_8K_FRAME)
    assert len(chunks) == 2
    assert all(len(c) == BYTES_PER_8K_FRAME for c in chunks)


def test_twilio_media_harness_events() -> None:
    start = json.loads(twilio_start_event(call_id="call-1", stream_sid="MZ1"))
    assert start["event"] == "start"
    assert start["start"]["callSid"] == "call-1"
    assert start["streamSid"] == "MZ1"

    media = json.loads(twilio_media_event(stream_sid="MZ1", pcm16_8k=pcm16_silence(), sequence=2))
    assert media["event"] == "media"
    assert media["streamSid"] == "MZ1"
    assert media["media"]["track"] == "inbound"
    assert decode_twilio_media_payload(media["media"]["payload"]) == pcm16_silence()

    stop = json.loads(twilio_stop_event(stream_sid="MZ1"))
    assert stop["event"] == "stop"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("+48 123 123 123", "+48123123123"),
        ("0048123123123", "+48123123123"),
        ("123123123", "123123123"),
    ],
)
def test_normalize_phone_number(raw: str, expected: str) -> None:
    assert normalize_phone_number(raw) == expected


def test_redact_pii_masks_email_phone_and_reference() -> None:
    text = "Email john.doe@example.com ref HA-ABCD1234 phone +48123123123"
    redacted = redact_pii(text)
    assert "john.doe@example.com" not in redacted
    assert "j***@example.com" in redacted
    assert "HA-ABCD1234" not in redacted
    assert "HA-" in redacted and "***" in redacted
    assert "+48123123123" not in redacted
    assert "+4***23" in redacted


def test_build_stream_url_prefers_explicit(settings) -> None:
    settings.VOICE_PUBLIC_WSS_URL = "wss://example.com/ws/voice/openline/"
    settings.HOTEL_PUBLIC_URL = "https://hotel.example"
    assert build_stream_url() == "wss://example.com/ws/voice/openline"


def test_build_stream_url_from_hotel_public_url(settings) -> None:
    settings.VOICE_PUBLIC_WSS_URL = ""
    settings.HOTEL_PUBLIC_URL = "https://hotel.example/"
    assert build_stream_url() == "wss://hotel.example/ws/voice/openline/"


def test_build_stream_url_from_http_public_url(settings) -> None:
    settings.VOICE_PUBLIC_WSS_URL = ""
    settings.HOTEL_PUBLIC_URL = "http://localhost:8000/"
    assert build_stream_url() == "ws://localhost:8000/ws/voice/openline/"


def test_build_stream_url_empty_when_unconfigured(settings) -> None:
    settings.VOICE_PUBLIC_WSS_URL = ""
    settings.HOTEL_PUBLIC_URL = ""
    assert build_stream_url() == ""


def test_redact_pii_masks_phone_and_email() -> None:
    from apps.voice.telephony import redact_pii

    redacted = redact_pii("Call from +48 123 456 789 to anna@example.com")
    assert "+48 123 456 789" not in redacted
    assert "anna@example.com" not in redacted
    assert "***" in redacted
    assert "a***@example.com" in redacted


@pytest.mark.django_db
def test_handle_openline_event_creates_and_updates_phone_call(settings) -> None:
    settings.VOICE_PHONE_ENABLED = False
    event = FakeEvent("call.ringing")
    call = FakeCall("ol-call-1", direction="inbound", from_number="+48111111111", to_number="+48222222222", status="ringing")
    payload = {
        "call": {
            "id": "ol-call-1",
            "direction": "inbound",
            "from": "+48111111111",
            "to": "+48222222222",
            "status": "ringing",
        }
    }

    phone_call = handle_openline_event(event=event, call=call, payload=payload)
    assert phone_call is not None
    assert phone_call.external_id == "ol-call-1"
    assert phone_call.direction == PhoneCall.Direction.INBOUND
    assert phone_call.status == PhoneCall.Status.RINGING
    assert phone_call.from_number == "+48111111111"

    # Duplicate/replayed event is idempotent.
    phone_call2 = handle_openline_event(event=event, call=call, payload=payload)
    assert phone_call2.pk == phone_call.pk
    assert PhoneCall.objects.filter(external_id="ol-call-1").count() == 1

    # Answered event moves forward.
    answered = handle_openline_event(
        event=FakeEvent("call.answered"),
        call=FakeCall("ol-call-1", status="answered"),
        payload={"call": {"id": "ol-call-1", "status": "answered"}},
    )
    assert answered.status in {PhoneCall.Status.ANSWERED, PhoneCall.Status.ACTIVE}
    assert answered.answered_at is not None

    # Terminal event sets ended.
    ended = handle_openline_event(
        event=FakeEvent("call.ended"),
        call=FakeCall("ol-call-1", status="ended"),
        payload={"call": {"id": "ol-call-1", "status": "ended"}},
    )
    assert ended.status == PhoneCall.Status.ENDED
    assert ended.ended_at is not None


@pytest.mark.django_db
def test_handle_openline_event_out_of_order_terminal_does_not_regress(settings) -> None:
    settings.VOICE_PHONE_ENABLED = False
    handle_openline_event(
        event=FakeEvent("call.ended"),
        call=FakeCall("ol-call-2"),
        payload={"call": {"id": "ol-call-2", "status": "ended"}},
    )
    call = handle_openline_event(
        event=FakeEvent("call.answered"),
        call=FakeCall("ol-call-2"),
        payload={"call": {"id": "ol-call-2", "status": "answered"}},
    )
    assert call.status == PhoneCall.Status.ENDED


@pytest.mark.django_db
def test_handle_openline_event_returns_none_without_external_id(settings) -> None:
    settings.VOICE_PHONE_ENABLED = False
    event = FakeEvent("call.ringing")
    call = FakeCall("", direction="inbound")
    assert handle_openline_event(event=event, call=call, payload={"call": {}}) is None


@pytest.mark.django_db
def test_openline_handler_attaches_stream_for_inbound_ringing(settings, monkeypatch) -> None:
    settings.VOICE_PHONE_ENABLED = True
    settings.VOICE_PUBLIC_WSS_URL = "wss://example.com/ws/voice/openline/"
    settings.OPENLINE_INCOMING_STREAM_MODE = "webhook_attach"
    settings.OPENLINE_DEFAULT_STREAM_FORMAT = "twilio"

    from apps.agents.models import AgentSettings
    from apps.voice import openline_handlers

    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.save(update_fields=["is_enabled", "phone_enabled"])

    attached: dict = {}

    def fake_attach(call_id, **kwargs):
        attached["call_id"] = call_id
        attached["kwargs"] = kwargs

    monkeypatch.setattr("django_openline.services.attach_stream", fake_attach)

    event = FakeEvent("call.ringing")
    call = FakeCall("ol-attach-1", direction="inbound", from_number="+48111", to_number="+48222", status="ringing")
    payload = {"call": {"id": "ol-attach-1", "direction": "inbound", "from": "+48111", "to": "+48222", "status": "ringing"}}
    openline_handlers.handle_openline_webhook(event=event, call=call, payload=payload)

    assert attached["call_id"] == "ol-attach-1"
    assert attached["kwargs"]["url"].startswith("wss://example.com/ws/voice/openline")
    assert "token=" in attached["kwargs"]["url"]
    assert attached["kwargs"]["format"] == "twilio"
    assert attached["kwargs"]["parameters"]["streamToken"]
    assert PhoneCall.objects.filter(external_id="ol-attach-1").exists()


@pytest.mark.django_db
def test_openline_handler_skips_attach_when_not_webhook_mode(settings, monkeypatch) -> None:
    settings.VOICE_PHONE_ENABLED = True
    settings.VOICE_PUBLIC_WSS_URL = "wss://example.com/ws/voice/openline/"
    settings.OPENLINE_INCOMING_STREAM_MODE = "global"

    from apps.agents.models import AgentSettings
    from apps.voice import openline_handlers

    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.save(update_fields=["is_enabled", "phone_enabled"])

    called = {"count": 0}
    monkeypatch.setattr("django_openline.services.attach_stream", lambda *a, **k: called.__setitem__("count", called["count"] + 1))

    openline_handlers.handle_openline_webhook(
        event=FakeEvent("call.ringing"),
        call=FakeCall("ol-noattach-1", direction="inbound"),
        payload={"call": {"id": "ol-noattach-1", "direction": "inbound", "status": "ringing"}},
    )
    assert called["count"] == 0


@pytest.mark.django_db
def test_openline_handler_rollback_skips_attach_when_flag_off_or_stream_url_missing(settings, monkeypatch) -> None:
    settings.OPENLINE_INCOMING_STREAM_MODE = "webhook_attach"

    from apps.agents.models import AgentSettings
    from apps.voice import openline_handlers

    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.save(update_fields=["is_enabled", "phone_enabled"])

    called = {"count": 0}
    monkeypatch.setattr(
        "django_openline.services.attach_stream",
        lambda *a, **k: called.__setitem__("count", called["count"] + 1),
    )

    # Rollback: feature flag off.
    settings.VOICE_PHONE_ENABLED = False
    settings.VOICE_PUBLIC_WSS_URL = "wss://example.com/ws/voice/openline/"
    openline_handlers.handle_openline_webhook(
        event=FakeEvent("call.ringing"),
        call=FakeCall("ol-rb-1", direction="inbound"),
        payload={"call": {"id": "ol-rb-1", "direction": "inbound", "status": "ringing"}},
    )

    # Rollback: stream URL cleared.
    settings.VOICE_PHONE_ENABLED = True
    settings.VOICE_PUBLIC_WSS_URL = ""
    settings.HOTEL_PUBLIC_URL = ""
    openline_handlers.handle_openline_webhook(
        event=FakeEvent("call.ringing"),
        call=FakeCall("ol-rb-2", direction="inbound"),
        payload={"call": {"id": "ol-rb-2", "direction": "inbound", "status": "ringing"}},
    )

    assert called["count"] == 0
    assert PhoneCall.objects.filter(external_id__in=["ol-rb-1", "ol-rb-2"]).count() == 2


@pytest.mark.django_db
def test_place_outbound_call_creates_phone_call(settings, monkeypatch) -> None:
    settings.VOICE_PHONE_ENABLED = True
    settings.VOICE_PUBLIC_WSS_URL = "wss://example.com/ws/voice/openline/"

    from apps.agents.models import AgentSettings
    from apps.voice.services import place_outbound_call

    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.save(update_fields=["is_enabled", "phone_enabled"])

    class FakeOpenLineCall:
        openline_id = "ol-out-1"
        from_number = "+48000000000"

    monkeypatch.setattr(
        "apps.voice.services.openline_services.place_call",
        lambda **kwargs: FakeOpenLineCall(),
    )

    phone_call = place_outbound_call(to_number="+48123123123", reference="HA-TEST", metadata={"source": "test"})
    assert phone_call.external_id == "ol-out-1"
    assert phone_call.direction == PhoneCall.Direction.OUTBOUND
    assert phone_call.to_number == "+48123123123"
    assert phone_call.conversation_id is not None
    assert phone_call.conversation.channel == Conversation.Channel.PHONE
    assert phone_call.metadata["source"] == "test"


@pytest.mark.django_db
def test_place_outbound_call_rejects_when_disabled(settings) -> None:
    settings.VOICE_PHONE_ENABLED = False
    from apps.voice.services import PhoneServiceError, place_outbound_call

    with pytest.raises(PhoneServiceError) as exc:
        place_outbound_call(to_number="+48123123123")
    assert exc.value.code == "phone_disabled"


@pytest.mark.django_db
def test_place_outbound_call_wraps_openline_error(settings, monkeypatch) -> None:
    settings.VOICE_PHONE_ENABLED = True
    settings.VOICE_PUBLIC_WSS_URL = "wss://example.com/ws/voice/openline/"

    from django_openline.exceptions import OpenLineError

    from apps.agents.models import AgentSettings
    from apps.voice.services import PhoneServiceError, place_outbound_call

    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.save(update_fields=["is_enabled", "phone_enabled"])

    def boom(**kwargs):
        raise OpenLineError("boom")

    monkeypatch.setattr("apps.voice.services.openline_services.place_call", boom)

    with pytest.raises(PhoneServiceError) as exc:
        place_outbound_call(to_number="+48123123123")
    assert exc.value.code == "openline_error"


@pytest.mark.django_db
def test_openline_handler_swallows_attach_provider_error(settings, monkeypatch) -> None:
    settings.VOICE_PHONE_ENABLED = True
    settings.VOICE_PUBLIC_WSS_URL = "wss://example.com/ws/voice/openline/"
    settings.OPENLINE_INCOMING_STREAM_MODE = "webhook_attach"

    from apps.agents.models import AgentSettings
    from apps.voice import openline_handlers

    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.save(update_fields=["is_enabled", "phone_enabled"])

    def boom(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("django_openline.services.attach_stream", boom)

    # Should not raise; error is logged.
    openline_handlers.handle_openline_webhook(
        event=FakeEvent("call.ringing"),
        call=FakeCall("ol-err-1", direction="inbound"),
        payload={"call": {"id": "ol-err-1", "direction": "inbound", "status": "ringing"}},
    )
    assert PhoneCall.objects.filter(external_id="ol-err-1").exists()


@pytest.mark.django_db
def test_phone_call_api_requires_staff(settings) -> None:
    settings.VOICE_PHONE_ENABLED = True
    from rest_framework.test import APIClient

    client = APIClient()
    resp = client.post("/api/v1/phone/calls/", {"to_number": "+48123123123"}, format="json")
    assert resp.status_code in {401, 403}


@pytest.mark.django_db
def test_phone_call_api_returns_503_when_disabled(settings) -> None:
    settings.VOICE_PHONE_ENABLED = False
    from django.contrib.auth import get_user_model
    from rest_framework.test import APIClient

    User = get_user_model()
    staff = User.objects.create_user(username="staff-phone", password="pw", is_staff=True)
    client = APIClient()
    client.force_authenticate(user=staff)
    resp = client.post("/api/v1/phone/calls/", {"to_number": "+48123123123"}, format="json")
    assert resp.status_code == 503
    body = resp.json()
    err = body.get("error")
    if isinstance(err, dict):
        assert err.get("code") == "phone_disabled"
    else:
        assert body.get("code") == "phone_disabled" or err == "phone_disabled"


def test_split_tts_chunks_respects_sentence_boundaries() -> None:
    from apps.voice.bridge import OpenLineMediaBridge

    text = "Hello there. This is a second sentence. And a third one!"
    chunks = OpenLineMediaBridge._split_tts_chunks(text, max_chars=60)
    assert chunks
    assert all(len(c) <= 60 for c in chunks)
    assert "Hello there." in chunks[0]


def test_split_tts_chunks_splits_long_text() -> None:
    from apps.voice.bridge import OpenLineMediaBridge

    text = "word " * 80
    chunks = OpenLineMediaBridge._split_tts_chunks(text.strip(), max_chars=50)
    assert len(chunks) > 1
    assert all(len(c) <= 50 for c in chunks)


@pytest.mark.django_db
def test_call_allowed_rejects_when_voice_disabled(settings) -> None:
    from asgiref.sync import async_to_sync

    from apps.voice.bridge import OpenLineMediaBridge

    settings.VOICE_PHONE_ENABLED = False
    call = PhoneCall.objects.create(
        external_id="guard-1",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        from_number="+48111",
        to_number="+48222",
        status=PhoneCall.Status.RINGING,
    )
    bridge = OpenLineMediaBridge()
    assert async_to_sync(bridge._call_allowed)(call) is False


@pytest.mark.django_db
def test_call_allowed_rejects_per_number_hourly_limit(settings) -> None:
    from asgiref.sync import async_to_sync

    from apps.agents.models import AgentSettings
    from apps.voice.bridge import OpenLineMediaBridge

    settings.VOICE_PHONE_ENABLED = True
    settings.VOICE_MAX_CALLS_PER_NUMBER_PER_HOUR = 1
    settings.VOICE_MAX_CONCURRENT_CALLS = 10
    settings.VOICE_MAX_MINUTES_PER_DAY = 0
    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.save(update_fields=["is_enabled", "phone_enabled"])
    PhoneCall.objects.create(
        external_id="guard-prev",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        from_number="+48999",
        to_number="+48222",
        status=PhoneCall.Status.ENDED,
    )
    call = PhoneCall.objects.create(
        external_id="guard-now",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        from_number="+48999",
        to_number="+48222",
        status=PhoneCall.Status.RINGING,
    )
    bridge = OpenLineMediaBridge()
    assert async_to_sync(bridge._call_allowed)(call) is False


@pytest.mark.django_db
def test_call_allowed_accepts_normal_call(settings) -> None:
    from asgiref.sync import async_to_sync

    from apps.agents.models import AgentSettings
    from apps.voice.bridge import OpenLineMediaBridge

    settings.VOICE_PHONE_ENABLED = True
    settings.VOICE_MAX_CALLS_PER_NUMBER_PER_HOUR = 10
    settings.VOICE_MAX_CONCURRENT_CALLS = 10
    settings.VOICE_MAX_MINUTES_PER_DAY = 0
    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.save(update_fields=["is_enabled", "phone_enabled"])
    call = PhoneCall.objects.create(
        external_id="guard-ok",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        from_number="+48123",
        to_number="+48222",
        status=PhoneCall.Status.RINGING,
    )
    bridge = OpenLineMediaBridge()
    assert async_to_sync(bridge._call_allowed)(call) is True


@pytest.mark.django_db
def test_call_allowed_rejects_concurrent_limit(settings) -> None:
    from asgiref.sync import async_to_sync

    from apps.agents.models import AgentSettings
    from apps.voice.bridge import OpenLineMediaBridge

    settings.VOICE_PHONE_ENABLED = True
    settings.VOICE_MAX_CONCURRENT_CALLS = 1
    settings.VOICE_MAX_CALLS_PER_NUMBER_PER_HOUR = 10
    settings.VOICE_MAX_MINUTES_PER_DAY = 0
    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.save(update_fields=["is_enabled", "phone_enabled"])
    PhoneCall.objects.create(
        external_id="guard-active",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        from_number="+48111",
        to_number="+48222",
        status=PhoneCall.Status.ACTIVE,
    )
    call = PhoneCall.objects.create(
        external_id="guard-new",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        from_number="+48333",
        to_number="+48222",
        status=PhoneCall.Status.RINGING,
    )
    bridge = OpenLineMediaBridge()
    assert async_to_sync(bridge._call_allowed)(call) is False


@pytest.mark.django_db(transaction=True)
def test_bridge_closes_on_unknown_call(settings) -> None:
    import asyncio

    from asgiref.testing import ApplicationCommunicator

    from apps.voice.bridge import OpenLineMediaBridge

    async def run():
        settings.VOICE_PHONE_ENABLED = True
        app = OpenLineMediaBridge()
        communicator = ApplicationCommunicator(app, {"type": "websocket", "path": "/ws/voice/openline/"})
        await communicator.send_input({"type": "websocket.connect"})
        connected = await communicator.receive_output(timeout=2)
        assert connected["type"] == "websocket.accept"

        await communicator.send_input(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {"event": "start", "start": {"streamSid": "MZ1", "callSid": "unknown-call", "customParameters": {}}}
                ),
            }
        )
        # Bridge should reject unknown call and close.
        closed = await communicator.receive_output(timeout=3)
        assert closed["type"] == "websocket.close"
        await communicator.wait()

    asyncio.run(run())


@pytest.mark.django_db(transaction=True)
def test_bridge_dtmf_turn_emits_marks(settings, monkeypatch) -> None:
    import asyncio

    from asgiref.testing import ApplicationCommunicator

    from apps.agents.models import AgentSettings
    from apps.voice.audio import BYTES_PER_16K_FRAME
    from apps.voice.bridge import OpenLineMediaBridge

    settings.VOICE_PHONE_ENABLED = True
    settings.VOICE_PHONE_SILENCE_TIMEOUT_SECONDS = 60
    settings.VOICE_PHONE_MAX_SECONDS = 600

    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.save(update_fields=["is_enabled", "phone_enabled"])

    conversation = Conversation.objects.create(channel=Conversation.Channel.PHONE, language="pl")
    phone_call = PhoneCall.objects.create(
        external_id="bridge-dtmf-1",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        from_number="+48123123123",
        to_number="+48000000000",
        status=PhoneCall.Status.RINGING,
        conversation=conversation,
    )
    stream_token = ensure_stream_token(phone_call)

    class FakeSession:
        def __init__(self, *, phone_call, conversation, settings_obj=None):
            self.phone_call = phone_call
            self.conversation = conversation
            self.settings_obj = settings_obj or AgentSettings.load()
            self.texts: list[str] = []

        def initial_greeting(self):
            return "Witamy w hotelu."

        def handle_text(self, text):
            self.texts.append(text)
            return "Potwierdzam opcję jeden."

        def synthesize_pcm16(self, text):
            return b"\x00\x00" * (BYTES_PER_16K_FRAME // 2)  # 20ms 16kHz silence

        def should_transfer(self):
            return False

        def transfer_number(self):
            return ""

        def mark_transferred(self, reason=""):
            return None

        def fallback_reply(self):
            return "Przepraszam."

    monkeypatch.setattr("apps.voice.bridge.PhoneVoiceSession", FakeSession)

    async def run():
        app = OpenLineMediaBridge()
        communicator = ApplicationCommunicator(app, {"type": "websocket", "path": "/ws/voice/openline/"})
        await communicator.send_input({"type": "websocket.connect"})
        connected = await communicator.receive_output(timeout=2)
        assert connected["type"] == "websocket.accept"

        await communicator.send_input(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "event": "start",
                        "start": {
                            "streamSid": "MZDTMF",
                            "callSid": "bridge-dtmf-1",
                            "customParameters": {"streamToken": stream_token},
                        },
                    }
                ),
            }
        )

        # Collect initial output until we see a mark (greeting playback).
        seen_mark = False
        for _ in range(20):
            msg = await communicator.receive_output(timeout=2)
            if msg["type"] == "websocket.send" and msg.get("text"):
                payload = json.loads(msg["text"])
                if payload.get("event") == "mark":
                    seen_mark = True
                    break
        assert seen_mark

        # Send DTMF "1" and "#" to flush.
        await communicator.send_input(
            {"type": "websocket.receive", "text": json.dumps({"event": "dtmf", "dtmf": {"digit": "1"}})}
        )
        await communicator.send_input(
            {"type": "websocket.receive", "text": json.dumps({"event": "dtmf", "dtmf": {"digit": "#"}})}
        )

        # Expect another mark from the response TTS.
        seen_reply_mark = False
        for _ in range(30):
            msg = await communicator.receive_output(timeout=2)
            if msg["type"] == "websocket.send" and msg.get("text"):
                payload = json.loads(msg["text"])
                if payload.get("event") == "mark":
                    seen_reply_mark = True
                    break
            if msg["type"] == "websocket.close":
                break
        assert seen_reply_mark

        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        await communicator.wait()

    asyncio.run(run())


@pytest.mark.django_db(transaction=True)
def test_bridge_barge_in_sends_clear(settings, monkeypatch) -> None:
    import asyncio
    import struct

    from asgiref.testing import ApplicationCommunicator

    from apps.agents.models import AgentSettings
    from apps.voice.audio import BYTES_PER_16K_FRAME, encode_twilio_media_payload
    from apps.voice.bridge import OpenLineMediaBridge

    settings.VOICE_PHONE_ENABLED = True
    settings.VOICE_PHONE_SILENCE_TIMEOUT_SECONDS = 60
    settings.VOICE_PHONE_MAX_SECONDS = 600

    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.save(update_fields=["is_enabled", "phone_enabled"])

    conversation = Conversation.objects.create(channel=Conversation.Channel.PHONE, language="pl")
    phone_call = PhoneCall.objects.create(
        external_id="bridge-bargein-1",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        from_number="+48123123123",
        to_number="+48000000000",
        status=PhoneCall.Status.RINGING,
        conversation=conversation,
    )
    stream_token = ensure_stream_token(phone_call)

    class FakeSettings:
        phone_max_seconds = 600

    class FakeSession:
        def __init__(self, *, phone_call, conversation, settings_obj=None):
            self.phone_call = phone_call
            self.conversation = conversation
            self.settings_obj = settings_obj or FakeSettings()

        def initial_greeting(self):
            return "Dzień dobry, witamy w Hotelu Aurora. Jak mogę pomóc?"

        def handle_text(self, text):
            return "Oczywiście."

        def transcribe_pcm16(self, pcm16, sample_rate=16000):
            return ""

        def synthesize_pcm16(self, text):
            # ~2 seconds of 16kHz silence so bot_speaking stays true.
            return b"\x00\x00" * (BYTES_PER_16K_FRAME * 100)

        def should_transfer(self):
            return False

        def transfer_number(self):
            return ""

        def mark_transferred(self, reason=""):
            return None

        def fallback_reply(self):
            return "Przepraszam."

        def silence_prompt(self):
            return "Halo?"

        def silence_exceeded(self):
            return False

    monkeypatch.setattr("apps.voice.bridge.PhoneVoiceSession", FakeSession)

    async def run():
        app = OpenLineMediaBridge()
        communicator = ApplicationCommunicator(app, {"type": "websocket", "path": "/ws/voice/openline/"})
        await communicator.send_input({"type": "websocket.connect"})
        await communicator.receive_output(timeout=2)

        await communicator.send_input(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "event": "start",
                        "start": {
                            "streamSid": "MZBARGE",
                            "callSid": "bridge-bargein-1",
                            "customParameters": {"streamToken": stream_token},
                        },
                    }
                ),
            }
        )

        # Wait until we see some media (bot speaking).
        saw_media = False
        for _ in range(30):
            msg = await communicator.receive_output(timeout=2)
            if msg["type"] == "websocket.send" and msg.get("text"):
                payload = json.loads(msg["text"])
                if payload.get("event") == "media":
                    saw_media = True
                    break
        assert saw_media

        # Send loud caller audio to trigger barge-in.
        loud_8k = struct.pack("<h", 12000) * 160  # 20ms 8kHz loud frame
        media_event = json.dumps(
            {
                "event": "media",
                "streamSid": "MZBARGE",
                "media": {"track": "inbound", "chunk": "1", "payload": encode_twilio_media_payload(loud_8k)},
            }
        )
        for _ in range(3):
            await communicator.send_input({"type": "websocket.receive", "text": media_event})

        saw_clear = False
        for _ in range(40):
            msg = await communicator.receive_output(timeout=2)
            if msg["type"] == "websocket.send" and msg.get("text"):
                payload = json.loads(msg["text"])
                if payload.get("event") == "clear":
                    saw_clear = True
                    break
            if msg["type"] == "websocket.close":
                break
        assert saw_clear

        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        await communicator.wait()

    asyncio.run(run())


@pytest.mark.django_db(transaction=True)
def test_bridge_fake_audio_en_booking_flow(settings, monkeypatch, rooms) -> None:
    import asyncio
    import struct
    from datetime import timedelta

    from asgiref.testing import ApplicationCommunicator
    from django.utils import timezone

    from apps.agents.models import AgentSettings
    from apps.hotel.models import Booking
    from apps.voice.audio import BYTES_PER_16K_FRAME, encode_twilio_media_payload
    from apps.voice.bridge import OpenLineMediaBridge
    from apps.voice.phone_session import PhoneVoiceSession

    settings.VOICE_PHONE_ENABLED = True
    settings.VOICE_PHONE_SILENCE_TIMEOUT_SECONDS = 60
    settings.VOICE_PHONE_MAX_SECONDS = 600

    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.provider = "demo"
    agent_settings.save()

    check_in = timezone.localdate() + timedelta(days=30)
    check_out = check_in + timedelta(days=3)

    conversation = Conversation.objects.create(channel=Conversation.Channel.PHONE, language="en")
    conversation.messages.create(role="assistant", content="Hello, this is Hotel Aurora reception. How can I help you?")
    phone_call = PhoneCall.objects.create(
        external_id="bridge-flow-en-1",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        from_number="+48123123123",
        to_number="+48000000000",
        status=PhoneCall.Status.RINGING,
        conversation=conversation,
    )
    stream_token = ensure_stream_token(phone_call)

    utterances = iter(
        [
            f"I would like to book a room from {check_in.isoformat()} to {check_out.isoformat()} for 2 guests.",
            "The Standard Double, please.",
            "Yes, please confirm.",
            "Email, please.",
            "Anna Kowalska, anna@example.com",
        ]
    )

    monkeypatch.setattr(
        PhoneVoiceSession, "transcribe_pcm16", lambda self, pcm16, sample_rate=16000: next(utterances, "")
    )
    monkeypatch.setattr(
        PhoneVoiceSession,
        "synthesize_pcm16",
        lambda self, text: b"\x00\x00" * (BYTES_PER_16K_FRAME // 2),
    )

    async def send_utterance(communicator, stream_sid: str, seq_start: int) -> int:
        loud_8k = struct.pack("<h", 12000) * 160
        silence_8k = b"\x00\x00" * 160
        seq = seq_start
        for _ in range(8):
            payload = encode_twilio_media_payload(loud_8k)
            await communicator.send_input(
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"track": "inbound", "chunk": str(seq), "payload": payload},
                        }
                    ),
                }
            )
            seq += 1
            await asyncio.sleep(0.01)
        for _ in range(60):
            payload = encode_twilio_media_payload(silence_8k)
            await communicator.send_input(
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"track": "inbound", "chunk": str(seq), "payload": payload},
                        }
                    ),
                }
            )
            seq += 1
            await asyncio.sleep(0.02)
        return seq

    async def drain_until_mark(communicator, max_msgs=120):
        for _ in range(max_msgs):
            try:
                msg = await communicator.receive_output(timeout=3)
            except Exception:
                return False
            if msg["type"] == "websocket.send" and msg.get("text"):
                payload = json.loads(msg["text"])
                if payload.get("event") == "mark":
                    return True
            if msg["type"] == "websocket.close":
                return False
        return False

    async def run():
        app = OpenLineMediaBridge()
        communicator = ApplicationCommunicator(app, {"type": "websocket", "path": "/ws/voice/openline/"})
        await communicator.send_input({"type": "websocket.connect"})
        await communicator.receive_output(timeout=2)
        await communicator.send_input(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "event": "start",
                        "start": {
                            "streamSid": "MZFLOW",
                            "callSid": "bridge-flow-en-1",
                            "customParameters": {"streamToken": stream_token},
                        },
                    }
                ),
            }
        )
        assert await drain_until_mark(communicator)

        seq = 1
        for _ in range(5):
            seq = await send_utterance(communicator, "MZFLOW", seq)
            assert await drain_until_mark(communicator, max_msgs=200)

        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        await communicator.wait()

    asyncio.run(run())

    booking = Booking.objects.get()
    assert booking.status == Booking.Status.CONFIRMED
    assert booking.client.email == "anna@example.com"


def test_sanitize_webhook_payload_redacts_and_truncates() -> None:
    from django_openline.webhooks import _sanitize_payload

    payload = {
        "token": "a" * 80,
        "authorization": "short",
        "nested": {"password": "x" * 100, "ok": "yes"},
        "big": "y" * 3000,
        "list": list(range(60)),
    }
    sanitized = _sanitize_payload(payload)
    assert sanitized["token"] == "[redacted:80]"
    assert sanitized["authorization"] == "[redacted:5]"
    assert sanitized["nested"]["password"] == "[redacted:100]"
    assert sanitized["nested"]["ok"] == "yes"
    assert sanitized["big"].endswith("...[truncated:3000]")
    assert len(sanitized["list"]) == 51
    assert sanitized["list"][-1] == "[truncated]"


@pytest.mark.django_db
def test_should_attach_stream_requires_enabled_webhook_mode(settings) -> None:
    from apps.agents.models import AgentSettings
    from apps.voice.telephony import should_attach_stream

    settings.VOICE_PHONE_ENABLED = True
    settings.OPENLINE_INCOMING_STREAM_MODE = "webhook_attach"
    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.save(update_fields=["is_enabled", "phone_enabled"])
    call = PhoneCall.objects.create(
        external_id="attach-check-1",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        status=PhoneCall.Status.RINGING,
    )
    assert should_attach_stream(call) is True

    settings.OPENLINE_INCOMING_STREAM_MODE = "global"
    assert should_attach_stream(call) is False

    settings.OPENLINE_INCOMING_STREAM_MODE = "webhook_attach"
    settings.VOICE_PHONE_ENABLED = False
    assert should_attach_stream(call) is False

    settings.VOICE_PHONE_ENABLED = True
    agent_settings.phone_enabled = False
    agent_settings.save(update_fields=["phone_enabled"])
    assert should_attach_stream(call) is False


@pytest.mark.django_db
def test_should_attach_stream_rejects_terminal_call(settings) -> None:
    from apps.agents.models import AgentSettings
    from apps.voice.telephony import should_attach_stream

    settings.VOICE_PHONE_ENABLED = True
    settings.OPENLINE_INCOMING_STREAM_MODE = "webhook_attach"
    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.save(update_fields=["is_enabled", "phone_enabled"])
    call = PhoneCall.objects.create(
        external_id="attach-terminal-1",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        status=PhoneCall.Status.ENDED,
    )
    assert should_attach_stream(call) is False


@pytest.mark.django_db
def test_resolve_client_by_phone_matches_existing_client() -> None:
    from apps.hotel.models import Client
    from apps.voice.telephony import resolve_client_by_phone

    client = Client.objects.create(first_name="Jan", last_name="Test", email="jan@example.com", phone="+48123123123")
    resolved = resolve_client_by_phone("+48 123 123 123")
    assert resolved is not None
    assert resolved.pk == client.pk
    assert resolve_client_by_phone("+48999999999") is None


@pytest.mark.django_db
def test_process_webhook_sanitizes_stored_payload() -> None:
    from django_openline.models import Call, WebhookEvent
    from django_openline.webhooks import process_webhook

    raw = json.dumps(
        {
            "event": "call.ringing",
            "eventId": "evt-1",
            "call": {
                "id": "ol-db-1",
                "direction": "inbound",
                "from": "+48123123123",
                "to": "+48000000000",
                "status": "ringing",
                "token": "secret-token-value-should-be-redacted-if-long-enough-" + ("x" * 40),
            },
        }
    ).encode("utf-8")
    event, created = process_webhook(raw)
    assert created is True
    assert event.event_type == "call.ringing"
    assert WebhookEvent.objects.filter(pk=event.pk).exists()
    stored = event.payload
    assert stored["call"]["from"] == "+48123123123"
    assert "secret-token-value" not in str(stored["call"].get("token", ""))
    assert Call.objects.filter(openline_id="ol-db-1").exists()


@pytest.mark.django_db(transaction=True)
def test_bridge_fake_audio_pl_booking_flow(settings, monkeypatch, rooms) -> None:
    import asyncio
    import struct
    from datetime import timedelta

    from asgiref.testing import ApplicationCommunicator
    from django.utils import timezone

    from apps.agents.models import AgentSettings
    from apps.hotel.models import Booking
    from apps.voice.audio import BYTES_PER_16K_FRAME, encode_twilio_media_payload
    from apps.voice.bridge import OpenLineMediaBridge
    from apps.voice.phone_session import PhoneVoiceSession

    settings.VOICE_PHONE_ENABLED = True
    settings.VOICE_PHONE_SILENCE_TIMEOUT_SECONDS = 60
    settings.VOICE_PHONE_MAX_SECONDS = 600

    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.provider = "demo"
    agent_settings.save()

    check_in = timezone.localdate() + timedelta(days=30)
    check_out = check_in + timedelta(days=3)

    conversation = Conversation.objects.create(channel=Conversation.Channel.PHONE, language="pl")
    conversation.messages.create(role="assistant", content="Dzień dobry, tu recepcja Hotelu Aurora. Jak mogę pomóc?")
    phone_call = PhoneCall.objects.create(
        external_id="bridge-flow-pl-1",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        from_number="+48123123123",
        to_number="+48000000000",
        status=PhoneCall.Status.RINGING,
        conversation=conversation,
    )
    stream_token = ensure_stream_token(phone_call)

    utterances = iter(
        [
            f"Chcę zarezerwować pokój od {check_in.isoformat()} do {check_out.isoformat()} dla 2 osoby.",
            "Standard Double, proszę.",
            "Tak, potwierdzam.",
            "E-mail, proszę.",
            "Anna Kowalska, anna@example.com",
        ]
    )

    monkeypatch.setattr(
        PhoneVoiceSession, "transcribe_pcm16", lambda self, pcm16, sample_rate=16000: next(utterances, "")
    )
    monkeypatch.setattr(
        PhoneVoiceSession,
        "synthesize_pcm16",
        lambda self, text: b"\x00\x00" * (BYTES_PER_16K_FRAME // 2),
    )

    async def send_utterance(communicator, stream_sid: str, seq_start: int) -> int:
        loud_8k = struct.pack("<h", 12000) * 160
        silence_8k = b"\x00\x00" * 160
        seq = seq_start
        for _ in range(8):
            payload = encode_twilio_media_payload(loud_8k)
            await communicator.send_input(
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"track": "inbound", "chunk": str(seq), "payload": payload},
                        }
                    ),
                }
            )
            seq += 1
            await asyncio.sleep(0.01)
        for _ in range(60):
            payload = encode_twilio_media_payload(silence_8k)
            await communicator.send_input(
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"track": "inbound", "chunk": str(seq), "payload": payload},
                        }
                    ),
                }
            )
            seq += 1
            await asyncio.sleep(0.02)
        return seq

    async def drain_until_mark(communicator, max_msgs=120):
        for _ in range(max_msgs):
            try:
                msg = await communicator.receive_output(timeout=3)
            except Exception:
                return False
            if msg["type"] == "websocket.send" and msg.get("text"):
                payload = json.loads(msg["text"])
                if payload.get("event") == "mark":
                    return True
            if msg["type"] == "websocket.close":
                return False
        return False

    async def run():
        app = OpenLineMediaBridge()
        communicator = ApplicationCommunicator(app, {"type": "websocket", "path": "/ws/voice/openline/"})
        await communicator.send_input({"type": "websocket.connect"})
        await communicator.receive_output(timeout=2)
        await communicator.send_input(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "event": "start",
                        "start": {
                            "streamSid": "MZFLOWPL",
                            "callSid": "bridge-flow-pl-1",
                            "customParameters": {"streamToken": stream_token},
                        },
                    }
                ),
            }
        )
        assert await drain_until_mark(communicator)

        seq = 1
        for _ in range(5):
            seq = await send_utterance(communicator, "MZFLOWPL", seq)
            assert await drain_until_mark(communicator, max_msgs=200)

        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        await communicator.wait()

    asyncio.run(run())

    booking = Booking.objects.get()
    assert booking.status == Booking.Status.CONFIRMED
    assert booking.language == "pl"
    assert booking.client.email == "anna@example.com"


@pytest.mark.django_db(transaction=True)
def test_bridge_transfer_flow_falls_back(settings, monkeypatch) -> None:
    import asyncio
    import struct

    from asgiref.testing import ApplicationCommunicator

    from apps.agents.models import AgentSettings
    from apps.voice.audio import BYTES_PER_16K_FRAME, encode_twilio_media_payload
    from apps.voice.bridge import OpenLineMediaBridge

    settings.VOICE_PHONE_ENABLED = True
    settings.VOICE_PHONE_SILENCE_TIMEOUT_SECONDS = 60
    settings.VOICE_PHONE_MAX_SECONDS = 600

    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.save(update_fields=["is_enabled", "phone_enabled"])

    conversation = Conversation.objects.create(channel=Conversation.Channel.PHONE, language="en")
    conversation.messages.create(role="assistant", content="Hello, Hotel Aurora reception.")
    phone_call = PhoneCall.objects.create(
        external_id="bridge-transfer-1",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        from_number="+48123123123",
        to_number="+48000000000",
        status=PhoneCall.Status.RINGING,
        conversation=conversation,
    )
    stream_token = ensure_stream_token(phone_call)

    class FakeSession:
        def __init__(self, *, phone_call, conversation, settings_obj=None):
            self.phone_call = phone_call
            self.conversation = conversation
            self.settings_obj = settings_obj or agent_settings
            self._transfer = False

        def initial_greeting(self):
            return "Hello, Hotel Aurora reception."

        def handle_text(self, text):
            self._transfer = True
            return "I'll connect you with reception now."

        def transcribe_pcm16(self, pcm16, sample_rate=16000):
            return "I want to talk to a human"

        def synthesize_pcm16(self, text):
            return b"\x00\x00" * (BYTES_PER_16K_FRAME // 2)

        def should_transfer(self):
            return self._transfer

        def transfer_number(self):
            return "+48111222333"

        def mark_transferred(self, reason=""):
            self.phone_call.metadata = {**(self.phone_call.metadata or {}), "transfer_reason": reason}
            self.phone_call.save(update_fields=["metadata", "updated_at"])

        def fallback_reply(self):
            return "Sorry."

        def silence_prompt(self):
            return "Hello?"

        def silence_exceeded(self):
            return False

    async def _no_transfer(self, call_id, number):
        return False

    async def _noop(self, state):
        return None

    monkeypatch.setattr("apps.voice.bridge.PhoneVoiceSession", FakeSession)
    monkeypatch.setattr(OpenLineMediaBridge, "_try_provider_transfer", _no_transfer)
    monkeypatch.setattr(OpenLineMediaBridge, "_hangup_provider_call", _noop)
    monkeypatch.setattr(OpenLineMediaBridge, "_notify_staff_transfer_fallback", _noop)

    async def send_utterance(communicator, stream_sid: str):
        loud_8k = struct.pack("<h", 12000) * 160
        silence_8k = b"\x00\x00" * 160
        seq = 1
        for _ in range(8):
            await communicator.send_input(
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"track": "inbound", "chunk": str(seq), "payload": encode_twilio_media_payload(loud_8k)},
                        }
                    ),
                }
            )
            seq += 1
            await asyncio.sleep(0.01)
        for _ in range(60):
            await communicator.send_input(
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"track": "inbound", "chunk": str(seq), "payload": encode_twilio_media_payload(silence_8k)},
                        }
                    ),
                }
            )
            seq += 1
            await asyncio.sleep(0.02)

    async def run():
        app = OpenLineMediaBridge()
        communicator = ApplicationCommunicator(app, {"type": "websocket", "path": "/ws/voice/openline/"})
        await communicator.send_input({"type": "websocket.connect"})
        await communicator.receive_output(timeout=2)
        await communicator.send_input(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "event": "start",
                        "start": {
                            "streamSid": "MZTR",
                            "callSid": "bridge-transfer-1",
                            "customParameters": {"streamToken": stream_token},
                        },
                    }
                ),
            }
        )
        # drain greeting
        for _ in range(40):
            msg = await communicator.receive_output(timeout=2)
            if msg["type"] == "websocket.send" and msg.get("text"):
                if json.loads(msg["text"]).get("event") == "mark":
                    break

        await send_utterance(communicator, "MZTR")
        # after transfer fallback the bridge closes
        closed = False
        for _ in range(80):
            if communicator.future.done():
                while not communicator.output_queue.empty():
                    msg = communicator.output_queue.get_nowait()
                    if msg.get("type") == "websocket.close":
                        closed = True
                break
            try:
                msg = await communicator.receive_output(timeout=3)
            except (TimeoutError, asyncio.CancelledError):
                continue
            if msg["type"] == "websocket.close":
                closed = True
                break
        if not communicator.future.done():
            await communicator.wait()
        assert closed

    asyncio.run(run())

    phone_call = PhoneCall.objects.get(external_id="bridge-transfer-1")
    assert phone_call.metadata.get("transfer_reason") == "transfer_fallback_hangup"


@pytest.mark.django_db
def test_phone_session_cancel_flow(settings, rooms) -> None:
    from datetime import timedelta

    from django.utils import timezone

    from apps.agents.models import AgentSettings
    from apps.hotel.booking_service import create_booking
    from apps.hotel.models import Booking
    from apps.voice.phone_session import PhoneVoiceSession

    settings.VOICE_PHONE_ENABLED = True
    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.provider = "demo"
    agent_settings.save()

    check_in = timezone.localdate() + timedelta(days=30)
    check_out = check_in + timedelta(days=3)
    booking = create_booking(
        room_type=rooms[0].room_type,
        room=rooms[0],
        check_in=check_in,
        check_out=check_out,
        first_name="Anna",
        last_name="Kowalska",
        email="anna@example.com",
        adults=2,
        source=Booking.Source.VOICE,
        language="en",
    )
    from apps.hotel.booking_service import confirm_booking

    booking = confirm_booking(booking.reference)

    conversation = Conversation.objects.create(channel=Conversation.Channel.PHONE, language="en")
    phone_call = PhoneCall.objects.create(
        external_id="session-cancel-1",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        from_number="+48123123123",
        to_number="+48000000000",
        status=PhoneCall.Status.ACTIVE,
        conversation=conversation,
    )
    session = PhoneVoiceSession(phone_call=phone_call, conversation=conversation, settings_obj=agent_settings)
    reply = session.handle_text(f"Please cancel booking {booking.reference}")
    assert booking.reference in reply or "cancel" in reply.lower()
    booking.refresh_from_db()
    assert booking.status == Booking.Status.CANCELLED


@pytest.mark.django_db
def test_process_webhook_duplicate_delivery_is_idempotent() -> None:
    from django_openline.models import WebhookEvent
    from django_openline.webhooks import process_webhook

    raw = json.dumps(
        {
            "event": "call.ringing",
            "eventId": "evt-dup-1",
            "call": {"id": "ol-dup-1", "direction": "inbound", "from": "+48111", "to": "+48222", "status": "ringing"},
        }
    ).encode("utf-8")
    event1, created1 = process_webhook(raw)
    event2, created2 = process_webhook(raw)
    assert created1 is True
    assert created2 is False
    assert event1.pk == event2.pk
    assert WebhookEvent.objects.filter(event_type="call.ringing").count() == 1


@pytest.mark.django_db
def test_handle_openline_event_ignores_missing_external_id() -> None:
    # already covered above, but ensure payload-only missing id is safe
    phone_call = handle_openline_event(event=FakeEvent("call.ringing"), call=FakeCall(""), payload={})
    assert phone_call is None


@pytest.mark.django_db
def test_openline_handler_ignores_non_inbound(settings, monkeypatch) -> None:
    settings.VOICE_PHONE_ENABLED = True
    settings.VOICE_PUBLIC_WSS_URL = "wss://example.com/ws/voice/openline/"
    settings.OPENLINE_INCOMING_STREAM_MODE = "webhook_attach"

    from apps.agents.models import AgentSettings
    from apps.voice import openline_handlers

    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.save(update_fields=["is_enabled", "phone_enabled"])

    called = {"count": 0}
    monkeypatch.setattr("django_openline.services.attach_stream", lambda *a, **k: called.__setitem__("count", called["count"] + 1))

    openline_handlers.handle_openline_webhook(
        event=FakeEvent("call.ringing"),
        call=FakeCall("ol-out-1", direction="outbound"),
        payload={"call": {"id": "ol-out-1", "direction": "outbound", "status": "ringing"}},
    )
    assert called["count"] == 0


@pytest.mark.django_db
def test_process_webhook_rejects_invalid_json() -> None:
    from django_openline.webhooks import process_webhook

    with pytest.raises(ValueError):
        process_webhook(b"not-json")


@pytest.mark.django_db
def test_process_webhook_rejects_non_object_payload() -> None:
    from django_openline.webhooks import process_webhook

    with pytest.raises(ValueError):
        process_webhook(b"[1, 2, 3]")


@pytest.mark.django_db
def test_process_webhook_stores_event_without_call_id() -> None:
    from django_openline.models import Call, WebhookEvent
    from django_openline.webhooks import process_webhook

    raw = json.dumps({"event": "server.ping", "eventId": "evt-no-call-1", "data": {"ok": True}}).encode("utf-8")
    event, created = process_webhook(raw)

    assert created is True
    assert event.call is None
    assert WebhookEvent.objects.filter(pk=event.pk, event_type="server.ping").exists()
    assert Call.objects.count() == 0


def test_media_harness_sequence_builder() -> None:
    from apps.voice.media_harness import (
        twilio_connected_event,
        twilio_mark_event,
        twilio_media_sequence,
    )

    connected = json.loads(twilio_connected_event())
    assert connected["event"] == "connected"

    mark = json.loads(twilio_mark_event(stream_sid="MZ1", name="m1"))
    assert mark["event"] == "mark"
    assert mark["mark"]["name"] == "m1"

    seq = twilio_media_sequence(call_id="call-1", stream_sid="MZSEQ", media_frames=[pcm16_silence(), pcm16_silence()])
    events = [json.loads(e)["event"] for e in seq]
    assert events == ["connected", "start", "media", "media", "stop"]
    assert json.loads(seq[1])["start"]["callSid"] == "call-1"


def test_wav_pcm_fixture_round_trip(tmp_path) -> None:
    import wave

    from apps.voice.audio import mulaw_to_pcm16, pcm16_to_mulaw, resample_pcm16
    from apps.voice.media_harness import twilio_media_sequence

    # Synthetic WAV fixture: 16 kHz, 16-bit mono, 100ms of silence.
    wav_path = tmp_path / "silence_16k.wav"
    samples = b"\x00\x00" * 1600
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(samples)

    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getframerate() == 16000
        pcm16_16k = wf.readframes(wf.getnframes())

    pcm16_8k, _ = resample_pcm16(pcm16_16k, 16000, 8000)
    assert pcm16_8k == b"\x00\x00" * 800
    # Round-trip through mu-law as Twilio media would (160 PCM16 bytes = 80 samples = 80 mu-law bytes).
    encoded = pcm16_to_mulaw(pcm16_8k[:160])
    decoded = mulaw_to_pcm16(encoded)
    assert len(decoded) == 160

    seq = twilio_media_sequence(call_id="wav-fixture-1", stream_sid="MZWAV", media_frames=[pcm16_8k[:160]] * 3)
    events = [json.loads(e)["event"] for e in seq]
    assert events == ["connected", "start", "media", "media", "media", "stop"]


@pytest.mark.django_db(transaction=True)
def test_bridge_accepts_harness_media_sequence(settings, monkeypatch) -> None:
    import asyncio

    from asgiref.testing import ApplicationCommunicator

    from apps.agents.models import AgentSettings
    from apps.voice.audio import BYTES_PER_16K_FRAME
    from apps.voice.bridge import OpenLineMediaBridge
    from apps.voice.media_harness import twilio_media_sequence
    from apps.voice.phone_session import PhoneVoiceSession

    settings.VOICE_PHONE_ENABLED = True
    settings.VOICE_PHONE_SILENCE_TIMEOUT_SECONDS = 60

    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.save(update_fields=["is_enabled", "phone_enabled"])

    conversation = Conversation.objects.create(channel=Conversation.Channel.PHONE, language="en")
    conversation.messages.create(role="assistant", content="Hello.")
    PhoneCall.objects.create(
        external_id="harness-seq-1",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        from_number="+48123123123",
        to_number="+48000000000",
        status=PhoneCall.Status.RINGING,
        conversation=conversation,
    )

    monkeypatch.setattr(
        PhoneVoiceSession,
        "synthesize_pcm16",
        lambda self, text: b"\x00\x00" * (BYTES_PER_16K_FRAME // 2),
    )

    # Silence-only frames: no speech detected, bridge should handle sequence cleanly.
    seq = twilio_media_sequence(
        call_id="harness-seq-1",
        stream_sid="MZHARNESS",
        media_frames=[pcm16_silence() for _ in range(5)],
    )

    async def run():
        app = OpenLineMediaBridge()
        communicator = ApplicationCommunicator(app, {"type": "websocket", "path": "/ws/voice/openline/"})
        await communicator.send_input({"type": "websocket.connect"})
        await communicator.receive_output(timeout=2)

        for event in seq:
            await communicator.send_input({"type": "websocket.receive", "text": event})
        # stop event in sequence should close the bridge
        for _ in range(40):
            msg = await communicator.receive_output(timeout=3)
            if msg["type"] == "websocket.close":
                return
        raise AssertionError("Bridge did not close after harness stop event")

    asyncio.run(run())


@pytest.mark.django_db
def test_prune_phone_data_dry_run_and_apply() -> None:
    from datetime import timedelta
    from io import StringIO

    from django.core.management import call_command
    from django.utils import timezone

    old = timezone.now() - timedelta(days=40)
    recent = timezone.now() - timedelta(days=1)

    old_call = PhoneCall.objects.create(
        external_id="old-call-1",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        from_number="+48123123123",
        to_number="+48000000000",
        status=PhoneCall.Status.ENDED,
        transcript=[{"role": "user", "content": "my email is anna@example.com"}],
        summary="Caller booked a room.",
        metadata={"recording_opt_out": False},
        recording_url="https://example.com/rec.wav",
    )
    PhoneCall.objects.filter(pk=old_call.pk).update(created_at=old)

    recent_call = PhoneCall.objects.create(
        external_id="recent-call-1",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        from_number="+48123123123",
        to_number="+48000000000",
        status=PhoneCall.Status.ENDED,
        transcript=[{"role": "user", "content": "hello"}],
        summary="Recent.",
        metadata={"k": "v"},
    )
    PhoneCall.objects.filter(pk=recent_call.pk).update(created_at=recent)

    out = StringIO()
    call_command("prune_phone_data", "--days", "30", stdout=out)
    old_call.refresh_from_db()
    recent_call.refresh_from_db()
    # Dry run: no changes applied
    assert old_call.transcript != []
    assert old_call.summary == "Caller booked a room."
    assert recent_call.summary == "Recent."

    out = StringIO()
    call_command("prune_phone_data", "--days", "30", "--apply", stdout=out)
    old_call.refresh_from_db()
    recent_call.refresh_from_db()
    assert old_call.transcript == []
    assert old_call.summary == "[redacted]"
    assert old_call.metadata.get("redacted") is True
    assert old_call.recording_url == ""
    assert "anna@example.com" not in old_call.from_number
    assert old_call.from_number != "+48123123123" or old_call.from_number == ""
    # Recent call untouched
    assert recent_call.summary == "Recent."
    assert recent_call.transcript == [{"role": "user", "content": "hello"}]


@pytest.mark.django_db
def test_handle_openline_event_caps_oversized_fields(settings) -> None:
    settings.VOICE_PHONE_ENABLED = False
    long_id = "x" * 200
    long_num = "+" + "1" * 100
    event = FakeEvent("call.ringing")
    call = FakeCall(long_id, direction="inbound", from_number=long_num, to_number=long_num, status="ringing")
    payload = {"call": {"id": long_id, "from": long_num, "to": long_num, "status": "ringing"}}
    phone_call = handle_openline_event(event=event, call=call, payload=payload)
    assert phone_call is not None
    assert len(phone_call.external_id) == 128
    assert len(phone_call.from_number) <= 64
    assert len(phone_call.to_number) <= 64
    assert PhoneCall.objects.filter(external_id=phone_call.external_id).exists()


@pytest.mark.django_db
def test_handle_openline_event_handles_non_dict_payload(settings) -> None:
    settings.VOICE_PHONE_ENABLED = False
    call = FakeCall("ol-nondict-1", direction="inbound", from_number="+48111", to_number="+48222", status="ringing")
    phone_call = handle_openline_event(event=FakeEvent("call.ringing"), call=call, payload=None)  # type: ignore[arg-type]
    assert phone_call is not None
    assert phone_call.external_id == "ol-nondict-1"


@pytest.mark.django_db
def test_handle_openline_event_handles_invalid_datetime_and_duration(settings) -> None:
    settings.VOICE_PHONE_ENABLED = False
    payload = {
        "call": {
            "id": "ol-bad-time-1",
            "status": "active",
            "startedAt": "not-a-date",
            "answeredAt": "2026-13-99T99:99:99Z",
            "endedAt": "",
            "durationSeconds": "not-a-number",
        }
    }
    phone_call = handle_openline_event(event=FakeEvent("call.active"), call=FakeCall("ol-bad-time-1"), payload=payload)
    assert phone_call is not None
    assert phone_call.status == PhoneCall.Status.ACTIVE
    assert phone_call.duration_seconds is None


@pytest.mark.django_db
def test_handle_openline_event_does_not_overwrite_transferred_with_terminal(settings) -> None:
    settings.VOICE_PHONE_ENABLED = False
    call = PhoneCall.objects.create(
        external_id="ol-transfer-protect-1",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        status=PhoneCall.Status.TRANSFERRED,
    )
    payload = {"call": {"id": "ol-transfer-protect-1", "status": "completed"}}
    updated = handle_openline_event(event=FakeEvent("call.completed"), call=FakeCall("ol-transfer-protect-1"), payload=payload)
    assert updated is not None
    call.refresh_from_db()
    assert call.status == PhoneCall.Status.TRANSFERRED


@pytest.mark.django_db
def test_handle_openline_event_extracts_call_id_from_snapshot(settings) -> None:
    settings.VOICE_PHONE_ENABLED = False
    payload = {"call": {"call_id": "ol-snapshot-id-1", "status": "ringing", "direction": "inbound"}}
    updated = handle_openline_event(event=FakeEvent("call.ringing"), call=FakeCall("", status="ringing"), payload=payload)
    assert updated is not None
    assert updated.external_id == "ol-snapshot-id-1"


@pytest.mark.django_db(transaction=True)
def test_bridge_rejects_missing_or_invalid_stream_token(settings) -> None:
    import asyncio

    from asgiref.testing import ApplicationCommunicator

    from apps.agents.models import AgentSettings
    from apps.voice.bridge import OpenLineMediaBridge

    settings.VOICE_PHONE_ENABLED = True
    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.save(update_fields=["is_enabled", "phone_enabled"])

    conversation = Conversation.objects.create(channel=Conversation.Channel.PHONE, language="pl")
    phone_call = PhoneCall.objects.create(
        external_id="bridge-auth-1",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        status=PhoneCall.Status.RINGING,
        conversation=conversation,
    )
    ensure_stream_token(phone_call)

    async def run(payload_token):
        app = OpenLineMediaBridge()
        communicator = ApplicationCommunicator(app, {"type": "websocket", "path": "/ws/voice/openline/"})
        await communicator.send_input({"type": "websocket.connect"})
        connected = await communicator.receive_output(timeout=2)
        assert connected["type"] == "websocket.accept"
        custom = {} if payload_token is None else {"streamToken": payload_token}
        await communicator.send_input(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "event": "start",
                        "start": {"streamSid": "MZAUTH", "callSid": "bridge-auth-1", "customParameters": custom},
                    }
                ),
            }
        )
        closed = await communicator.receive_output(timeout=3)
        assert closed["type"] == "websocket.close"
        await communicator.wait()

    asyncio.run(run(None))
    asyncio.run(run("wrong-token"))


@pytest.mark.django_db
def test_prune_phone_data_redacts_linked_phone_conversation(settings) -> None:
    from datetime import timedelta
    from io import StringIO

    from django.core.management import call_command
    from django.utils import timezone

    from apps.agents.models import Message

    old = timezone.now() - timedelta(days=40)
    conversation = Conversation.objects.create(channel=Conversation.Channel.PHONE, language="pl")
    Message.objects.create(conversation=conversation, role=Message.Role.USER, content="My email is anna@example.com")
    Message.objects.create(
        conversation=conversation,
        role=Message.Role.ASSISTANT,
        content="Thanks",
        tool_calls=[{"name": "lookup", "args": {"email": "anna@example.com"}}],
        tool_call_id="call-1",
    )
    call = PhoneCall.objects.create(
        external_id="old-prune-conv-1",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        status=PhoneCall.Status.ENDED,
        conversation=conversation,
        transcript=[{"role": "user", "content": "secret"}],
        summary="summary",
    )
    PhoneCall.objects.filter(pk=call.pk).update(created_at=old)

    out = StringIO()
    call_command("prune_phone_data", "--days", "30", "--apply", stdout=out)
    msg1, msg2 = list(conversation.messages.order_by("pk"))
    assert msg1.content == "[redacted]"
    assert msg2.content == "[redacted]"
    assert msg2.tool_calls == []
    assert msg2.tool_call_id == ""


@pytest.mark.django_db
def test_outbound_call_does_not_create_conversation_when_openline_fails(settings, monkeypatch) -> None:
    from django_openline.exceptions import OpenLineError

    from apps.agents.models import AgentSettings
    from apps.voice.services import PhoneServiceError, place_outbound_call

    settings.VOICE_PHONE_ENABLED = True
    settings.VOICE_PUBLIC_WSS_URL = "wss://example.com/ws/voice/openline/"
    agent = AgentSettings.load()
    agent.is_enabled = True
    agent.phone_enabled = True
    agent.save(update_fields=["is_enabled", "phone_enabled"])

    def _boom(**kwargs):
        raise OpenLineError("boom")

    monkeypatch.setattr("apps.voice.services.openline_services.place_call", _boom)

    before = Conversation.objects.count()
    with pytest.raises(PhoneServiceError) as exc:
        place_outbound_call(to_number="+48123123123")
    assert exc.value.code == "openline_error"
    assert Conversation.objects.count() == before


@pytest.mark.django_db
def test_call_allowed_rejects_transferred_call(settings) -> None:
    from asgiref.sync import async_to_sync

    from apps.agents.models import AgentSettings
    from apps.voice.bridge import OpenLineMediaBridge

    settings.VOICE_PHONE_ENABLED = True
    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.save(update_fields=["is_enabled", "phone_enabled"])
    call = PhoneCall.objects.create(
        external_id="guard-transferred-1",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        status=PhoneCall.Status.TRANSFERRED,
    )
    bridge = OpenLineMediaBridge()
    assert async_to_sync(bridge._call_allowed)(call) is False


@pytest.mark.django_db(transaction=True)
def test_dtmf_llm_repeated_failure_escalates_to_transfer(settings, monkeypatch) -> None:
    import asyncio

    from asgiref.testing import ApplicationCommunicator

    from apps.agents.models import AgentSettings
    from apps.voice.bridge import OpenLineMediaBridge

    settings.VOICE_PHONE_ENABLED = True
    settings.VOICE_PHONE_SILENCE_TIMEOUT_SECONDS = 60
    settings.VOICE_PHONE_MAX_SECONDS = 600

    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.save(update_fields=["is_enabled", "phone_enabled"])

    conversation = Conversation.objects.create(channel=Conversation.Channel.PHONE, language="pl")
    phone_call = PhoneCall.objects.create(
        external_id="bridge-dtmf-fail-1",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        status=PhoneCall.Status.RINGING,
        conversation=conversation,
    )
    stream_token = ensure_stream_token(phone_call)

    class FakeSession:
        def __init__(self, *, phone_call, conversation, settings_obj=None):
            self.phone_call = phone_call
            self.conversation = conversation
            self.settings_obj = settings_obj or AgentSettings.load()

        def initial_greeting(self):
            return "Witamy."

        def handle_text(self, text):
            raise RuntimeError("llm down")

        def synthesize_pcm16(self, text):
            return b"\x00\x00" * 160

        def should_transfer(self):
            return False

        def transfer_number(self):
            return ""

        def mark_transferred(self, reason=""):
            self.phone_call.status = PhoneCall.Status.TRANSFERRED
            self.phone_call.metadata = {**(self.phone_call.metadata or {}), "transfer_reason": reason}
            self.phone_call.save(update_fields=["status", "metadata", "updated_at"])

        def fallback_reply(self):
            return "Przepraszam."

    async def _noop(*args, **kwargs):
        return None

    async def _no_transfer(self, call_id, number):
        return False

    monkeypatch.setattr("apps.voice.bridge.PhoneVoiceSession", FakeSession)
    monkeypatch.setattr(OpenLineMediaBridge, "_hangup_provider_call", _noop)
    monkeypatch.setattr(OpenLineMediaBridge, "_notify_staff_transfer_fallback", _noop)
    monkeypatch.setattr(OpenLineMediaBridge, "_try_provider_transfer", _no_transfer)

    async def run():
        app = OpenLineMediaBridge()
        communicator = ApplicationCommunicator(app, {"type": "websocket", "path": "/ws/voice/openline/"})
        await communicator.send_input({"type": "websocket.connect"})
        await communicator.receive_output(timeout=2)
        await communicator.send_input(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "event": "start",
                        "start": {
                            "streamSid": "MZDTMFFAIL",
                            "callSid": "bridge-dtmf-fail-1",
                            "customParameters": {"streamToken": stream_token},
                        },
                    }
                ),
            }
        )
        # Drain greeting
        for _ in range(40):
            msg = await communicator.receive_output(timeout=2)
            if msg["type"] == "websocket.send" and msg.get("text"):
                if json.loads(msg["text"]).get("event") == "mark":
                    break

        for _ in range(2):
            await communicator.send_input(
                {"type": "websocket.receive", "text": json.dumps({"event": "dtmf", "dtmf": {"digit": "1"}})}
            )
            await communicator.send_input(
                {"type": "websocket.receive", "text": json.dumps({"event": "dtmf", "dtmf": {"digit": "#"}})}
            )
            await asyncio.sleep(0.2)

        closed = False
        for _ in range(80):
            if communicator.future.done():
                while not communicator.output_queue.empty():
                    msg = communicator.output_queue.get_nowait()
                    if msg.get("type") == "websocket.close":
                        closed = True
                break
            try:
                msg = await communicator.receive_output(timeout=3)
            except (TimeoutError, asyncio.CancelledError):
                continue
            if msg["type"] == "websocket.close":
                closed = True
                break
        assert closed
        if not communicator.future.done():
            await communicator.wait()

    asyncio.run(run())
    phone_call.refresh_from_db()
    assert phone_call.status == PhoneCall.Status.TRANSFERRED


@pytest.mark.django_db
def test_process_audio_idle_silence_not_reset_by_silent_media(settings) -> None:
    import asyncio
    import time
    from types import SimpleNamespace

    from apps.voice.bridge import BridgeState, OpenLineMediaBridge

    settings.VOICE_PHONE_SILENCE_TIMEOUT_SECONDS = 1

    bridge = OpenLineMediaBridge()
    state = BridgeState()
    state.started = True
    state.session = SimpleNamespace(settings_obj=SimpleNamespace(phone_max_seconds=0))
    state.last_activity_at = time.monotonic() - 2.0

    called = {"count": 0}

    async def fake_timeout(send, st):
        called["count"] += 1

    bridge._handle_silence_timeout = fake_timeout  # type: ignore[assignment]

    before = state.last_activity_at
    asyncio.run(bridge._process_audio(b"\x00\x00" * 160, send=None, state=state))
    assert called["count"] == 1
    assert state.last_activity_at == before


@pytest.mark.django_db(transaction=True)
def test_bridge_mark_ack_clears_bot_speaking(settings, monkeypatch) -> None:
    import asyncio

    from asgiref.testing import ApplicationCommunicator

    from apps.agents.models import AgentSettings
    from apps.voice.audio import BYTES_PER_16K_FRAME
    from apps.voice.bridge import OpenLineMediaBridge

    settings.VOICE_PHONE_ENABLED = True
    settings.VOICE_PHONE_SILENCE_TIMEOUT_SECONDS = 60
    settings.VOICE_PHONE_MAX_SECONDS = 600

    agent_settings = AgentSettings.load()
    agent_settings.is_enabled = True
    agent_settings.phone_enabled = True
    agent_settings.save(update_fields=["is_enabled", "phone_enabled"])

    conversation = Conversation.objects.create(channel=Conversation.Channel.PHONE, language="pl")
    phone_call = PhoneCall.objects.create(
        external_id="bridge-mark-ack-1",
        provider=PhoneCall.Provider.OPENLINE,
        direction=PhoneCall.Direction.INBOUND,
        status=PhoneCall.Status.RINGING,
        conversation=conversation,
    )
    stream_token = ensure_stream_token(phone_call)

    class FakeSession:
        def __init__(self, *, phone_call, conversation, settings_obj=None):
            self.phone_call = phone_call
            self.conversation = conversation
            self.settings_obj = settings_obj or AgentSettings.load()

        def initial_greeting(self):
            return "Witamy w hotelu."

        def handle_text(self, text):
            return "Dobrze."

        def synthesize_pcm16(self, text):
            return b"\x00\x00" * (BYTES_PER_16K_FRAME // 2)

        def should_transfer(self):
            return False

        def transfer_number(self):
            return ""

        def mark_transferred(self, reason=""):
            return None

        def fallback_reply(self):
            return "Przepraszam."

    monkeypatch.setattr("apps.voice.bridge.PhoneVoiceSession", FakeSession)

    async def run():
        app = OpenLineMediaBridge()
        communicator = ApplicationCommunicator(app, {"type": "websocket", "path": "/ws/voice/openline/"})
        await communicator.send_input({"type": "websocket.connect"})
        await communicator.receive_output(timeout=2)
        await communicator.send_input(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "event": "start",
                        "start": {
                            "streamSid": "MZMARKACK",
                            "callSid": "bridge-mark-ack-1",
                            "customParameters": {"streamToken": stream_token},
                        },
                    }
                ),
            }
        )

        mark_name = None
        for _ in range(30):
            msg = await communicator.receive_output(timeout=2)
            if msg["type"] == "websocket.send" and msg.get("text"):
                payload = json.loads(msg["text"])
                if payload.get("event") == "mark":
                    mark_name = payload["mark"]["name"]
                    break
        assert mark_name

        # Before ack, bot should still be considered speaking (defensive timeout not yet elapsed).
        # Send ack and then ensure bridge can proceed cleanly.
        await communicator.send_input(
            {
                "type": "websocket.receive",
                "text": json.dumps({"event": "mark", "mark": {"name": mark_name}}),
            }
        )
        await asyncio.sleep(0.1)
        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        await communicator.wait()

    asyncio.run(run())

