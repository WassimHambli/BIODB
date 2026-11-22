"""Typed exception hierarchy for biodb."""

from __future__ import annotations

from typing import Any

from ._redact import redact

_EXCERPT_LEN = 300
_REDACTED_ATTRS = frozenset({"message", "url", "body_excerpt"})


def excerpt(body: bytes | str | None, limit: int = _EXCERPT_LEN) -> str:
    """Return a short single-line excerpt of a response body for error messages.

    Credential values are redacted *before* truncating, so a cut-off ``"api-key":"1.2.`` cannot
    leak a prefix."""
    if body is None:
        return ""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    text = redact(" ".join(text.split()))
    return text if len(text) <= limit else text[:limit] + "..."


class BiodbError(Exception):
    """Base class for all biodb errors.

    Attributes:
        service: Name of the service ("uniprot", "rcsb", "ensembl", "ncbi") or "".
        url: Request URL, or "" if no request was made.
        status: HTTP status code, if a response was received.
        body_excerpt: Short excerpt of the response body, if any.
    """

    def __init__(
        self,
        message: str,
        *,
        service: str = "",
        url: str = "",
        status: int | None = None,
        body_excerpt: str = "",
    ) -> None:
        message = redact(message)
        super().__init__(message)
        self.message = message
        self.service = service
        self.url = url
        self.status = status
        self.body_excerpt = body_excerpt

    def __setattr__(self, name: str, value: Any) -> None:
        # message, url and body_excerpt are assigned after construction too (e.g. when a message
        # is decoded); credentials are removed on every assignment, never only in __init__.
        if name in _REDACTED_ATTRS and isinstance(value, str):
            value = redact(value)
            if name == "message":
                self.args = (value,)
        super().__setattr__(name, value)

    def __str__(self) -> str:
        parts = [self.message]
        if self.service:
            parts.append(f"service={self.service}")
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.url:
            parts.append(f"url={self.url}")
        if self.body_excerpt:
            parts.append(f"body={self.body_excerpt!r}")
        return " | ".join(parts)


class NotFoundError(BiodbError):
    """The requested record does not exist (HTTP 404, or service-specific equivalent)."""


class InactiveEntryError(NotFoundError):
    """The record exists in the service's history but is no longer active (UniProt deleted or
    demerged accession). ``reason`` is the service's reason code (e.g. ``"DELETED"``,
    ``"DEMERGED"``); ``replaced_by`` lists successor accessions, when the service gives any."""

    reason: str = ""
    replaced_by: tuple[str, ...] = ()


class ObsoleteEntryError(NotFoundError):
    """The PDB entry was removed (obsoleted). ``replaced_by`` lists the entry ID(s) RCSB names as
    its replacement, when it gives any."""

    replaced_by: tuple[str, ...] = ()


class RetiredIdentifierError(NotFoundError):
    """The Ensembl stable ID is known to the archive but no longer current. ``latest`` is the
    archive's latest versioned ID (e.g. ``ENSG00000183000.3``), ``release`` the last release it
    appeared in, and ``possible_replacement`` the candidate successor stable IDs, best first."""

    latest: str | None = None
    release: str | None = None
    possible_replacement: tuple[str, ...] = ()


class InvalidIdentifierError(BiodbError):
    """An identifier failed client-side validation; no request was made."""

    #: Which identifier type failed validation, e.g. "PDB ID" (set by ``biodb.ids``).
    kind: str = ""


class InvalidParameterError(BiodbError, ValueError):
    """A request parameter (e.g. an efetch ``rettype``) failed client-side validation; no
    request was made."""


class RateLimitError(BiodbError):
    """HTTP 429 persisted after all retries, or the server asked us to wait longer than the
    retry cap. ``retry_after`` holds the server-requested wait in seconds, when known."""

    retry_after: float | None = None


class ServerError(BiodbError):
    """An HTTP 5xx answer: persisted after all retries, not retryable (e.g. 501), or the server
    asked us to wait longer than the retry cap. ``retry_after`` holds the server-requested wait
    in seconds, when it sent a ``Retry-After`` header."""

    retry_after: float | None = None


class TransportError(BiodbError):
    """Network or timeout failure persisted after all retries were exhausted."""


class ResponseValidationError(BiodbError):
    """The payload was not the expected JSON or failed the pydantic model."""


class BadRequestError(BiodbError):
    """Any other 4xx response (or an error reported inside a 200 body)."""
