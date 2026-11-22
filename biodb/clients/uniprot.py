"""UniProt REST client."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Sequence
from urllib.parse import quote, urljoin, urlsplit

from ..errors import (
    BadRequestError,
    InactiveEntryError,
    NotFoundError,
    ResponseValidationError,
)
from ..ids import validate_uniprot_accession
from ..models.uniprot import UniProtEntry
from ..transport import Response
from ._base import BaseClient, reject_html, require_fasta

logger = logging.getLogger("biodb")
_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


class UniProtClient(BaseClient):
    """Client for https://rest.uniprot.org (UniProtKB)."""

    service = "uniprot"
    base_url = "https://rest.uniprot.org"

    def get_entry(
        self, accession: str, *, cache: bool = True, ttl: float | None = None
    ) -> UniProtEntry:
        """Fetch a UniProtKB entry as a validated :class:`UniProtEntry`.

        Raises:
            InvalidIdentifierError: malformed accession (no request is made).
            NotFoundError: accession does not exist.
            InactiveEntryError: accession is deleted or demerged (UniProt returns HTTP 200 with
                an "Inactive" stub for these; a subclass of ``NotFoundError``). Merged accessions
                are redirected by UniProt to the surviving entry, which is returned, so
                ``entry.accession`` may differ from ``accession``; see below.

        The returned entry has ``requested_accession`` set. When the primary accession differs
        from the requested one (merged accession, or ``-1`` selecting the canonical isoform),
        ``entry.redirected`` is true and a message is logged on the ``biodb`` logger at INFO.
        """
        acc = validate_uniprot_accession(accession)
        url = f"{self.base_url}/uniprotkb/{quote(acc, safe='')}.json"

        def parse(resp: Response) -> UniProtEntry:
            entry = self._parse(UniProtEntry, resp.json(), url)
            if not entry.is_active:
                ir = entry.inactive_reason
                reason = (ir.reason_type if ir else None) or "INACTIVE"
                successors = tuple(ir.merge_demerge_to) if ir else ()
                err = InactiveEntryError(
                    f"UniProt accession {acc} is inactive ({reason})"
                    + (f"; see {', '.join(successors)}" if successors else ""),
                    service=self.service,
                    url=url,
                    status=resp.status,
                )
                err.reason = reason
                err.replaced_by = successors
                raise err
            return entry

        entry = self._call("GET", url, parse, cache=cache, ttl=ttl)
        entry.requested_accession = acc
        if entry.redirected:
            logger.info(
                "UniProt accession %s was redirected: returned entry is %s (merged accession or "
                "canonical isoform); see UniProtEntry.redirected",
                acc,
                entry.accession,
            )
        return entry

    def get_fasta(self, accession: str, *, cache: bool = True, ttl: float | None = None) -> str:
        """Fetch the FASTA record for an accession (isoform suffixes allowed)."""
        acc = validate_uniprot_accession(accession)
        url = f"{self.base_url}/uniprotkb/{quote(acc, safe='')}.fasta"

        def parse(resp: Response) -> str:
            reject_html(resp, self.service)
            if not resp.text.strip():
                # UniProt answers HTTP 200 with an empty body for deleted/demerged accessions.
                raise NotFoundError(
                    f"UniProt returned an empty FASTA for {acc} (deleted or demerged accession)",
                    service=self.service,
                    url=url,
                    status=200,
                )
            require_fasta(resp, self.service)
            return resp.text

        return self._call("GET", url, parse, cache=cache, ttl=ttl)

    def search(
        self,
        query: str,
        fields: Sequence[str] | None = None,
        size: int = 25,
        max_results: int | None = None,
        *,
        cache: bool = True,
        ttl: float | None = None,
    ) -> Iterator[UniProtEntry]:
        """Search UniProtKB, following cursor pagination lazily.

        Args:
            query: UniProt query syntax, e.g. ``gene:HBA1 AND organism_id:9606``.
            fields: Optional return fields (``accession``, ``id``, ``gene_names``, ...). With
                restricted fields, unrequested model attributes are ``None``/empty.
            size: Page size (1-500).
            max_results: Stop after this many entries (``None`` = all).
        """
        if not query.strip():
            raise BadRequestError("empty UniProt query", service=self.service)
        if not 1 <= size <= 500:
            raise BadRequestError("size must be between 1 and 500", service=self.service)
        if max_results is not None and max_results < 1:
            raise BadRequestError("max_results must be >= 1", service=self.service)
        return self._search(query, fields, size, max_results, cache, ttl)

    def _search(
        self,
        query: str,
        fields: Sequence[str] | None,
        size: int,
        max_results: int | None,
        cache: bool,
        ttl: float | None,
    ) -> Iterator[UniProtEntry]:
        base_host = (urlsplit(self.base_url).hostname or "").lower()
        url: str | None = f"{self.base_url}/uniprotkb/search"
        first: dict[str, str | int] = {"query": query, "format": "json", "size": size}
        if fields:
            first["fields"] = ",".join(fields)
        params: dict[str, str | int] | None = first
        seen: set[str] = set()
        yielded = 0
        while url is not None:
            page_url = url
            seen.add(page_url)

            def parse(
                resp: Response, page_url: str = page_url
            ) -> tuple[list[UniProtEntry], str | None]:
                payload = resp.json()
                results = payload.get("results") if isinstance(payload, dict) else None
                if not isinstance(results, list):
                    raise ResponseValidationError(
                        "search response lacks a 'results' list",
                        service=self.service,
                        url=page_url,
                    )
                entries = [self._parse(UniProtEntry, item, page_url) for item in results]
                match = _NEXT_RE.search(resp.headers.get("link", ""))
                try:
                    nxt = urljoin(page_url, match.group(1)) if match else None
                    if nxt is not None:
                        self._check_next(nxt, base_host, seen)
                except ValueError as exc:  # unparsable URL
                    raise ResponseValidationError(
                        f"malformed pagination link: {exc}", service=self.service, url=page_url
                    ) from exc
                return entries, nxt

            entries, url = self._call("GET", page_url, parse, params=params, cache=cache, ttl=ttl)
            for entry in entries:
                yield entry
                yielded += 1
                if max_results is not None and yielded >= max_results:
                    return
            params = None

    def _check_next(self, nxt: str, base_host: str, seen: set[str]) -> None:
        """Refuse a ``next`` link that leaves the UniProt host (or the https scheme) or that
        repeats a page already fetched (a server-side pagination loop)."""
        parts = urlsplit(nxt)
        wrong_port = parts.port not in (None, 443)
        if (parts.hostname or "").lower() != base_host or parts.scheme != "https" or wrong_port:
            raise ResponseValidationError(
                f"refusing a pagination link to another host or scheme: {nxt!r}",
                service=self.service,
                url=nxt,
            )
        if nxt in seen:
            raise ResponseValidationError(
                f"pagination loop: the next link repeats a page already fetched: {nxt!r}",
                service=self.service,
                url=nxt,
            )
