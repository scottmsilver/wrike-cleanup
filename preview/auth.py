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
    """Placeholder. Phase 6 (Task 22) replaces this with real
    google.oauth2.id_token verification."""
    if is_dev_mode():
        return True
    auth = request_headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    # TODO Phase 6: validate the JWT against Google's JWKS.
    # For now, in non-dev mode, this returns False — Phase 6 will replace.
    return False


def authorize_scheduler_request(request_headers) -> bool:
    if not check_internal_secret(request_headers):
        return False
    if not is_dev_mode() and not check_oidc_token(request_headers):
        return False
    return True
