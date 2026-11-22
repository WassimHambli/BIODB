"""Ensembl models."""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from ._base import BioModel


class EnsemblFeature(BioModel):
    """Result of ``/lookup`` (gene, transcript, translation, ...)."""

    id: str
    object_type: str | None = None
    species: str | None = None
    display_name: str | None = None
    biotype: str | None = None
    seq_region_name: str | None = None
    start: int | None = None
    end: int | None = None
    strand: int | None = None
    assembly_name: str | None = None
    description: str | None = None
    version: int | None = None
    transcripts: list[dict[str, Any]] = Field(default_factory=list, alias="Transcript")
    #: Version suffix (``.N``) the caller put on the ID; stripped before the request (set by the
    #: client). ``None`` if no version was requested.
    requested_version: int | None = None


class EnsemblSequence(BioModel):
    """Result of ``/sequence/id``."""

    id: str
    molecule: str | None = None
    seq: str
    desc: str | None = None
    query: str | None = None
    version: int | None = None
    #: Version suffix the caller put on the ID (set by the client), or ``None``.
    requested_version: int | None = None

    @property
    def length(self) -> int:
        """Sequence length."""
        return len(self.seq)


class EnsemblXref(BioModel):
    """One cross-reference from ``/xrefs/id``."""

    dbname: str
    primary_id: str
    display_id: str | None = None
    description: str | None = None
    info_type: str | None = None


class EnsemblArchiveReplacement(BioModel):
    """A candidate successor of a retired stable ID (``possible_replacement`` item)."""

    stable_id: str
    score: float | None = None


class EnsemblArchiveRecord(BioModel):
    """Result of ``/archive/id``: the history of a stable ID.

    ``is_current`` is the server's ``"1"``/``""`` string converted to a bool (required: a
    payload without it is a ``ResponseValidationError`` rather than "retired"); ``latest`` is
    the latest versioned ID (e.g. ``ENSG00000139618.19``) and ``release`` the Ensembl release.
    """

    id: str
    is_current: bool
    version: int | None = None
    latest: str | None = None
    release: str | None = None
    assembly: str | None = None
    type: str | None = None
    possible_replacement: list[EnsemblArchiveReplacement] = Field(default_factory=list)

    @field_validator("is_current", mode="before")
    @classmethod
    def _to_bool(cls, v: Any) -> bool:
        if v is None:
            raise ValueError("is_current is missing")
        if isinstance(v, str):
            return v.strip() not in ("", "0")
        return bool(v)

    @field_validator("release", mode="before")
    @classmethod
    def _release_str(cls, v: Any) -> Any:
        return str(v) if isinstance(v, int) else v
