"""Request signing and verification.

Authenticated requests carry ``X-Gateway-Key-Id``, ``X-Gateway-Timestamp`` (unix
seconds) and ``X-Gateway-Signature``: hex HMAC-SHA256 over the canonical string
``METHOD\\npath\\ntimestamp\\nsha256(body)``.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass, field

HEADER_KEY_ID = "X-Gateway-Key-Id"
HEADER_TIMESTAMP = "X-Gateway-Timestamp"
HEADER_SIGNATURE = "X-Gateway-Signature"
KEY_ID_PATTERN = re.compile(r"k[0-9]{1,3}")


class AuthError(Exception):
    """Raised when a request fails verification."""


@dataclass
class KeyRing:
    """The active signing key plus any retired keys still inside their grace window."""

    active_id: str
    keys: dict[str, bytes]
    retired_at: dict[str, float] = field(default_factory=dict)
    grace_seconds: int = 900

    def usable(self, key_id: str, now: float | None = None) -> bytes | None:
        now = time.time() if now is None else now
        retired = self.retired_at.get(key_id)
        if retired is not None and now - retired > self.grace_seconds:
            return None
        return self.keys.get(key_id)


def rotate_keys(ring: KeyRing, new_id: str, new_secret: bytes, now: float | None = None) -> KeyRing:
    """Make ``new_id`` the active key and start the grace clock on the old one."""
    if not KEY_ID_PATTERN.fullmatch(new_id):
        raise ValueError(f"key id {new_id!r} does not match {KEY_ID_PATTERN.pattern}")
    if new_id in ring.keys:
        raise ValueError(f"key id {new_id!r} already present")
    ring.retired_at[ring.active_id] = time.time() if now is None else now
    ring.keys[new_id] = new_secret
    ring.active_id = new_id
    return ring


def canonical(method: str, path: str, timestamp: int, body: bytes) -> bytes:
    return f"{method.upper()}\n{path}\n{timestamp}\n{hashlib.sha256(body).hexdigest()}".encode()


# legacy: HMAC-SHA256 over the canonical string, hex encoded.
def sign(secret: bytes, method: str, path: str, timestamp: int, body: bytes) -> str:
    return hmac.new(secret, canonical(method, path, timestamp, body), hashlib.sha256).hexdigest()


def verify(
    ring: KeyRing,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    max_skew_seconds: int = 300,
    now: float | None = None,
) -> str:
    """Return the key id that signed the request, or raise AuthError."""
    now = time.time() if now is None else now
    key_id = headers.get(HEADER_KEY_ID, "")
    secret = ring.usable(key_id, now)
    if secret is None:
        raise AuthError("unknown or expired key id")
    try:
        ts = int(headers.get(HEADER_TIMESTAMP, ""))
    except ValueError:
        raise AuthError("missing or malformed timestamp") from None
    if abs(now - ts) > max_skew_seconds:
        raise AuthError("timestamp outside allowed skew")
    expected = sign(secret, method, path, ts, body)
    if not hmac.compare_digest(expected, headers.get(HEADER_SIGNATURE, "")):
        raise AuthError("signature mismatch")
    return key_id
