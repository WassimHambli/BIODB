"""UniProt models."""

from __future__ import annotations

from pydantic import Field

from ._base import BioModel


class _Value(BioModel):
    value: str


class _Name(BioModel):
    full_name: _Value | None = Field(default=None, alias="fullName")


class _ProteinDescription(BioModel):
    recommended_name: _Name | None = Field(default=None, alias="recommendedName")
    submission_names: list[_Name] = Field(default_factory=list, alias="submissionNames")


class _Gene(BioModel):
    gene_name: _Value | None = Field(default=None, alias="geneName")


class _Organism(BioModel):
    scientific_name: str | None = Field(default=None, alias="scientificName")
    taxon_id: int | None = Field(default=None, alias="taxonId")


class _Sequence(BioModel):
    value: str
    length: int


class _InactiveReason(BioModel):
    reason_type: str | None = Field(default=None, alias="inactiveReasonType")
    deleted_reason: str | None = Field(default=None, alias="deletedReason")
    merge_demerge_to: list[str] = Field(default_factory=list, alias="mergeDemergeTo")


class UniProtEntry(BioModel):
    """A UniProtKB entry (only the fields biodb exposes are validated; see ``.raw``)."""

    primary_accession: str = Field(alias="primaryAccession")
    uniprot_id: str | None = Field(default=None, alias="uniProtkbId")
    entry_type: str | None = Field(default=None, alias="entryType")
    organism: _Organism | None = None
    protein_description: _ProteinDescription | None = Field(
        default=None, alias="proteinDescription"
    )
    genes: list[_Gene] = Field(default_factory=list)
    sequence: _Sequence | None = None
    inactive_reason: _InactiveReason | None = Field(default=None, alias="inactiveReason")
    #: The accession the caller asked for (set by the client, not part of the UniProt payload).
    requested_accession: str | None = None

    @property
    def redirected(self) -> bool:
        """True if the returned primary accession differs from the requested one: a merged
        accession was redirected to its surviving entry, or ``-1`` selected the canonical
        isoform's entry."""
        return self.requested_accession is not None and self.requested_accession != self.accession

    @property
    def is_active(self) -> bool:
        """False for a deleted or demerged accession (UniProt answers HTTP 200 for these, with
        ``entryType`` "Inactive" and an ``inactiveReason``)."""
        return self.inactive_reason is None and self.entry_type != "Inactive"

    @property
    def accession(self) -> str:
        """Primary accession, e.g. ``P69905``."""
        return self.primary_accession

    @property
    def entry_name(self) -> str | None:
        """Entry name / ID, e.g. ``HBA_HUMAN``."""
        return self.uniprot_id

    @property
    def reviewed(self) -> bool | None:
        """True for Swiss-Prot, False for TrEMBL, None if the entry type is unknown/other."""
        if self.entry_type is None:
            return None
        t = self.entry_type.lower()
        if "unreviewed" in t:
            return False
        if "reviewed" in t:
            return True
        return None

    @property
    def protein_name(self) -> str | None:
        """Recommended full name, falling back to the first submitted name (TrEMBL)."""
        d = self.protein_description
        if d is None:
            return None
        names = [d.recommended_name, *d.submission_names]
        for n in names:
            if n is not None and n.full_name is not None:
                return n.full_name.value
        return None

    @property
    def gene_names(self) -> list[str]:
        """Primary gene names."""
        return [g.gene_name.value for g in self.genes if g.gene_name is not None]

    @property
    def organism_name(self) -> str | None:
        """Scientific name of the source organism."""
        return self.organism.scientific_name if self.organism else None

    @property
    def taxon_id(self) -> int | None:
        """NCBI taxonomy ID of the source organism."""
        return self.organism.taxon_id if self.organism else None

    @property
    def sequence_str(self) -> str | None:
        """Amino-acid sequence."""
        return self.sequence.value if self.sequence else None

    @property
    def length(self) -> int | None:
        """Sequence length."""
        return self.sequence.length if self.sequence else None
