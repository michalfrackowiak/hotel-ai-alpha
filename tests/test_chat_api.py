"""Chat and voice HTTP endpoints."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.mark.django_db
def test_chat_session_can_be_created(client):
    response = client.post("/api/v1/chat/sessions/", {"language": "en"}, format="json")

    assert response.status_code == 201
    payload = response.json()
    assert payload["session_key"]
    assert payload["channel"] == "chat"
    assert payload["language"] == "en"
    assert payload["greeting"]


@pytest.mark.django_db
def test_chat_session_accepts_polish(client):
    response = client.post("/api/v1/chat/sessions/", {"language": "pl"}, format="json")

    assert response.status_code == 201
    assert response.json()["language"] == "pl"
    assert "Dzień dobry" in response.json()["greeting"]


@pytest.mark.django_db
def test_chat_message_round_trip(client):
    session = client.post("/api/v1/chat/sessions/", {"language": "en"}, format="json").json()
    key = session["session_key"]

    response = client.post(
        f"/api/v1/chat/sessions/{key}/messages/",
        {"message": "Hello, what dates should I give you?"},
        format="json",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["reply"]
    assert payload["session_key"] == key

    detail = client.get(f"/api/v1/chat/sessions/{key}/")
    assert detail.status_code == 200
    messages = detail.json()["messages"]
    assert len(messages) >= 3
    assert messages[-1]["role"] == "assistant"


@pytest.mark.django_db
def test_chat_message_rejects_empty_text(client):
    session = client.post("/api/v1/chat/sessions/", {"language": "en"}, format="json").json()

    response = client.post(
        f"/api/v1/chat/sessions/{session['session_key']}/messages/",
        {"message": "   "},
        format="json",
    )

    assert response.status_code == 400
    assert "error" in response.json()


@pytest.mark.django_db
def test_unknown_session_returns_404(client):
    response = client.get("/api/v1/chat/sessions/not-a-real-session/")

    assert response.status_code == 404


@pytest.mark.django_db
def test_voice_config_payload(client):
    response = client.get("/api/v1/voice/config/")

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert {language["code"] for language in payload["languages"]} == {"en", "pl"}
    assert payload["stt"]["provider"] == "browser"
    assert payload["tts"]["provider"] == "browser"
    assert payload["greeting"]["en"]
