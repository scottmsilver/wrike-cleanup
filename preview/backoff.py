"""Retry backoff schedule for /tick failures."""

MAX_ATTEMPTS = 5

_SCHEDULE = {
    1: 5 * 60,
    2: 30 * 60,
    3: 2 * 60 * 60,
    4: 12 * 60 * 60,
}


def backoff_seconds(attempts: int) -> int:
    """Return seconds to wait before the next attempt.

    Raises ValueError if attempts >= MAX_ATTEMPTS — caller must check
    MAX_ATTEMPTS first and mark the job failed instead of calling this.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")
    if attempts >= MAX_ATTEMPTS:
        raise ValueError(
            f"attempts ({attempts}) >= MAX_ATTEMPTS ({MAX_ATTEMPTS}); "
            "caller should mark job failed instead of retrying"
        )
    return _SCHEDULE[attempts]
