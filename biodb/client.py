"""The :class:`BioDB` facade."""

from __future__ import annotations

import os
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

import httpx

from .cache import DEFAULT_TTL, Cache
from .clients import EnsemblClient, NCBIClient, RCSBClient, UniProtClient
from .ratelimit import HostRateLimiter, default_rates
from .retry import RetryPolicy
from .transport import DEFAULT_TIMEOUT, Transport


@dataclass
class BioDBConfig:
    """Configuration for :class:`BioDB`.

    Attributes:
        cache_path: SQLite cache file. ``None`` uses ``$BIODB_CACHE_PATH`` or
            ``~/.cache/biodb/cache.sqlite``.
        cache_ttl: Default cache TTL in seconds (7 days).
        cache_enabled: ``False`` disables caching entirely.
        rate_limits: Per-host requests/second overrides, e.g. ``{"rest.ensembl.org": 5}``.
        max_attempts: Total attempts per request (including the first).
        timeout: HTTP timeout in seconds.
        ncbi_email: NCBI contact email (default: ``$NCBI_EMAIL``). Not shown in ``repr``.
        ncbi_api_key: NCBI API key (default: ``$NCBI_API_KEY``). Not shown in ``repr``.
        check_obsolete: On an RCSB 404, make one holdings call to tell removed entries
            (``ObsoleteEntryError``) from unknown ones.
        check_retired: On an Ensembl ``lookup_id`` not-found, make one archive call to tell
            retired IDs (``RetiredIdentifierError``) from unknown ones.
    """

    cache_path: Path | str | None = None
    cache_ttl: float = DEFAULT_TTL
    cache_enabled: bool = True
    rate_limits: Mapping[str, float] = field(default_factory=dict)
    max_attempts: int = 5
    timeout: float = DEFAULT_TIMEOUT
    ncbi_email: str | None = field(default=None, repr=False)
    ncbi_api_key: str | None = field(default=None, repr=False)
    check_obsolete: bool = True
    check_retired: bool = True


class BioDB:
    """One object exposing ``.uniprot``, ``.pdb``, ``.ensembl`` and ``.ncbi``.

    All four clients share a single :class:`~biodb.transport.Transport` (one HTTP client, one
    cache, one set of per-host rate limiters; the rate limits are per instance and per process).
    Use as a context manager, or call ``close()``.
    """

    def __init__(
        self,
        config: BioDBConfig | None = None,
        *,
        client: httpx.Client | None = None,
        http_transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        cfg = config or BioDBConfig()
        self.config = cfg
        api_key = (
            cfg.ncbi_api_key if cfg.ncbi_api_key is not None else os.environ.get("NCBI_API_KEY")
        )
        rates = {**default_rates(api_key), **dict(cfg.rate_limits)}
        retry = RetryPolicy(max_attempts=cfg.max_attempts)  # validate before touching the disk
        limiter = HostRateLimiter(rates, clock=clock, sleep=sleep)
        cache = Cache(cfg.cache_path, default_ttl=cfg.cache_ttl) if cfg.cache_enabled else None
        self.transport = Transport(
            client=client,
            http_transport=http_transport,
            cache=cache,
            rate_limiter=limiter,
            retry=retry,
            timeout=cfg.timeout,
            sleep=sleep,
            rng=rng,
        )
        self.uniprot = UniProtClient(self.transport)
        self.pdb = RCSBClient(self.transport, check_obsolete=cfg.check_obsolete)
        self.ensembl = EnsemblClient(self.transport, check_retired=cfg.check_retired)
        self.ncbi = NCBIClient(self.transport, email=cfg.ncbi_email, api_key=api_key)

    def close(self) -> None:
        """Release the HTTP client and cache."""
        self.transport.close()

    def __enter__(self) -> BioDB:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
