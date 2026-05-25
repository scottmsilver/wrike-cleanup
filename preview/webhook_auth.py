"""HMAC-SHA256 verification of Wrike webhook deliveries.

Wrike signs each event delivery with `HMAC-SHA256(signing_secret, raw_body)`
in the `X-Hook-Signature` header. The initial verification handshake instead
puts a random value in `X-Hook-Secret` and expects the receiver to echo
`HMAC-SHA256(signing_secret, received_secret_value)` back in its response
header — that's the `compute_handshake_response` path.
"""

import hashlib
import hmac


def verify_event_signature(secret: str, body: bytes, signature: str) -> bool:
    """Return True iff `signature` matches HMAC-SHA256 of `body` under `secret`.

    Uses `compare_digest` for constant-time comparison.
    """
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def compute_handshake_response(secret: str, secret_value: str) -> str:
    """Compute the value to echo back in the response's `X-Hook-Secret` header
    during Wrike's webhook verification handshake."""
    return hmac.new(secret.encode(), secret_value.encode(), hashlib.sha256).hexdigest()
