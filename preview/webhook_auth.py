import hashlib
import hmac


def verify_event_signature(secret: str, body: bytes, signature: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def compute_handshake_response(secret: str, secret_value: str) -> str:
    return hmac.new(secret.encode(), secret_value.encode(), hashlib.sha256).hexdigest()
