"""Encryption of provider API keys at rest and key rotation."""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.db import connection

from apps.agents.crypto import PREFIX, decrypt, encrypt, is_encrypted, rotate
from apps.agents.models import AIProvider


def _raw_api_key(provider: AIProvider) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT api_key FROM agents_aiprovider WHERE id = %s", [provider.pk])
        return cursor.fetchone()[0]


def test_encrypt_decrypt_round_trip():
    ciphertext = encrypt("sk-super-secret")

    assert ciphertext.startswith(PREFIX)
    assert "sk-super-secret" not in ciphertext
    assert decrypt(ciphertext) == "sk-super-secret"


def test_encrypt_is_idempotent_and_empty_stays_empty():
    ciphertext = encrypt("value")

    assert encrypt(ciphertext) == ciphertext
    assert encrypt("") == ""
    assert decrypt("") == ""
    assert is_encrypted(ciphertext) is True
    assert is_encrypted("plain") is False


def test_decrypt_rejects_a_wrong_key():
    ciphertext = encrypt("value", key="first-key")

    with pytest.raises(ValueError):
        decrypt(ciphertext, key="second-key")


def test_rotate_reencrypts_with_the_new_key():
    ciphertext = encrypt("value", key="old-key")

    rotated = rotate(ciphertext, old_key="old-key", new_key="new-key")

    assert decrypt(rotated, key="new-key") == "value"
    with pytest.raises(ValueError):
        decrypt(rotated, key="old-key")


@pytest.mark.django_db
def test_provider_api_key_is_encrypted_in_the_database():
    provider = AIProvider.objects.create(
        name="OpenAI",
        provider_type=AIProvider.Type.OPENAI,
        model="gpt-4o-mini",
        api_key="sk-plaintext",
    )

    raw = _raw_api_key(provider)
    assert raw.startswith(PREFIX)
    assert "sk-plaintext" not in raw
    assert provider.api_key == "sk-plaintext"
    provider.refresh_from_db()
    assert provider.api_key == "sk-plaintext"


@pytest.mark.django_db
def test_rotate_provider_keys_command(settings):
    provider = AIProvider.objects.create(
        name="OpenAI",
        provider_type=AIProvider.Type.OPENAI,
        model="gpt-4o-mini",
        api_key="sk-plaintext",
    )

    settings.FIELD_ENCRYPTION_KEY = "new-encryption-key"
    call_command("rotate_provider_keys", "--old-key", "test-secret-key")

    provider.refresh_from_db()
    assert provider.api_key == "sk-plaintext"
    assert _raw_api_key(provider).startswith(PREFIX)
