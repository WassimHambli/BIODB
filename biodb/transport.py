"""HTTP transport composing rate limiting, caching and retries around ``httpx.Client``."""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from ._version import __version__
from .cache import Cache, make_key
from .errors import (
    BadRequestError,
    BiodbError,
    NotFoundError,
    RateLimitError,
    ResponseValidationError,
    ServerError,
    TransportError,
    excerpt,
)
from .ratelimit import HostRateLimiter
from .retry import RETRY_STATUSES, RetryPolicy, parse_retry_after

logger = logging.getLogger("biodb")

USER_AGENT = f"biodb-client/{__version__}"
DEFAULT_TIMEOUT = 30.0
#: Response headers kept in the cache (content-type, and link for pagination).
_CACHED_HEADERS = ("content-type", "link")

#: Redirects are followed by hand (never by httpx): GET/HEAD only, at most this many hops.
MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
#: Errors that will not go away by retrying (bad URL, unsupported scheme, redirect loop).
_PERMANENT_HTTPX = (httpx.TooManyRedirects, httpx.UnsupportedProtocol)
_DEFAULT_PORTS = {"http": 80, "https": 443}

Params = Mapping[str, str | int | float | bool | None]


@dataclass
class Response:
    """A successful (2xx) HTTP response, either live or served from the cache."""

    status: int
    headers: dict[str, str]
    content: bytes
    url: str
    service: str = ""
    from_cache: bool = False

    @property
    def text(self) -> str:
        """Body decoded as UTF-8."""
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """Parse the body as JSON, raising ``ResponseValidationError`` if it is not JSON."""
        try:
            return json.loads(self.content)
        except (ValueError, RecursionError):
            # ``from None``: the decoder error keeps the whole body, which may echo credentials.
            raise ResponseValidationError(
                "response is not valid JSON",
                service=self.service,
                url=self.url,
                status=self.status,
                body_excerpt=excerpt(self.content),
            ) from None


