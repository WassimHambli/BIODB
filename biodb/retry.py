"""Retry policy: exponential backoff with full jitter, honouring Retry-After."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

RETRY_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with full jitter.

    Attributes:
        max_attempts: Total attempts, including the first (5 means up to 4 retries).
        base: Backoff base in seconds.
        cap: Maximum backoff (and maximum honoured Retry-After) in seconds.
    """

    max_attempts: int = 5
    base: float = 0.5
    cap: float = 30.0

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise ValueError("max_attempts must be an integer >= 1")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if not self.base >= 0 or not self.cap >= 0:
            raise ValueError("base and cap must be >= 0")

    def backoff(self, attempt: int, rng: random.Random) -> float:
        """Full-jitter delay after failed attempt number `attempt` (1-based)."""
        ceiling = min(self.cap, self.base * (2 ** (attempt - 1)))
        return rng.uniform(0.0, ceiling)

    def delay(self, attempt: int, retry_after: float | None, rng: random.Random) -> float:
        """Delay before the next attempt; a server Retry-After is a floor. (Retry-After values
        above ``cap`` never reach here: the transport raises ``RateLimitError`` instead.)"""
        jittered = self.backoff(attempt, rng)
        if retry_after is None:
            return jittered
        return max(jittered, min(retry_after, self.cap))


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date) into seconds, or None."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    current = now if now is not None else datetime.now(UTC)
    return max(0.0, (when - current).total_seconds())
