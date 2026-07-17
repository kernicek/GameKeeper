"""Symmetric encryption for BGG credentials at rest (issue #118).

Self-hostable product: a DB dump/backup must not leak stored BGG passwords, so
they are Fernet-encrypted before they hit the database.

Key precedence:
- ``BGG_ENCRYPTION_KEY`` env/setting when set — a urlsafe-base64 32-byte Fernet
  key (generate one with ``cryptography.fernet.Fernet.generate_key()``).
- Otherwise derived deterministically from ``SECRET_KEY``, so existing installs
  keep working with no new config.

Either source lives only in the environment, never in the database, and survives
restarts. Rotating ``SECRET_KEY`` without a stable ``BGG_ENCRYPTION_KEY``
invalidates stored ciphertext: ``decrypt`` then returns "" and the user
re-enters their password.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet_key() -> bytes:
    """The Fernet key bytes — explicit BGG_ENCRYPTION_KEY, else derived from
    SECRET_KEY (sha256 → urlsafe-base64 gives a valid 32-byte key)."""
    configured = getattr(settings, "BGG_ENCRYPTION_KEY", "") or ""
    if configured:
        return configured.encode() if isinstance(configured, str) else configured
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt(plaintext: str) -> str:
    """Fernet ciphertext token for a non-empty secret; "" for empty/None."""
    if not plaintext:
        return ""
    return Fernet(_fernet_key()).encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypted plaintext, or "" when empty or undecryptable (key rotated /
    corrupt) — callers treat "" as "no usable secret" and fall back to env."""
    if not ciphertext:
        return ""
    try:
        return Fernet(_fernet_key()).decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return ""