class Transport:
    """Shared HTTP layer used by all clients.

    Args:
        client: An already-built ``httpx.Client`` (used as-is; not closed by us).
        http_transport: An ``httpx`` transport (e.g. ``httpx.MockTransport``) used to build the
            client when ``client`` is not given.
        cache: A :class:`~biodb.cache.Cache`, or ``None`` to disable caching globally.
        rate_limiter: Per-host limiter; defaults to ``DEFAULT_RATES``.
        retry: Retry policy.
        timeout: Request timeout in seconds when building the client.
        sleep: Sleep function used for backoff (inject in tests).
        rng: Random source for jitter.
        wall_clock: Returns the current time as a ``datetime`` (for Retry-After dates).
    """

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        http_transport: httpx.BaseTransport | None = None,
        cache: Cache | None = None,
        rate_limiter: HostRateLimiter | None = None,
        retry: RetryPolicy | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        wall_clock: Callable[[], Any] | None = None,
    ) -> None:
        self._owns_client = client is None
        self.client = client or httpx.Client(
            transport=http_transport,
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
        )
        self.cache = cache
        self.rate_limiter = rate_limiter if rate_limiter is not None else HostRateLimiter()
        self.retry = retry or RetryPolicy()
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._wall_clock = wall_clock

    def close(self) -> None:
        """Close the HTTP client (if we created it) and the cache."""
        if self._owns_client:
            self.client.close()
        if self.cache is not None:
            self.cache.close()

    def __enter__(self) -> Transport:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def request(
        self,
        method: str,
        url: str,
        *,
        service: str = "",
        params: Params | None = None,
        json_body: Any = None,
        form: Params | None = None,
        headers: Mapping[str, str] | None = None,
        cache: bool = True,
        ttl: float | None = None,
        check: Callable[[Response], None] | None = None,
    ) -> Response:
        """Perform a request with rate limiting, caching and retries.

        Args:
            method: HTTP method.
            url: Absolute URL (query string may be included already).
            service: Service name recorded on errors.
            params: Query parameters (``None`` values are dropped).
            json_body: Body to send as JSON.
            form: Fields to send as an ``application/x-www-form-urlencoded`` body (``None``
                values are dropped); they take part in the cache key separately from ``params``.
            headers: Extra request headers (``Accept`` and ``Content-Type`` are part of the
                cache key).
            cache: ``False`` bypasses the cache for this call (no read, no write).
            ttl: Per-call TTL override in seconds for cache reads.
            check: The caller's validation of a 2xx response (JSON parse, shape, model, "not an
                error page"...). It runs on every live response **before** it is cached and on
                every cached response before it is returned; it may raise to reject the body.
                A rejected live body is never cached; a rejected cached body is discarded and
                fetched once more. A ``RateLimitError`` raised by ``check`` (an error reported
                inside a 200 body) is retried like an HTTP 429. Every biodb client passes a
                ``check``; without one the body is cached as received.

        Redirects (301/302/303/307/308) are followed by hand: only for GET and HEAD, at most
        :data:`MAX_REDIRECTS`, only to the same host (or the https upgrade of the same host),
        and each hop is rate limited. Anything else raises ``TransportError`` and nothing
        further is sent.

        Raises:
            NotFoundError, BadRequestError, RateLimitError, ServerError, TransportError.
        """
        method = method.upper()
        try:
            scheme = urlsplit(url).scheme
        except ValueError:  # e.g. an unbalanced "[" in the host
            scheme = ""
        if scheme not in ("http", "https"):
            raise TransportError(
                f"unsupported URL (only http and https are supported): {url!r}",
                service=service,
                url=url,
            )
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        body = (
            json.dumps(json_body, sort_keys=True, separators=(",", ":")).encode()
            if json_body is not None
            else None
        )
        form_clean = {k: v for k, v in (form or {}).items() if v is not None}
        req_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        use_cache = cache and self.cache is not None
        key = (
            make_key(method, url, clean, body, form=form_clean, headers=req_headers)
            if use_cache
            else ""
        )
        if use_cache and self.cache is not None:
            hit = self.cache.get(key, ttl)
            if hit is not None:
                cached = Response(
                    hit.status, hit.headers, hit.body, url, service=service, from_cache=True
                )
                try:
                    if check is not None:
                        check(cached)
                except BiodbError as exc:
                    logger.warning(
                        "discarding invalid cached response for %s %s (%s); refetching",
                        method,
                        url,
                        type(exc).__name__,
                    )
                    self.cache.delete(key)
                else:
                    logger.debug("cache hit %s %s", method, url)
                    return cached

        host = (urlsplit(url).hostname or "").lower()
        for attempt in range(1, self.retry.max_attempts + 1):
            self.rate_limiter.acquire(host)
            retry_after: float | None = None
            logger.debug("request %s %s (attempt %d)", method, url, attempt)
            failure: BiodbError
            try:
                raw = self._send(
                    method,
                    url,
                    service=service,
                    params=clean,
                    content=body,
                    data=form_clean,
                    headers=req_headers,
                )
            except _PERMANENT_HTTPX + (httpx.InvalidURL,) as exc:
                raise TransportError(
                    f"{type(exc).__name__}: {exc}", service=service, url=url
                ) from _scrub(exc)
            except (httpx.TransportError, httpx.DecodingError) as exc:
                reason = f"{type(exc).__name__}: {exc}"
                failure = TransportError(
                    f"network error after {attempt} attempts ({reason})",
                    service=service,
                    url=url,
                )
                failure.__cause__ = _scrub(exc)
            except httpx.RequestError as exc:
                raise TransportError(
                    f"{type(exc).__name__}: {exc}", service=service, url=url
                ) from _scrub(exc)
            else:
                status = raw.status_code
                if 200 <= status < 300:
                    resp = Response(
                        status,
                        {k.lower(): v for k, v in raw.headers.items()},
                        raw.content,
                        url,
                        service=service,
                    )
                    try:
                        if check is not None:
                            check(resp)
                    except RateLimitError as exc:
                        failure = exc
                        retry_after = exc.retry_after
                        reason = "rate limit reported in a 200 response"
                    else:
                        if use_cache and self.cache is not None:
                            kept = {
                                h: resp.headers[h] for h in _CACHED_HEADERS if h in resp.headers
                            }
                            self.cache.put(key, status, kept, resp.content)
                        return resp
                else:
                    now = self._wall_clock() if self._wall_clock else None
                    retry_after = parse_retry_after(raw.headers.get("retry-after"), now)
                    if status not in RETRY_STATUSES:
                        raise self._http_error(
                            status, f"HTTP {status}", url, service, raw.content, retry_after
                        )
                    failure = self._http_error(
                        status,
                        f"HTTP {status} after {attempt} attempts",
                        url,
                        service,
                        raw.content,
                        retry_after,
                    )
                    if retry_after is not None and retry_after > self.retry.cap:
                        # Retrying earlier than the server asked only burns attempts and risks
                        # a longer block; surface the requested wait to the caller instead.
                        raise self._http_error(
                            status,
                            f"HTTP {status}; server asked to retry after {retry_after:.0f}s "
                            f"(> cap {self.retry.cap:.0f}s)",
                            url,
                            service,
                            raw.content,
                            retry_after,
                        )
                    reason = f"HTTP {status}"
            if attempt >= self.retry.max_attempts:
                raise failure
            delay = self.retry.delay(attempt, retry_after, self._rng)
            logger.warning(
                "%s on %s; retrying in %.2fs (attempt %d/%d)",
                reason,
                url,
                delay,
                attempt,
                self.retry.max_attempts,
            )
            self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _send(
        self,
        method: str,
        url: str,
        *,
        service: str,
        params: Mapping[str, Any],
        content: bytes | None,
        data: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> httpx.Response:
        """One logical request: sends it and follows redirects by hand (see ``request``)."""
        current = url
        first = True
        hops = 0
        while True:
            raw = self.client.request(
                method,
                current,
                params=(params or None) if first else None,
                content=content if first else None,
                data=(data or None) if first else None,
                headers=headers,
                follow_redirects=False,
            )
            if raw.status_code not in _REDIRECT_STATUSES:
                return raw
            location = raw.headers.get("location")
            if not location:
                raise TransportError(
                    f"HTTP {raw.status_code} redirect without a Location header",
                    service=service,
                    url=url,
                    status=raw.status_code,
                )
            if method not in ("GET", "HEAD"):
                raise TransportError(
                    f"refusing to follow an HTTP {raw.status_code} redirect of a {method} "
                    f"request (only GET and HEAD are redirected)",
                    service=service,
                    url=url,
                    status=raw.status_code,
                )
            hops += 1
            if hops > MAX_REDIRECTS:
                raise TransportError(
                    f"too many redirects (more than {MAX_REDIRECTS})",
                    service=service,
                    url=url,
                    status=raw.status_code,
                )
            try:
                target = urljoin(str(raw.request.url), location)
                allowed = _same_origin_or_upgrade(str(raw.request.url), target)
            except ValueError as exc:  # unparsable host or port
                raise TransportError(
                    f"malformed redirect target: {exc}",
                    service=service,
                    url=url,
                    status=raw.status_code,
                ) from exc
            if not allowed:
                raise TransportError(
                    f"refusing to follow a redirect to {target!r}: only the same host (or its "
                    f"https upgrade) is allowed",
                    service=service,
                    url=url,
                    status=raw.status_code,
                )
            logger.debug("redirect %d -> %s", raw.status_code, target)
            current = target
            first = False
            self.rate_limiter.acquire((urlsplit(target).hostname or "").lower())

    @staticmethod
    def _http_error(
        status: int,
        message: str,
        url: str,
        service: str,
        body: bytes,
        retry_after: float | None,
    ) -> BiodbError:
        """Map a non-2xx status to the right exception class (5xx -> ServerError, 429 ->
        RateLimitError; both carry ``retry_after``)."""
        err: BiodbError
        if status == 429:
            rl = RateLimitError(
                message, service=service, url=url, status=status, body_excerpt=excerpt(body)
            )
            rl.retry_after = retry_after
            return rl
        if status >= 500:
            se = ServerError(
                message, service=service, url=url, status=status, body_excerpt=excerpt(body)
            )
            se.retry_after = retry_after
            return se
        cls = NotFoundError if status == 404 else BadRequestError
        err = cls(message, service=service, url=url, status=status, body_excerpt=excerpt(body))
        return err


def _scrub(exc: BaseException) -> BaseException:
    """Redact credentials from an httpx exception that is kept as ``__cause__``."""
    from ._redact import redact

    exc.args = tuple(redact(a) if isinstance(a, str) else a for a in exc.args)
    if isinstance(exc, httpx.RequestError):
        exc._request = None  # its body may hold the NCBI key or e-mail
    return exc


def _effective_port(scheme: str, port: int | None) -> int | None:
    return port if port is not None else _DEFAULT_PORTS.get(scheme)


def _same_origin_or_upgrade(current: str, target: str) -> bool:
    """True if ``target`` is on the same host and port as ``current`` and the scheme is
    unchanged or upgraded http -> https (a downgrade, another host or port is refused)."""
    a, b = urlsplit(current), urlsplit(target)
    if b.scheme not in ("http", "https") or a.scheme not in ("http", "https"):
        return False
    if (a.hostname or "").lower() != (b.hostname or "").lower() or not b.hostname:
        return False
    if a.scheme == b.scheme:
        return _effective_port(a.scheme, a.port) == _effective_port(b.scheme, b.port)
    if a.scheme == "http" and b.scheme == "https":
        return _effective_port("http", a.port) == 80 and _effective_port("https", b.port) == 443
    return False
