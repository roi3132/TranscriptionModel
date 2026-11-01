"""Small process-local sliding-window limiter for upload admission."""

from collections import defaultdict, deque
from collections.abc import Callable
from math import ceil
from threading import Lock
from time import monotonic

from transcription_service.core.errors import RateLimitExceededError


class LocalRateLimiter:
    """Limit requests per client address without trusting proxy headers."""

    def __init__(
        self,
        requests: int,
        window_seconds: int,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._limit = requests
        self._window = window_seconds
        self._clock = clock
        self._requests: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()
        self._next_cleanup = clock() + window_seconds

    def check(self, key: str) -> None:
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            if now >= self._next_cleanup:
                stale = [key for key, values in self._requests.items() if values[-1] <= cutoff]
                for stale_key in stale:
                    del self._requests[stale_key]
                self._next_cleanup = now + self._window
            requests = self._requests[key]
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if len(requests) >= self._limit:
                raise RateLimitExceededError(max(1, ceil(self._window - (now - requests[0]))))
            requests.append(now)
