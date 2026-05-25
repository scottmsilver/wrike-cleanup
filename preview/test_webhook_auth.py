import hashlib
import hmac

from webhook_auth import compute_handshake_response, verify_event_signature

SECRET = "devsecret"


def test_verify_event_signature_matches():
    body = b'{"eventType":"AttachmentAdded","attachmentId":"A","taskId":"T"}'
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert verify_event_signature(SECRET, body, sig) is True


def test_verify_event_signature_rejects_wrong():
    body = b'{"eventType":"AttachmentAdded"}'
    assert verify_event_signature(SECRET, body, "deadbeef") is False


def test_handshake_response_is_hmac_of_secret_value():
    secret_value = "wrike-supplied-handshake-value"
    expected = hmac.new(SECRET.encode(), secret_value.encode(), hashlib.sha256).hexdigest()
    assert compute_handshake_response(SECRET, secret_value) == expected
