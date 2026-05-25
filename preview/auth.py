"""Authentication for /tick and /reconcile.

Two checks (defense in depth):
1. Internal shared secret (X-Internal-Secret header).
2. OIDC token from Cloud Scheduler (Phase 6 — gated on DEV_MODE).

In DEV_MODE=1 (local docker compose) only check #1 is enforced.
"""

import hmac as _hmac
import os


def is_dev_mode() -> bool:
    return os.environ.get("DEV_MODE") == "1"


def check_internal_secret(request_headers) -> bool:
    expected = os.environ.get("INTERNAL_SECRET", "")
    if not expected:
        return False
    provided = request_headers.get("X-Internal-Secret", "")
    return _hmac.compare_digest(expected, provided)


def check_oidc_token(request_headers) -> bool:
    """Verify a Google-issued OIDC token (from Cloud Scheduler).

    Caches Google's public keys via the underlying transport; lookups are fast
    after the first verification."""
    if is_dev_mode():
        return True
    auth = request_headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token = auth[len("Bearer ") :]
    try:
        from google.auth.transport import requests as ga_requests
        from google.oauth2 import id_token

        expected_audience = os.environ.get("OIDC_AUDIENCE", "")
        if not expected_audience:
            # Without an expected audience, we'd accept any valid Google OIDC token.
            # Refuse in prod rather than silently degrade security.
            return False

        request_adapter = ga_requests.Request()
        claims = id_token.verify_oauth2_token(token, request_adapter, audience=expected_audience)

        expected_sa = os.environ.get("SCHEDULER_SA_EMAIL", "")
        if expected_sa and claims.get("email") != expected_sa:
            return False
        return True
    except Exception:
        return False


def authorize_scheduler_request(request_headers) -> bool:
    if not check_internal_secret(request_headers):
        return False
    if not is_dev_mode() and not check_oidc_token(request_headers):
        return False
    return True
