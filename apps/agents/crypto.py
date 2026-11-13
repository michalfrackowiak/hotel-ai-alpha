"""Symmetric encryption for provider credentials at rest.

The Fernet key is derived from ``FIELD_ENCRYPTION_KEY`` when set, otherwise from
``DJANGO_SECRET_KEY``. Ciphertexts are prefixed so plaintext records created
before v0.2.0 keep working and are encrypted on their next save.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

PREFIX = "enc:v1:"


def _key_bytes(explicit_key: str | None = None) -> bytes:
    material = explicit_key or getattr(settings, "FIELD_ENCRYPTION_KEY", "") or settings.SECRET_KEY
    return base64.urlsafe_b64encode(hashlib.sha256(material.encode("utf-8")).digest())


def _fernet(explicit_key: str | None = None) -> Fernet:
    return Fernet(_key_bytes(explicit_key))


def encrypt(value: str, *, key: str | None = None) -> str:
    if not value:
        return ""
    if value.startswith(PREFIX):
        return value
    token = _fernet(key).encrypt(value.encode("utf-8")).decode("ascii")
    return f"{PREFIX}{token}"


def decrypt(value: str, *, key: str | None = None) -> str:
    if not value or not value.startswith(PREFIX):
        return value
    try:
        return _fernet(key).decrypt(value[len(PREFIX) :].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError(
            "Could not decrypt a stored credential. Check FIELD_ENCRYPTION_KEY or "
            "run `manage.py rotate_provider_keys` with the old key."
        ) from exc


def rotate(value: str, *, old_key: str, new_key: str) -> str:
    return encrypt(decrypt(value, key=old_key), key=new_key)


def is_encrypted(value: str) -> bool:
    return bool(value and value.startswith(PREFIX))


def rotate_provider_keys(*, old_key: str, new_key: str) -> int:
    """Re-encrypt every stored provider key. Returns the number of records."""
    from django.db import connection

    from .models import AIProvider

    table = AIProvider._meta.db_table
    pk_column = AIProvider._meta.pk.column
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {pk_column}, api_key FROM {table}")
        rows = cursor.fetchall()

    rotated = 0
    for pk, value in rows:
        if not value:
            continue
        new_value = (
            rotate(value, old_key=old_key, new_key=new_key)
            if is_encrypted(value)
            else encrypt(value, key=new_key)
        )
        AIProvider.objects.filter(pk=pk).update(api_key=new_value)
        rotated += 1
    return rotated
