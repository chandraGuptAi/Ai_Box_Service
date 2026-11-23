"""Security primitives: token encryption, webhook signatures, signed requests, hashing, redaction."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class TokenCipher:
    """AES-256-GCM with versioned keys.

    Stored value: ``nonce(12) || ciphertext+tag`` (bytes). The key version is stored next to it,
    and ``aad`` binds a ciphertext to its row (a token copied to another connection won't decrypt).
    """

    def __init__(self, keys: dict[str, bytes], active: str) -> None:
        if active not in keys:
            raise ValueError("active encryption key version is not in the key set")
        self._keys = {v: AESGCM(k) for v, k in keys.items()}
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


def new_key() -> str:
    """A fresh base64 key for META_TOKEN_ENCRYPTION_KEYS."""
    return base64.b64encode(os.urandom(32)).decode()


def sha256_hex(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def appsecret_proof(access_token: str, app_secret: str) -> str:
    return hmac.new(app_secret.encode(), access_token.encode(), hashlib.sha256).hexdigest()


def verify_webhook_signature(raw_body: bytes, header: str | None, app_secrets: list[str]) -> bool:
    """``X-Hub-Signature-256: sha256=<hex>`` over the exact raw body, compared in constant time."""
    if not header or not header.startswith("sha256="):
        return False
    sent = header[7:].strip().lower()
    ok = False
    for secret in filter(None, app_secrets):
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        ok |= hmac.compare_digest(expected, sent)
    return ok


def parse_signed_request(signed_request: str, app_secret: str) -> dict[str, Any] | None:
    """Meta ``signed_request`` (deauthorize / data deletion callbacks). Returns None if invalid."""
    try:
        encoded_sig, payload = signed_request.split(".", 1)
        sig = _b64url(encoded_sig)
        data = json.loads(_b64url(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if str(data.get("algorithm", "")).upper() != "HMAC-SHA256":
        return None
    expected = hmac.new(app_secret.encode(), payload.encode(), hashlib.sha256).digest()
    return data if hmac.compare_digest(expected, sig) else None


def _b64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


_SECRET_NAMES = {"code", "pin", "signed_request", "authorization", "password", "appsecret_proof", "body", "text"}
_SECRET_SUFFIXES = ("token", "secret", "_code", "verifier")
_TOKEN_LIKE = re.compile(r"\b(EAA[A-Za-z0-9]{20,}|IG[A-Za-z0-9]{30,}|[A-Za-z0-9_\-]{80,})\b")


def _secret_key(key: Any) -> bool:
    name = str(key).lower()
    return name in _SECRET_NAMES or name.endswith(_SECRET_SUFFIXES)


def redact(value: Any, depth: int = 0) -> Any:
    """Copy of ``value`` safe for logs/audit: secret-named keys masked, long token-like strings cut."""
    if depth > 6:
        return "[…]"
    if isinstance(value, dict):
        return {k: "[REDACTED]" if _secret_key(k) else redact(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, depth + 1) for v in value[:50]]
    if isinstance(value, str):
        return _TOKEN_LIKE.sub("[REDACTED]", value)[:500]
    return value
