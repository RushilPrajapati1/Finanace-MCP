"""API-key generation and verification.

Keys are high-entropy random tokens shown to the operator exactly once. We only
ever persist a SHA-256 hash of the key, so a database leak does not expose
usable credentials. Verification hashes the presented key and compares in
constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_KEY_BYTES = 32
_PREFIX = "sk_live_"


def generate_api_key() -> tuple[str, str, str]:
    """Return ``(raw_key, key_hash, display_prefix)``.

    ``raw_key`` is returned to the caller once and never stored.
    """
    raw = _PREFIX + secrets.token_urlsafe(_KEY_BYTES)
    return raw, hash_api_key(raw), raw[: len(_PREFIX) + 6]


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_api_key(raw: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(raw), expected_hash)
