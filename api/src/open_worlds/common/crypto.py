"""Symmetric encryption for secrets we must store at rest (Drive refresh tokens).

We never store Google refresh tokens in plaintext. They are encrypted with a
Fernet key that lives only in the process environment, so a database leak alone
does not expose the ability to touch a user's Drive.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, MultiFernet

from .config import get_settings, split_keys


def _cipher() -> MultiFernet:
    """Encrypts with the current key; decrypts with it or any old one, so a
    key can be rotated without breaking what is stored."""
    s = get_settings()
    keys = [s.fernet_key, *split_keys(s.fernet_old_keys)]
    return MultiFernet([Fernet(k.encode()) for k in keys])


def encrypt(plaintext: str) -> str:
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _cipher().decrypt(token.encode()).decode()


def reencrypt(token: str) -> str:
    """The same secret, encrypted with the current key."""
    return _cipher().rotate(token.encode()).decode()


def generate_key() -> str:
    """Convenience for operators generating a new key for the environment."""
    return Fernet.generate_key().decode()
