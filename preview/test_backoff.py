import pytest

from backoff import MAX_ATTEMPTS, backoff_seconds


def test_backoff_first_failure():
    assert backoff_seconds(attempts=1) == 5 * 60


def test_backoff_second_failure():
    assert backoff_seconds(attempts=2) == 30 * 60


def test_backoff_third_failure():
    assert backoff_seconds(attempts=3) == 2 * 60 * 60


def test_backoff_fourth_failure():
    assert backoff_seconds(attempts=4) == 12 * 60 * 60


def test_max_attempts_is_5():
    assert MAX_ATTEMPTS == 5


def test_backoff_raises_when_attempts_at_max():
    with pytest.raises(ValueError):
        backoff_seconds(attempts=MAX_ATTEMPTS)


def test_backoff_raises_when_attempts_zero():
    with pytest.raises(ValueError):
        backoff_seconds(attempts=0)
