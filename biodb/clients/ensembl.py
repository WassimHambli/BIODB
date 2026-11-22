"""Ensembl REST client."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Literal, TypeVar
from urllib.parse import quote

from ..errors import (
    BadRequestError,
    BiodbError,
    InvalidIdentifierError,
    NotFoundError,
    ResponseValidationError,
    RetiredIdentifierError,
)
from ..ids import split_ensembl_version, validate_ensembl_id, validate_species
from ..models.ensembl import EnsemblArchiveRecord, EnsemblFeature, EnsemblSequence, EnsemblXref
from ..transport import Response, Transport
from ._base import BaseClient

R = TypeVar("R")
SequenceType = Literal["genomic", "cds", "cdna", "protein"]
_SEQ_TYPES = ("genomic", "cds", "cdna", "protein")
_JSON = {"Content-Type": "application/json"}
_BAD_SYMBOL_RE = re.compile(r"[/\\?#%\x00-\x1f\x7f]")
_NOT_FOUND_MARKERS = ("not found", "no valid lookup", "can not find", "cannot find")
logger = logging.getLogger("biodb")


def _split_version(eid: str) -> tuple[str, int | None]:
    """``"ENSG00000139618.19"`` -> ``("ENSG00000139618", 19)``. Ensembl's lookup, sequence and
    xrefs endpoints answer "ID not found" for a versioned ID (observed, even for the current
    version), so the suffix is stripped before the request. Only ENS-style and LRG IDs have a
    ``.N`` version; for other IDs (``AT1G01010.1``) the dot is part of the name."""
    return split_ensembl_version(eid)


def _check_version(
    base: str,
    requested: int | None,
    returned_id: str,
    returned: int | None,
    url: str,
    status: int,
) -> None:
    """Never silently return a different version than the one requested.

    The versions are only comparable when the response is about the requested ID: e.g.
    ``/sequence/id/ENST...?type=protein`` answers with the *protein's* ID and version (observed:
    ENST00000380152 -> ENSP00000369497 version 3), so for a different ID the check is skipped
    and logged at INFO."""
    if requested is None:
        return
    if returned_id != base:
        logger.info(
            "Ensembl answered with %s for the request for %s; cannot verify the requested "
            "version .%d against it",
            returned_id,
            base,
            requested,
        )
        return
    if returned is None:
        logger.info(
            "Ensembl returned no version for %s; cannot verify requested .%d", base, requested
        )
        return
    if returned != requested:
        raise NotFoundError(
            f"requested {base}.{requested} but Ensembl only serves the current version "
            f"{base}.{returned}; old versions cannot be retrieved",
            service="ensembl",
            url=url,
            status=status,
        )


def _path(part: str) -> str:
    return quote(part, safe=":()")


class EnsemblClient(BaseClient):
    """Client for https://rest.ensembl.org."""

    service = "ensembl"
    base_url = "https://rest.ensembl.org"

    def __init__(self, transport: Transport, *, check_retired: bool = True) -> None:
        super().__init__(transport)
        self.check_retired = check_retired

    def _get(
        self,
        url: str,
        params: dict[str, str | int] | None,
        cache: bool,
        ttl: float | None,
        parse: Callable[[Response], R],
    ) -> R:
        try:
            return self._call("GET", url, parse, params=params, headers=_JSON, cache=cache, ttl=ttl)
        except BadRequestError as exc:
            # Ensembl reports unknown IDs/symbols as HTTP 400 with {"error": "... not found"}.
            if exc.status == 400 and any(m in exc.body_excerpt.lower() for m in _NOT_FOUND_MARKERS):
                raise NotFoundError(
                    exc.message,
                    service=exc.service,
                    url=exc.url,
                    status=exc.status,
                    body_excerpt=exc.body_excerpt,
                ) from exc
            raise

    def archive_id(
        self, stable_id: str, *, cache: bool = True, ttl: float | None = None
    ) -> EnsemblArchiveRecord:
        """History of a stable ID (``/archive/id``): ``is_current``, ``latest`` (versioned ID),
        ``release`` and ``possible_replacement`` candidates for a retired ID. A version suffix
        is stripped, as for ``lookup_id``. ``NotFoundError`` for an ID the archive never saw."""
        base, _ = _split_version(validate_ensembl_id(stable_id))
        url = f"{self.base_url}/archive/id/{_path(base)}"
        return self._get(
            url, None, cache, ttl, lambda r: self._parse(EnsemblArchiveRecord, r.json(), url)
        )

    def lookup_id(
        self,
        ensembl_id: str,
        expand: bool = False,
        *,
        cache: bool = True,
        ttl: float | None = None,
        check_retired: bool | None = None,
    ) -> EnsemblFeature:
        """Look up a gene/transcript/etc. by stable ID (``expand=True`` adds transcripts).

        A ``.N`` version suffix of an ENS-style or LRG ID is stripped before the request (the
        server rejects it) and recorded in ``requested_version``; if the response is for that
        same ID and its ``version`` differs, ``NotFoundError`` is raised naming both versions,
        so an old version is never silently replaced by the current one. For other IDs
        (``FBgn0000008``, ``AT1G01010.1``) the ID is sent unchanged.

        Ensembl reports retired and unknown IDs identically. With ``check_retired`` (default:
        the client's setting, ``True``) a ``NotFoundError`` triggers one ``/archive/id`` call;
        if the ID is known but not current, :class:`~biodb.errors.RetiredIdentifierError` is
        raised (``.latest``, ``.possible_replacement``), otherwise the original error.
        """
        base, version = _split_version(validate_ensembl_id(ensembl_id))
        url = f"{self.base_url}/lookup/id/{_path(base)}"
        params: dict[str, str | int] | None = {"expand": 1} if expand else None
        try:
            feature = self._get(
                url, params, cache, ttl, lambda r: self._parse(EnsemblFeature, r.json(), url)
            )
        except NotFoundError as exc:
            if self.check_retired if check_retired is None else check_retired:
                self._raise_if_retired(base, exc, cache, ttl)
            raise
        _check_version(base, version, feature.id, feature.version, url, 200)
        feature.requested_version = version
        return feature

    def _raise_if_retired(
        self, base: str, original: NotFoundError, cache: bool, ttl: float | None
    ) -> None:
        try:
            archive = self.archive_id(base, cache=cache, ttl=ttl)
        except BiodbError as exc:  # archive lookup is best effort; keep the original error
            logger.info(
                "Ensembl archive check for %s failed (%s); keeping the original error", base, exc
            )
            return
        if archive.is_current:
            return
        err = RetiredIdentifierError(
            f"Ensembl ID {base} is retired (last in release {archive.release}, latest "
            f"{archive.latest})"
            + (
                "; possible replacements: "
                + ", ".join(r.stable_id for r in archive.possible_replacement)
                if archive.possible_replacement
                else ""
            ),
            service=self.service,
            url=original.url,
            status=original.status,
            body_excerpt=original.body_excerpt,
        )
        err.latest = archive.latest
        err.release = archive.release
        err.possible_replacement = tuple(r.stable_id for r in archive.possible_replacement)
        raise err from original

    def lookup_symbol(
        self, species: str, symbol: str, *, cache: bool = True, ttl: float | None = None
    ) -> EnsemblFeature:
        """Look up a gene by species (e.g. ``homo_sapiens``) and symbol (e.g. ``BRCA2``)."""
        sp = validate_species(species)
        sym = symbol.strip() if isinstance(symbol, str) else ""
        if not sym or sym in (".", "..") or _BAD_SYMBOL_RE.search(sym):
            err = InvalidIdentifierError(f"invalid gene symbol: {symbol!r}", service=self.service)
            err.kind = "gene symbol"
            raise err
        url = f"{self.base_url}/lookup/symbol/{_path(sp)}/{quote(sym, safe='')}"
        return self._get(
            url, None, cache, ttl, lambda r: self._parse(EnsemblFeature, r.json(), url)
        )

    def get_sequence(
        self,
        ensembl_id: str,
        type: SequenceType = "genomic",  # noqa: A002
        *,
        cache: bool = True,
        ttl: float | None = None,
    ) -> EnsemblSequence:
        """Fetch a sequence of ``type`` ``genomic``, ``cds``, ``cdna`` or ``protein``.

        A ``.N`` version suffix is handled as in :meth:`lookup_id`, except that the version is
        compared only when the returned ``id`` equals the requested one: ``type="protein"`` for
        a transcript ID answers with the protein's own ID and version, so there the check is
        skipped (``requested_version`` is kept; an INFO message is logged)."""
        base, version = _split_version(validate_ensembl_id(ensembl_id))
        if type not in _SEQ_TYPES:
            raise InvalidIdentifierError(f"invalid sequence type: {type!r}", service=self.service)
        url = f"{self.base_url}/sequence/id/{_path(base)}"
        seq = self._get(
            url, {"type": type}, cache, ttl, lambda r: self._parse(EnsemblSequence, r.json(), url)
        )
        _check_version(base, version, seq.id, seq.version, url, 200)
        seq.requested_version = version
        return seq

    def get_xrefs(
        self, ensembl_id: str, *, cache: bool = True, ttl: float | None = None
    ) -> list[EnsemblXref]:
        """External cross-references for an Ensembl ID."""
        base, version = _split_version(validate_ensembl_id(ensembl_id))
        if version is not None:
            logger.info(
                "xrefs for %s: version suffix .%d ignored (server rejects it)", base, version
            )
        url = f"{self.base_url}/xrefs/id/{_path(base)}"

        def parse(resp: Response) -> list[EnsemblXref]:
            data = resp.json()
            if not isinstance(data, list):
                raise ResponseValidationError(
                    "xrefs response is not a JSON list", service=self.service, url=url
                )
            return [self._parse(EnsemblXref, item, url) for item in data]

        return self._get(url, None, cache, ttl, parse)
