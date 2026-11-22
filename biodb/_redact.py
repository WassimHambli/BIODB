"""Credential redaction for error messages, response excerpts and log records.

Two mechanisms, both applied by :func:`redact`:

* *Pattern based*: the value of an ``api_key`` / ``api-key`` / ``apikey`` / ``email`` field is
  replaced in JSON (``"api-key": "..."``), query-string / form (``api_key=...``) and
  percent-encoded (``api_key%3D...``) styles, case-insensitively. NCBI echoes the caller's IP
  address in an ``"api-key"`` field of its rate-limit errors, which this also hides.
* *Literal*: every secret registered with :func:`register_secret` (the actual key and e-mail of
  an :class:`~biodb.clients.ncbi.NCBIClient`) is removed wherever it occurs, also in its
  URL-quoted forms.
"""

from __future__ import annotations

import logging
import re
import threading
from urllib.parse import quote, quote_plus

REDACTED = "[REDACTED]"
#: Secrets shorter than this are only removed by the pattern rules, never by literal search
#: (a one- or two-character "key" would otherwise mangle unrelated text).
_MIN_LITERAL = 6

_NAMES = r"(?:api[_-]?key|email)"
_JSON_RE = re.compile(rf'(?i)("{_NAMES}"\s*:\s*)"(?:[^"\\]|\\.)*(?:"|$)')
_PAIR_RE = re.compile(rf"(?i)\b({_NAMES}\s*(?:=|%3D)\s*)[^&\s\"'<>,;}}]*")

_lock = threading.Lock()
_secrets: set[str] = set()
_literal_re: re.Pattern[str] | None = None


def register_secret(value: str | None) -> None:
    """Remember ``value`` so that :func:`redact` removes it wherever it appears."""
    global _literal_re
    if not value or len(value) < _MIN_LITERAL:
        return
    with _lock:
        _secrets.add(value)
        forms = set()
        for s in _secrets:
            forms.update({s, quote(s, safe=""), quote_plus(s), quote(s, safe="@")})
        ordered = sorted(forms, key=len, reverse=True)
        _literal_re = re.compile("|".join(re.escape(f) for f in ordered), re.IGNORECASE)


def redact(text: str) -> str:
    """Return ``text`` with credential values replaced by ``[REDACTED]``."""
    if not text:
        return text
    out = _JSON_RE.sub(lambda m: f'{m.group(1)}"{REDACTED}"', text)
    out = _PAIR_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    pattern = _literal_re
    if pattern is not None:
        out = pattern.sub(REDACTED, out)
    return out


class RedactingFilter(logging.Filter):
    """Logging filter that redacts the formatted message of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed format string: leave it to logging to complain
            return True
        record.msg = redact(message)
        record.args = None
        return True


def install_log_filter() -> None:
    """Attach :class:`RedactingFilter` to the ``biodb`` logger (idempotent)."""
    log = logging.getLogger("biodb")
    if not any(isinstance(f, RedactingFilter) for f in log.filters):
        log.addFilter(RedactingFilter())
