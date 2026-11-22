"""RCSB PDB models."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from ._base import BioModel


class _Struct(BioModel):
    title: str | None = None


class _Exptl(BioModel):
    method: str


class _EntryInfo(BioModel):
    resolution_combined: list[float] | None = None


class _AccessionInfo(BioModel):
    deposit_date: datetime | None = None
    initial_release_date: datetime | None = None


class _ContainerIds(BioModel):
    polymer_entity_ids: list[str] = Field(default_factory=list)


class RCSBEntry(BioModel):
    """A PDB entry from ``data.rcsb.org/rest/v1/core/entry``."""

    rcsb_id: str
    struct: _Struct | None = None
    exptl: list[_Exptl] = Field(default_factory=list)
    rcsb_entry_info: _EntryInfo | None = None
    rcsb_accession_info: _AccessionInfo | None = None
    rcsb_entry_container_identifiers: _ContainerIds | None = None

    @property
    def pdb_id(self) -> str:
        """Upper-case PDB ID."""
        return self.rcsb_id

    @property
    def title(self) -> str | None:
        """Structure title."""
        return self.struct.title if self.struct else None

    @property
    def method(self) -> str | None:
        """First experimental method, e.g. ``X-RAY DIFFRACTION``."""
        return self.exptl[0].method if self.exptl else None

    @property
    def resolution(self) -> float | None:
        """Combined resolution in angstroms, if reported (absent for e.g. NMR)."""
        info = self.rcsb_entry_info
        if info and info.resolution_combined:
            return info.resolution_combined[0]
        return None

    @property
    def deposit_date(self) -> datetime | None:
        """Deposition date."""
        return self.rcsb_accession_info.deposit_date if self.rcsb_accession_info else None

    @property
    def release_date(self) -> datetime | None:
        """Initial release date."""
        return self.rcsb_accession_info.initial_release_date if self.rcsb_accession_info else None

    @property
    def polymer_entity_ids(self) -> list[str]:
        """IDs of the polymer entities in this entry."""
        ids = self.rcsb_entry_container_identifiers
        return list(ids.polymer_entity_ids) if ids else []


class _EntityPoly(BioModel):
    type: str | None = None
    pdbx_seq_one_letter_code_can: str | None = None
    rcsb_sample_sequence_length: int | None = None


class _PolymerEntityInfo(BioModel):
    pdbx_description: str | None = None


class _SourceOrganism(BioModel):
    ncbi_scientific_name: str | None = None
    ncbi_taxonomy_id: int | None = None


class _EntityIds(BioModel):
    entity_id: str | None = None
    uniprot_ids: list[str] = Field(default_factory=list)


class RCSBPolymerEntity(BioModel):
    """A polymer entity from ``data.rcsb.org/rest/v1/core/polymer_entity``."""

    rcsb_id: str
    entity_poly: _EntityPoly | None = None
    rcsb_polymer_entity: _PolymerEntityInfo | None = None
    rcsb_entity_source_organism: list[_SourceOrganism] = Field(default_factory=list)
    rcsb_polymer_entity_container_identifiers: _EntityIds | None = None

    @property
    def description(self) -> str | None:
        """Entity description, e.g. ``Hemoglobin subunit alpha``."""
        return self.rcsb_polymer_entity.pdbx_description if self.rcsb_polymer_entity else None

    @property
    def polymer_type(self) -> str | None:
        """E.g. ``polypeptide(L)``."""
        return self.entity_poly.type if self.entity_poly else None

    @property
    def sequence(self) -> str | None:
        """Canonical one-letter sequence."""
        return self.entity_poly.pdbx_seq_one_letter_code_can if self.entity_poly else None

    @property
    def length(self) -> int | None:
        """Sample sequence length."""
        return self.entity_poly.rcsb_sample_sequence_length if self.entity_poly else None

    @property
    def organisms(self) -> list[str]:
        """NCBI scientific names of source organisms."""
        return [
            o.ncbi_scientific_name
            for o in self.rcsb_entity_source_organism
            if o.ncbi_scientific_name
        ]

    @property
    def uniprot_ids(self) -> list[str]:
        """Mapped UniProt accessions (SIFTS)."""
        ids = self.rcsb_polymer_entity_container_identifiers
        return list(ids.uniprot_ids) if ids else []


class _SearchHit(BioModel):
    identifier: str
    score: float | None = None


class RCSBSearchResult(BioModel):
    """Response of the RCSB search API."""

    total_count: int
    result_set: list[_SearchHit] = Field(default_factory=list)


class RCSBHoldingsStatus(BioModel):
    """Release status of a PDB entry from ``/holdings/status``.

    ``status`` is RCSB's value (``CURRENT``, ``REMOVED``, ``UNRELEASED``, ...), ``status_code``
    its short code (e.g. ``OBS`` for removed), and ``replaced_by`` the replacing entry ID(s).
    """

    pdb_id: str
    status: str | None = None
    status_code: str | None = None
    replaced_by: list[str] = Field(default_factory=list)

    @property
    def is_removed(self) -> bool:
        """True if the entry has been removed (obsoleted)."""
        return (self.status or "").upper() == "REMOVED"
