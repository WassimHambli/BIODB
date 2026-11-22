"""NCBI E-utilities client."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import unquote_plus

from .._redact import register_secret
from ..errors import (
    BadRequestError,
    InvalidParameterError,
    NotFoundError,
    RateLimitError,
    ResponseValidationError,
    excerpt,
)
from ..ids import validate_ncbi_db, validate_ncbi_id
from ..models.ncbi import ESearchResult, NCBISummary
from ..transport import Params, Response, Transport
from ._base import BaseClient, R, reject_html, require_fasta

ENV_EMAIL = "NCBI_EMAIL"
ENV_API_KEY = "NCBI_API_KEY"
TOOL_NAME = "biodb"
#: esummary/efetch send at most this many ids per request (NCBI recommends POST above ~200 ids;
#: observed 2026-11-14: a GET with 1500 ids (10.5 kB URL) gets HTTP 414, and JSON esummary
#: refuses more than 500 UIDs per request). Longer lists are split into batches of this size.
POST_ID_THRESHOLD = 200
CHUNK_SIZE = POST_ID_THRESHOLD

logger = logging.getLogger("biodb")

_ALL_MODES = frozenset({"text", "xml", "asn.1"})
#: efetch ``rettype`` allow-list per database, from the NCBI E-utilities "Valid values of
#: &retmode and &rettype for efetch" table (the empty string is the database default). Databases
#: not listed here are not validated. ``retmode`` is checked against ``_ALL_MODES``.
EFETCH_RETTYPES: dict[str, frozenset[str]] = {
    "gene": frozenset({"", "gene_table"}),
    "protein": frozenset(
        {"", "native", "fasta", "gp", "gpc", "ipg", "ft", "acc", "seqid", "fasta_cds_na"}
    ),
    "nuccore": frozenset(
        {
            "",
            "native",
            "fasta",
            "gb",
            "gbwithparts",
            "gbc",
            "ft",
            "acc",
            "seqid",
            "fasta_cds_na",
            "fasta_cds_aa",
        }
    ),
    "pubmed": frozenset({"", "medline", "uilist", "abstract"}),
}
EFETCH_RETTYPES["nucleotide"] = EFETCH_RETTYPES["nuccore"]

#: An ``<ERROR>`` element near the start of a body is NCBI's way of reporting a failed efetch
#: with HTTP 200 (the element can follow an XML declaration / DOCTYPE).
_ERROR_ELEMENT_RE = re.compile(r"<ERROR[\s>]", re.IGNORECASE)
_ERROR_START_RE = re.compile(r'(?:\+|%20|\s)*error(?::|%3A)|\{\s*"error"', re.IGNORECASE)
_ERROR_SCAN = 2048
_FASTA_RETTYPES = frozenset({"fasta", "fasta_cds_na", "fasta_cds_aa"})
_SEQUENCE_DBS = frozenset({"nuccore", "nucleotide", "protein"})

_PERCENT_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_SPACED_RE = re.compile(r"(?:\S ){4,}")


def decode_error_body(body: str) -> str:
    """Make a URL-/form-encoded NCBI error body readable.

    Observed (efetch of a bad accession): ``+Error%3A+CEFetchPApplication%3A%3Aproxy_stream()%3A
    +Error%3A+F+a+i+l+e+d++t+o++r+e+t+r+i+e+v+e++s+e+q+u+e+n+c+e+...``. Decoding gives letters
    separated by single spaces and words by double spaces, which is collapsed. Bodies that are
    not encoded are returned unchanged.
    """
    if not _PERCENT_RE.search(body) and "+" not in body[:3]:
        return body
    text = unquote_plus(body).strip()
    head, sep, tail = text.rpartition("Error: ")
    if _SPACED_RE.search(tail):
        tail = tail.replace("  ", "\0").replace(" ", "").replace("\0", " ")
    return " ".join((head + sep + tail).split())


def _as_id_list(ids: str | int | Iterable[str | int]) -> list[str]:
    items: list[str | int] = [ids] if isinstance(ids, str | int) else list(ids)
    if not items:
        raise BadRequestError("no identifiers given", service="ncbi")
    return [validate_ncbi_id(i) for i in items]


def _chunks(ids: list[str]) -> list[list[str]]:
    return [ids[i : i + CHUNK_SIZE] for i in range(0, len(ids), CHUNK_SIZE)]


def _join_texts(texts: list[str]) -> str:
    """Concatenate batch outputs, making sure records of adjacent batches do not run together."""
    out = ""
    for t in texts:
        if out and t and not out.endswith("\n"):
            out += "\n"
        out += t
    return out


def _check_efetch_format(db: str, rettype: str, retmode: str) -> None:
    if retmode not in _ALL_MODES:
        raise InvalidParameterError(
            f"invalid efetch retmode {retmode!r}; expected one of {sorted(_ALL_MODES)}",
            service="ncbi",
        )
    allowed = EFETCH_RETTYPES.get(db)
    if allowed is not None and rettype not in allowed:
        # Warn, don't raise: the allow-list is not exhaustive, and rejecting a valid
        # combination would block real queries. NCBI silently falls back to a default format
        # for unknown rettypes (observed N08), so the warning is the useful signal.
        logger.warning(
            "efetch rettype %r is not in biodb's known list for db=%s (%s); NCBI may silently "
            "return its default format instead",
            rettype,
            db,
            ", ".join(sorted(x or "(default)" for x in allowed)),
        )


class NCBIClient(BaseClient):
    """Client for NCBI E-utilities (esearch, esummary, efetch).

    ``email`` and ``api_key`` default to the ``NCBI_EMAIL`` / ``NCBI_API_KEY`` environment
    variables and are never hardcoded. The key and e-mail are excluded from cache keys, are
    never part of a URL (when either is configured every call is a POST with a form body;
    without credentials calls are GETs, and POSTs above 200 ids) and are redacted from error
    messages, response excerpts, reprs and logs.
    """

    service = "ncbi"
    base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(
        self,
        transport: Transport,
        *,
        email: str | None = None,
        api_key: str | None = None,
    ) -> None:
        super().__init__(transport)
        self.email = email if email is not None else os.environ.get(ENV_EMAIL)
        self.api_key = api_key if api_key is not None else os.environ.get(ENV_API_KEY)
        register_secret(self.email)
        register_secret(self.api_key)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(credentials={'set' if self._has_credentials else 'none'})"

    @property
    def _has_credentials(self) -> bool:
        return bool(self.email or self.api_key)

    def _request(self, method: str, url: str, **kwargs: Any) -> Response:
        try:
            return super()._request(method, url, **kwargs)
        except BadRequestError as exc:
            if exc.body_excerpt and not exc.body_excerpt.lstrip().startswith(("{", "<")):
                decoded = decode_error_body(exc.body_excerpt)
                if decoded != exc.body_excerpt:
                    exc.body_excerpt = decoded
                    exc.message = f"{exc.message}: {decoded}"
            raise

    def _params(self, extra: dict[str, str | int]) -> Params:
        params: dict[str, str | int | None] = {"tool": TOOL_NAME, **extra}
        if self.email:
            params["email"] = self.email
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def _fetch(
        self,
        url: str,
        params: Params,
        batched: bool,
        cache: bool,
        ttl: float | None,
        parse: Callable[[Response], R],
    ) -> R:
        """One request, validated by ``parse`` before it is cached. POST (form body) when
        credentials are configured (they must never appear in a URL) or the id list was split
        in batches; GET otherwise."""
        if batched or self._has_credentials:
            return self._call("POST", url, parse, form=params, cache=cache, ttl=ttl)
        return self._call("GET", url, parse, params=params, cache=cache, ttl=ttl)

    def _check_json(self, resp: Response) -> Any:
        """Parse the body and reject HTTP-200 responses that carry an error in the JSON."""
        data = resp.json()
        if not isinstance(data, dict):
            return data
        message = data.get("error")
        esr = data.get("esearchresult")
        if message is None and isinstance(esr, dict):
            message = esr.get("ERROR")
        if message is None:
            return data
        text = str(message)
        cls = RateLimitError if "rate limit" in text.lower() else BadRequestError
        raise cls(
            f"NCBI reported an error in a 200 response: {text}",
            service=self.service,
            url=resp.url,
            status=resp.status,
            body_excerpt=excerpt(resp.content),
        )

    def esearch(
        self,
        db: str,
        term: str,
        retmax: int = 20,
        *,
        cache: bool = True,
        ttl: float | None = None,
    ) -> ESearchResult:
        """Search a database; returns ``.ids`` (UIDs) and total ``.count``."""
        database = validate_ncbi_db(db)
        if not term.strip():
            raise BadRequestError("empty search term", service=self.service)
        if retmax < 0:
            raise BadRequestError("retmax must be >= 0", service=self.service)
        url = f"{self.base_url}/esearch.fcgi"

        def parse(resp: Response) -> ESearchResult:
            data = self._check_json(resp)
            inner = data.get("esearchresult") if isinstance(data, dict) else None
            return self._parse(ESearchResult, inner, url)

        result = self._fetch(
            url,
            self._params({"db": database, "term": term, "retmax": retmax, "retmode": "json"}),
            False,
            cache,
            ttl,
            parse,
        )
        errors = result.error_list
        if errors.get("phrasesnotfound") or errors.get("fieldsnotfound"):
            logger.warning(
                "NCBI esearch (db=%s, term=%r): %s; query was translated to %r",
                database,
                term,
                "; ".join(result.warnings),
                result.query_translation,
            )
        return result

    def esummary(
        self,
        db: str,
        ids: str | int | Iterable[str | int],
        *,
        cache: bool = True,
        ttl: float | None = None,
    ) -> dict[str, NCBISummary]:
        """Document summaries keyed by UID, in the order given. Raises ``NotFoundError`` for
        unknown UIDs, including numeric UIDs that were requested but are absent from the
        response (all missing UIDs of all batches are listed in one error; such answers are
        never cached).

        More than 200 ids are split into batches of at most 200 (one POST each; NCBI caps JSON
        esummary at 500 UIDs per request) and the results merged.
        """
        database = validate_ncbi_db(db)
        id_list = _as_id_list(ids)
        url = f"{self.base_url}/esummary.fcgi"
        batches = _chunks(id_list)
        out: dict[str, NCBISummary] = {}
        missing: list[str] = []
        for batch in batches:

            def parse(resp: Response, batch: list[str] = batch) -> dict[str, NCBISummary]:
                return self._parse_esummary(resp, batch, url)

            try:
                out.update(
                    self._fetch(
                        url,
                        self._params({"db": database, "id": ",".join(batch), "retmode": "json"}),
                        len(batches) > 1,
                        cache,
                        ttl,
                        parse,
                    )
                )
            except _MissingUIDs as exc:
                out.update(exc.found)
                missing.extend(exc.missing)
        if missing:
            raise NotFoundError(
                f"NCBI has no document summary for UID(s): {', '.join(missing)}",
                service=self.service,
                url=url,
                status=200,
            )
        return out

    def _parse_esummary(
        self, resp: Response, requested: list[str], url: str
    ) -> dict[str, NCBISummary]:
        data = self._check_json(resp)
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict) or not isinstance(result.get("uids"), list):
            raise ResponseValidationError(
                "esummary response lacks result.uids",
                service=self.service,
                url=url,
                body_excerpt=excerpt(resp.content),
            )
        found: dict[str, NCBISummary] = {}
        missing: list[str] = []
        returned: set[str] = set()
        for uid in result["uids"]:
            returned.add(_uid_key(str(uid)))
            doc = result.get(str(uid))
            if isinstance(doc, dict) and "error" in doc:
                missing.append(str(uid))
                continue
            found[str(uid)] = self._parse(NCBISummary, doc, url)
        # numeric UIDs we asked for but that are not in the response at all (accessions are
        # resolved server-side to other UIDs, so they cannot be matched this way)
        missing.extend(u for u in requested if u.isdecimal() and _uid_key(u) not in returned)
        if missing:
            raise _MissingUIDs(
                f"NCBI has no document summary for UID(s): {', '.join(missing)}",
                service=self.service,
                url=url,
                status=resp.status,
                found=found,
                missing=missing,
            )
        return found

    def efetch(
        self,
        db: str,
        ids: str | int | Iterable[str | int],
        rettype: str = "fasta",
        retmode: str = "text",
        *,
        cache: bool = True,
        ttl: float | None = None,
    ) -> str:
        """Fetch records as text (e.g. ``rettype="fasta", retmode="text"``).

        ``retmode`` is checked for every database (``text``, ``xml`` or ``asn.1``, else
        ``InvalidParameterError``). ``rettype`` is only checked against a known list for gene,
        protein, nuccore (and its alias nucleotide) and pubmed, and an unknown value merely
        logs a WARNING: the list is not exhaustive and NCBI silently falls back to a default
        format for an unknown ``rettype``.

        More than 200 ids are fetched in batches of at most 200 (one POST each) and the texts
        concatenated in order, for ``retmode="text"`` only. With ``retmode="xml"`` or
        ``"asn.1"`` the batches could not be merged into one valid document, so more than 200
        ids raise ``InvalidParameterError`` before any request: batch yourself.

        An ``<ERROR>`` element or ``Error:`` line near the start of the body raises
        ``BadRequestError``, a blank body ``NotFoundError``, and an HTML page, or a FASTA request
        whose body does not start with ``>``, ``ResponseValidationError``; none is cached.
        """
        database = validate_ncbi_db(db)
        id_list = _as_id_list(ids)
        _check_efetch_format(database, rettype, retmode)
        if len(id_list) > CHUNK_SIZE and retmode != "text":
            raise InvalidParameterError(
                f"efetch of more than {CHUNK_SIZE} ids with retmode={retmode!r} is not "
                f"supported: batches of structured output cannot be merged into one document. "
                f"Batch yourself ({len(id_list)} ids given) or use retmode='text'",
                service="ncbi",
            )
        url = f"{self.base_url}/efetch.fcgi"
        batches = _chunks(id_list)
        fasta = retmode == "text" and rettype in _FASTA_RETTYPES and database in _SEQUENCE_DBS

        def parse(resp: Response) -> str:
            text = self._parse_efetch(resp)
            if fasta:
                require_fasta(resp, self.service)
            return text

        texts: list[str] = []
        for batch in batches:
            texts.append(
                self._fetch(
                    url,
                    self._params(
                        {
                            "db": database,
                            "id": ",".join(batch),
                            "rettype": rettype,
                            "retmode": retmode,
                        }
                    ),
                    len(batches) > 1,
                    cache,
                    ttl,
                    parse,
                )
            )
        return _join_texts(texts)

    def _parse_efetch(self, resp: Response) -> str:
        reject_html(resp, self.service)
        text = resp.text
        head = text[:_ERROR_SCAN]
        if _ERROR_START_RE.match(head.lstrip("\ufeff")) or _ERROR_ELEMENT_RE.search(head):
            cls = RateLimitError if "rate limit" in head.lower() else BadRequestError
            raise cls(
                "NCBI efetch returned an error body",
                service=self.service,
                url=resp.url,
                status=resp.status,
                body_excerpt=excerpt(resp.content),
            )
        if not text.strip():
            raise NotFoundError(
                "NCBI efetch returned an empty body (no such record, or nothing in this format)",
                service=self.service,
                url=resp.url,
                status=resp.status,
            )
        return text


class _MissingUIDs(NotFoundError):
    """Internal: one esummary batch had absent or errored UIDs. Carries the documents that were
    returned so the caller can merge batches and report every missing UID at once."""

    def __init__(
        self,
        message: str,
        *,
        found: dict[str, NCBISummary],
        missing: list[str],
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.found = found
        self.missing = missing


def _uid_key(uid: str) -> str:
    """Compare UIDs numerically ("0672" == "672")."""
    return uid.lstrip("0") or "0"
