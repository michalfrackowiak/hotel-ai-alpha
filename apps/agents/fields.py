"""Model fields that transparently encrypt small secrets at rest."""

from __future__ import annotations

from django.db import models

from .crypto import decrypt, encrypt


class EncryptedCharField(models.CharField):
    """A ``CharField`` whose database value is Fernet ciphertext.

    Reading returns plaintext; writing encrypts. Values without the ciphertext
    prefix (legacy rows or fixtures) are passed through so migrations can
    upgrade them in place.
    """

    def from_db_value(self, value, expression, connection):
        if value is None:
            return value
        return decrypt(value)

    def to_python(self, value):
        if value is None:
            return value
        return str(value)

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if not value:
            return value
        return encrypt(value)
