"""SQLite response cache (stdlib only), safe for use from multiple threads.

The cache is an optimisation and never a source of failures: if the file cannot be opened,
is corrupt or locked, or a read or write fails, one ``WARNING`` is logged on the ``biodb``
logger and the cache silently turns into a pass-through (every lookup misses, writes are
dropped). A damaged file is left in place; it is never deleted or repaired.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DEFAULT_TTL = 7 * 24 * 3600.0
ENV_CACHE_PATH = "BIODB_CACHE_PATH"
#: SQLite busy timeout in seconds (another process holding the file must not stall requests).
BUSY_TIMEOUT = 2.0
NCBI_HOST = "eutils.ncbi.nlm.nih.gov"
#: Parameters that never influence an NCBI response and must not change the cache key. Only
#: applied to the NCBI host: for any other service a parameter called ``email`` may well matter.
_NCBI_IGNORED_PARAMS = frozenset({"api_key", "email", "tool"})
#: Bump when the key derivation changes: old entries then simply stop being found.
KEY_VERSION = "v2"
logger = logging.getLogger("biodb")


def default_cache_path() -> Path:
    """`$BIODB_CACHE_PATH` if set, else `~/.cache/biodb/cache.sqlite`."""
    env = os.environ.get(ENV_CACHE_PATH)
    return Path(env).expanduser() if env else Path.home() / ".cache" / "biodb" / "cache.sqlite"


def _pairs(
    items: Mapping[str, object] | Sequence[tuple[str, object]] | None, ignore: frozenset[str]
) -> list[tuple[str, str]]:
    seq = items.items() if isinstance(items, Mapping) else (items or [])
    return sorted((str(k), str(v)) for k, v in seq if str(k).lower() not in ignore)


def make_key(
    method: str,
    url: str,
    params: Mapping[str, object] | Sequence[tuple[str, object]] | None = None,
    body: bytes | None = None,
    *,
    form: Mapping[str, object] | Sequence[tuple[str, object]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> str:
    """sha256 over a version tag, method, URL, query params, form fields, body and the
    ``Accept`` / ``Content-Type`` headers (each hashed separately, so a param named like a
    form field cannot collide with it). On the NCBI host ``api_key``, ``email`` and ``tool``
    (in the params, the form or the URL query) are ignored."""
    parts = urlsplit(url)
    ignore = _NCBI_IGNORED_PARAMS if (parts.hostname or "").lower() == NCBI_HOST else frozenset()
    if ignore and parts.query:
        kept = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in ignore
        ]
        url = urlunsplit(parts._replace(query=urlencode(kept)))
    lowered = {k.lower(): v for k, v in (headers or {}).items()}
    h = hashlib.sha256()
    for chunk in (
        KEY_VERSION.encode(),
        method.upper().encode(),
        url.encode(),
        json.dumps(_pairs(params, ignore)).encode(),
        json.dumps(_pairs(form, ignore)).encode(),
        body or b"",
        lowered.get("accept", "").encode(),
        lowered.get("content-type", "").encode(),
    ):
        h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()


@dataclass(frozen=True)
class CachedResponse:
    """A stored 2xx response."""

    status: int
    headers: dict[str, str]
    body: bytes
    created_at: float


#: ``AttributeError`` covers a connection closed by another thread between check and use.
_CACHE_ERRORS = (sqlite3.Error, OSError, AttributeError)


class Cache:
    """SQLite-backed cache. One connection guarded by a lock (threads share it).

    Failures to open, read or write are logged once as a WARNING and the cache then behaves as
    if empty; see the module docstring. ``enabled`` is ``False`` after the connection could
    not be opened.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        default_ttl: float = DEFAULT_TTL,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.default_ttl = default_ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._warned = False
        self._conn: Any = None
        self.path: Path | None = None
        try:
            if str(path) == ":memory:":
                target = ":memory:"
            else:
                self.path = Path(path).expanduser() if path is not None else default_cache_path()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                target = str(self.path)
            conn = sqlite3.connect(target, check_same_thread=False, timeout=BUSY_TIMEOUT)
            self._conn = conn
            conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT * 1000)}")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS responses ("
                "key TEXT PRIMARY KEY, status INTEGER NOT NULL, headers TEXT NOT NULL, "
                "body BLOB NOT NULL, created_at REAL NOT NULL)"
            )
            conn.commit()
        except (*_CACHE_ERRORS, RuntimeError, ValueError) as exc:  # e.g. no home directory, NUL
            self._fail("open", exc)
            self._close_quietly()

    @property
    def enabled(self) -> bool:
        """False if the cache could not be opened (it then never stores or returns anything)."""
        return self._conn is not None

    def _fail(self, op: str, exc: BaseException) -> None:
        if not self._warned:
            self._warned = True
            logger.warning(
                "biodb cache %s failed (%s: %s); continuing without the cache",
                op,
                type(exc).__name__,
                exc,
            )

    def _rollback(self) -> None:
        """A failed write must not leave a transaction open: it would lock out other users."""
        with self._lock, contextlib.suppress(*_CACHE_ERRORS):
            self._conn.rollback()

    def _close_quietly(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(sqlite3.Error, OSError):
                conn.close()

    def get(self, key: str, ttl: float | None = None) -> CachedResponse | None:
        """Return the entry if present and younger than ``ttl`` (default: the cache TTL).

        A ``ttl`` of zero or less is always a miss, and so is an entry stamped in the future
        (the clock moved backwards)."""
        limit = self.default_ttl if ttl is None else ttl
        if self._conn is None or limit <= 0:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT status, headers, body, created_at FROM responses WHERE key = ?",
                    (key,),
                ).fetchone()
        except _CACHE_ERRORS as exc:
            self._fail("read", exc)
            return None
        if row is None:
            return None
        status, headers, body, created_at = row
        try:
            age = self._clock() - float(created_at)
            if age < 0 or age > limit:
                return None
            parsed = json.loads(headers)
            if not isinstance(parsed, dict):
                return None
            return CachedResponse(int(status), parsed, bytes(body), float(created_at))
        except (ValueError, TypeError):
            return None

    def put(self, key: str, status: int, headers: Mapping[str, str], body: bytes) -> None:
        """Store a response. Callers must only store 2xx responses."""
        if not 200 <= status < 300:
            raise ValueError("only 2xx responses may be cached")
        if self._conn is None:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO responses (key, status, headers, body, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (key, status, json.dumps(dict(headers)), body, self._clock()),
                )
                self._conn.commit()
        except _CACHE_ERRORS as exc:
            self._rollback()
            self._fail("write", exc)

    def delete(self, key: str) -> None:
        """Remove one entry (used to discard a cached body that failed validation)."""
        if self._conn is None:
            return
        try:
            with self._lock:
                self._conn.execute("DELETE FROM responses WHERE key = ?", (key,))
                self._conn.commit()
        except _CACHE_ERRORS as exc:
            self._rollback()
            self._fail("delete", exc)

    def purge_expired(self, ttl: float | None = None) -> int:
        """Delete entries older than `ttl`; returns the number removed."""
        limit = self.default_ttl if ttl is None else ttl
        if self._conn is None:
            return 0
        try:
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM responses WHERE created_at < ?", (self._clock() - limit,)
                )
                self._conn.commit()
                return int(cur.rowcount)
        except _CACHE_ERRORS as exc:
            self._rollback()
            self._fail("purge", exc)
            return 0

    def clear(self) -> None:
        """Remove every entry."""
        if self._conn is None:
            return
        try:
            with self._lock:
                self._conn.execute("DELETE FROM responses")
                self._conn.commit()
        except _CACHE_ERRORS as exc:
            self._rollback()
            self._fail("clear", exc)

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            self._close_quietly()
