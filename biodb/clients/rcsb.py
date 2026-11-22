"""RCSB PDB client."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

from ..errors import (
    BadRequestError,
    BiodbError,
    InvalidIdentifierError,
    NotFoundError,
    ObsoleteEntryError,
    ResponseValidationError,
    excerpt,
)
from ..ids import validate_pdb_id
from ..models.rcsb import RCSBEntry, RCSBHoldingsStatus, RCSBPolymerEntity, RCSBSearchResult
from ..transport import Response, Transport
from ._base import BaseClient

logger = logging.getLogger("biodb")
_ENTITY_ID_RE = re.compile(r"[0-9]{1,6}")


def _current_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


#: Read once at import (single-threaded) so downloads get the file mode a plain ``open()`` would.
_FILE_MODE = 0o666 & ~_current_umask()


class RCSBClient(BaseClient):
    """Client for RCSB PDB (data, search and file services)."""

    service = "rcsb"
    data_url = "https://data.rcsb.org/rest/v1/core"
    search_url = "https://search.rcsb.org/rcsbsearch/v2/query"
    files_url = "https://files.rcsb.org/download"
    holdings_url = "https://data.rcsb.org/rest/v1/holdings"

    def __init__(self, transport: Transport, *, check_obsolete: bool = True) -> None:
        super().__init__(transport)
        self.check_obsolete = check_obsolete

    def get_entry(
        self,
        pdb_id: str,
        *,
        cache: bool = True,
        ttl: float | None = None,
        check_obsolete: bool | None = None,
    ) -> RCSBEntry:
        """Fetch core metadata for a PDB entry.

        RCSB answers HTTP 404 for both unknown and obsolete (removed) entries. With
        ``check_obsolete`` (default: the client's setting, ``True``) a 404 triggers one extra
        request to the holdings status endpoint; if the entry was removed,
        :class:`~biodb.errors.ObsoleteEntryError` (a ``NotFoundError``) is raised with
        ``.replaced_by``. Otherwise the original ``NotFoundError`` is re-raised.
        """
        pid = validate_pdb_id(pdb_id)
        url = f"{self.data_url}/entry/{quote(pid, safe='')}"
        try:
            return self._call(
                "GET",
                url,
                lambda resp: self._parse(RCSBEntry, resp.json(), url),
                cache=cache,
                ttl=ttl,
            )
        except NotFoundError as exc:
            if exc.status == 404 and (
                self.check_obsolete if check_obsolete is None else check_obsolete
            ):
                self._raise_if_obsolete(pid, exc, cache, ttl)
            raise

    def _raise_if_obsolete(
        self, pid: str, original: NotFoundError, cache: bool, ttl: float | None
    ) -> None:
        try:
            holdings = self.get_holdings_status(pid, cache=cache, ttl=ttl)
        except BiodbError as exc:  # holdings lookup is best effort; keep the original error
            logger.info(
                "RCSB holdings check for %s failed (%s); keeping the original 404", pid, exc
            )
            return
        if not holdings.is_removed:
            return
        err = ObsoleteEntryError(
            f"PDB entry {pid} has been removed (obsolete)"
            + (f"; replaced by {', '.join(holdings.replaced_by)}" if holdings.replaced_by else ""),
            service=self.service,
            url=original.url,
            status=original.status,
            body_excerpt=original.body_excerpt,
        )
        err.replaced_by = tuple(holdings.replaced_by)
        raise err from original

    def get_holdings_status(
        self, pdb_id: str, *, cache: bool = True, ttl: float | None = None
    ) -> RCSBHoldingsStatus:
        """Release status of an entry (``holdings/status``): ``CURRENT``, ``REMOVED``,
        ``UNRELEASED``, ... plus the replacing ID(s) for a removed entry. ``NotFoundError`` if
        RCSB has never heard of the ID."""
        pid = validate_pdb_id(pdb_id)
        url = f"{self.holdings_url}/status/{quote(pid, safe='')}"

        def parse(resp: Response) -> RCSBHoldingsStatus:
            data = resp.json()
            combined = (
                data.get("rcsb_repository_holdings_combined") if isinstance(data, dict) else None
            )
            if not isinstance(data, dict) or not isinstance(combined, dict):
                raise ResponseValidationError(
                    "holdings response lacks rcsb_repository_holdings_combined",
                    service=self.service,
                    url=url,
                )
            replaced = combined.get("id_code_replaced_by_latest")
            flat = {
                "pdb_id": data.get("rcsb_id", pid),
                "status": combined.get("status"),
                "status_code": combined.get("status_code"),
                "replaced_by": [replaced] if isinstance(replaced, str) and replaced else [],
            }
            out = self._parse(RCSBHoldingsStatus, flat, url)
            out._raw = data
            return out

        return self._call("GET", url, parse, cache=cache, ttl=ttl)

    def get_polymer_entity(
        self, pdb_id: str, entity_id: str | int, *, cache: bool = True, ttl: float | None = None
    ) -> RCSBPolymerEntity:
        """Fetch a polymer entity (e.g. ``get_polymer_entity("4HHB", 1)``)."""
        pid = validate_pdb_id(pdb_id)
        eid = str(entity_id).strip()
        if not _ENTITY_ID_RE.fullmatch(eid):
            raise InvalidIdentifierError(f"invalid entity id: {entity_id!r}", service=self.service)
        url = f"{self.data_url}/polymer_entity/{quote(pid, safe='')}/{eid}"
        return self._call(
            "GET",
            url,
            lambda resp: self._parse(RCSBPolymerEntity, resp.json(), url),
            cache=cache,
            ttl=ttl,
        )

    def search_text(
        self, text: str, max_results: int = 25, *, cache: bool = True, ttl: float | None = None
    ) -> list[str]:
        """Full-text search returning matching PDB IDs (best first); ``[]`` if none."""
        if not text.strip():
            raise BadRequestError("empty search text", service=self.service)
        if not 1 <= max_results <= 10000:
            raise BadRequestError("max_results must be between 1 and 10000", service=self.service)
        body = {
            "query": {
                "type": "terminal",
                "service": "full_text",
                "parameters": {"value": text},
            },
            "return_type": "entry",
            "request_options": {"paginate": {"start": 0, "rows": max_results}},
        }

        def parse(resp: Response) -> list[str]:
            if resp.status == 204:
                return []  # RCSB's legitimate answer for a search without hits
            result = self._parse(RCSBSearchResult, resp.json(), self.search_url)
            return [hit.identifier for hit in result.result_set]

        return self._call(
            "POST",
            self.search_url,
            parse,
            json_body=body,
            headers={"Content-Type": "application/json"},
            cache=cache,
            ttl=ttl,
        )

    def download_mmcif(self, pdb_id: str, dest_path: str | Path, *, overwrite: bool = True) -> Path:
        """Download the mmCIF file for an entry and return its path.

        ``dest_path`` is a file path (``~`` is expanded), or an existing directory, in which
        case the file is ``<dir>/<ID>.cif``. The destination directory must already exist
        (``FileNotFoundError`` otherwise, before any request); with ``overwrite=False`` an
        existing file raises ``FileExistsError`` (also before any request).

        The body is fetched fully into memory and must look like mmCIF (the first line that is
        not blank or a ``#`` comment starts with ``data_``), otherwise
        ``ResponseValidationError`` is raised and nothing is written. It is then written to a
        unique temporary file in the destination directory and moved into place with
        ``os.replace``; the temporary file is always removed. Never cached.
        """
        pid = validate_pdb_id(pdb_id)
        raw = str(dest_path)
        dest = Path(raw).expanduser()
        if dest.is_dir():
            dest = dest / f"{pid}.cif"
        elif raw.endswith(("/", os.sep)):
            raise FileNotFoundError(f"destination directory does not exist: {dest}")
        parent = dest.parent
        if not parent.is_dir():
            raise FileNotFoundError(f"destination directory does not exist: {parent}")
        if not overwrite and dest.exists():
            raise FileExistsError(f"{dest} already exists (overwrite=False)")
        url = f"{self.files_url}/{quote(pid, safe='')}.cif"
        resp = self._request("GET", url, cache=False)
        _check_mmcif(resp, self.service, url)
        fd, tmp_name = tempfile.mkstemp(dir=parent, prefix=f".{dest.name}.", suffix=".part")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(resp.content)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, _FILE_MODE)
            if overwrite:
                os.replace(tmp, dest)
            else:
                try:
                    os.link(tmp, dest)  # atomic "create only if absent"
                except FileExistsError:
                    raise
                except OSError:  # no hard links on this file system: best effort
                    if dest.exists():
                        raise FileExistsError(f"{dest} already exists (overwrite=False)") from None
                    os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        return dest


def _check_mmcif(resp: Response, service: str, url: str) -> None:
    """The first line that is not blank or a ``#`` comment must start a data block."""
    text = resp.content.decode("utf-8", errors="replace")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("data_"):
            return
        break
    raise ResponseValidationError(
        "downloaded file does not look like mmCIF (no leading data_ block)",
        service=service,
        url=url,
        status=resp.status,
        body_excerpt=excerpt(resp.content),
    )
