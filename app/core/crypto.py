"""Shared crypto helpers for modules that store third-party credentials (OAuth refresh tokens, codes).

- ``TokenCipher``: AES-256-GCM with versioned keys. A stored value is ``nonce(12) || ciphertext+tag``; the
  key version is stored next to it. ``aad`` (associated data) binds a ciphertext to its database row, so
  a token copied into another row will not decrypt.
- Keys come from an env var holding JSON ``{"1": "<base64 32 bytes>", "2": "..."}``: the active version
  encrypts, every listed version can still decrypt (key rotation).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class TokenCipher:
    def __init__(self, keys: dict[str, bytes], active: str) -> None:
        if active not in keys:
            raise ValueError("active encryption key version is not in the key set")
        self._keys = {version: AESGCM(key) for version, key in keys.items()}
        self.active = active

    def encrypt(self, plaintext: str, aad: str) -> tuple[bytes, str]:
        nonce = os.urandom(12)
        return nonce + self._keys[self.active].encrypt(nonce, plaintext.encode(), aad.encode()), self.active

    def decrypt(self, blob: bytes, version: str, aad: str) -> str:
        key = self._keys.get(version)
        if key is None:
            raise KeyError(f"encryption key version {version!r} is not configured")
        try:
            return key.decrypt(bytes(blob[:12]), bytes(blob[12:]), aad.encode()).decode()
        except InvalidTag as exc:
            raise ValueError("credential failed integrity check") from exc


def parse_keys(raw: str, env_name: str) -> dict[str, bytes]:
    """``{"1": "<base64>"}`` -> ``{"1": b"..."}``; every key must be 32 bytes (AES-256)."""
    if not raw:
        return {}
    keys = {str(version): base64.b64decode(value) for version, value in json.loads(raw).items()}
    for version, key in keys.items():
        if len(key) != 32:
            raise ValueError(f"{env_name}[{version}] must decode to 32 bytes")
    return keys


def new_key() -> str:
    """A fresh base64 key for a *_TOKEN_ENCRYPTION_KEYS setting."""
    return base64.b64encode(os.urandom(32)).decode()


def sha256_hex(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)
