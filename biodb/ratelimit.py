"""Thread-safe token-bucket rate limiting, one bucket per host."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

#: Default requests/second per host.
DEFAULT_RATES: dict[str, float] = {
    "rest.uniprot.org": 10.0,
    "data.rcsb.org": 10.0,
    "search.rcsb.org": 10.0,
    "files.rcsb.org": 10.0,
    "rest.ensembl.org": 15.0,
    "eutils.ncbi.nlm.nih.gov": 3.0,
}
NCBI_HOST = "eutils.ncbi.nlm.nih.gov"
NCBI_RATE_WITH_KEY = 10.0
#: Extra spacing in seconds added to every request interval (1/rate + margin). A sleep that
#: returns slightly early, or a clock read that lags the sleep, then cannot push a one-second
#: window over the service's published limit (NCBI: "no more than 3 requests per second").
SAFETY_MARGIN = 0.01


def _check_rate(rate: Any, who: str) -> float:
    """Return ``rate`` as a float or raise ``ValueError`` naming ``who``."""
    if (
        isinstance(rate, bool)
        or not isinstance(rate, (int, float))
        or not math.isfinite(rate)
        or rate <= 0
    ):
        raise ValueError(f"rate for {who} must be a positive finite number, got {rate!r}")
    return float(rate)


def default_rates(ncbi_api_key: str | None = None) -> dict[str, float]:
    """Return the default per-host rates; NCBI gets 10 req/s when an API key is present."""
    rates = dict(DEFAULT_RATES)
    if ncbi_api_key:
        rates[NCBI_HOST] = NCBI_RATE_WITH_KEY
    return rates


class TokenBucket:
    """Token bucket allowing `rate` requests/second with a burst of `capacity` tokens (default 1).

    Consecutive requests are spaced ``1/rate + margin`` seconds apart (``margin`` defaults to
    :data:`SAFETY_MARGIN`, 10 ms), so the effective rate is slightly below ``rate``. The
    margin is what keeps sleep jitter from putting more than ``rate`` requests into one
    second; it is not an exact guarantee for bursts (``capacity`` > 1).

    The clock and sleep functions are injectable so tests need no real time.
    """

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        margin: float = SAFETY_MARGIN,
    ) -> None:
        self.rate = _check_rate(rate, "TokenBucket")
        if not math.isfinite(margin) or margin < 0:
            raise ValueError(f"margin must be a non-negative finite number, got {margin!r}")
        #: Seconds between two requests: 1/rate plus the safety margin.
        self.interval = 1.0 / self.rate + margin
        # Default burst of 1: requests are evenly spaced (>= interval apart) instead of
        # arriving in a burst, which is what keeps a one-second window at or below `rate`.
        self.capacity = capacity if capacity is not None else 1.0
        self._clock = clock
        self._sleep = sleep
        self._tokens = self.capacity
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Take one token, sleeping if necessary. Returns the seconds waited."""
        with self._lock:
            now = self._clock()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) / self.interval)
            self._last = now
            self._tokens -= 1.0
            wait = -self._tokens * self.interval if self._tokens < 0 else 0.0
        if wait > 0:
            self._sleep(wait)
        return wait


class HostRateLimiter:
    """Registry of per-host token buckets. Hosts without a rate are not limited.

    Host names are case-insensitive. Every rate must be a positive finite number
    (``ValueError`` naming the host otherwise).

    **Scope:** a limiter is per instance and per process. All clients of one
    :class:`~biodb.client.BioDB` share one limiter (and therefore one budget per host), but two
    ``BioDB`` objects, two threads' separate instances or two processes each have their own
    budget and together can exceed a service's limit.
    """

    def __init__(
        self,
        rates: Mapping[str, float] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        source = DEFAULT_RATES if rates is None else rates
        self._rates = {
            host.strip().lower(): _check_rate(rate, host) for host, rate in source.items()
        }
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def acquire(self, host: str) -> float:
        """Block until a request to `host` is allowed. Returns the seconds waited."""
        host = host.strip().lower()
        rate = self._rates.get(host)
        if rate is None:
            return 0.0
        with self._lock:
            bucket = self._buckets.get(host)
            if bucket is None:
                bucket = TokenBucket(rate, clock=self._clock, sleep=self._sleep)
                self._buckets[host] = bucket
        return bucket.acquire()
