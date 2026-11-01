"""Process-local upload rate-limit behavior."""

import pytest

from transcription_service.core.errors import RateLimitExceededError
from transcription_service.core.rate_limit import LocalRateLimiter


def test_sliding_window_rejects_then_recovers() -> None:
    now = [100.0]
    limiter = LocalRateLimiter(2, 10, clock=lambda: now[0])

    limiter.check("client")
    limiter.check("client")
    with pytest.raises(RateLimitExceededError) as raised:
        limiter.check("client")
    assert raised.value.headers == {"Retry-After": "10"}

    now[0] = 110.0
    limiter.check("client")


def test_clients_have_independent_windows() -> None:
    limiter = LocalRateLimiter(1, 60, clock=lambda: 1.0)

    limiter.check("first")
    limiter.check("second")
