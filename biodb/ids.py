"""Client-side identifier validators. Each returns the normalised identifier or raises
:class:`~biodb.errors.InvalidIdentifierError` before any request is made."""

from __future__ import annotations

import re

from .errors import InvalidIdentifierError

_UNIPROT_RE = re.compile(
    r"(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})(?:-[0-9]+)?"
)
# UniProt accession without isoform suffix, optionally with a ".version" (NCBI protein style).
_UNIPROT_NCBI_RE = re.compile(
    r"(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})(?:\.[0-9]+)?"
)
_PDB_SHORT_RE = re.compile(r"[1-9][A-Z0-9]{3}")
# Extended PDB ID "pdb_0000xxxx": only the form that wraps a legacy 4-character ID exists today.
_PDB_EXTENDED_RE = re.compile(r"PDB_0000([1-9][A-Z0-9]{3})")
# Ensembl stable IDs that carry a real ".version": ENSxxxG00000000000 (any species prefix) and
# LRG records (LRG_1, LRG_1t1, LRG_1p1). Everything else (FBgn..., WBGene..., YAL001C,
# AT1G01010.1 ...) uses '.' as part of the name.
_ENS_STYLE_RE = re.compile(r"ENS[A-Z]*[EGTPR][0-9]{11}(?:\.[0-9]+)?", re.IGNORECASE)
_LRG_STYLE_RE = re.compile(r"LRG_[0-9]+(?:[tp][0-9]+)?(?:\.[0-9]+)?")
_ENS_VERSION_RE = re.compile(r"(?P<base>.+?)\.(?P<ver>[0-9]+)")
# Conservative superset of what rest.ensembl.org serves: no '/', '?', '#', '%', whitespace.
_ENSEMBL_GENERIC_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:()-]{1,63}")
_NCBI_UID_RE = re.compile(r"[0-9]+")
# RefSeq / GenBank style accessions: NM_007294.4, NZ_CP012345.1 (inner prefix), U12345.1
_NCBI_ACC_RE = re.compile(r"(?:[A-Z]{2}_[A-Z]{0,6}[0-9]{1,12}|[A-Z]{1,6}[0-9]{1,10})(?:\.[0-9]+)?")
# PDB chain records in NCBI protein, e.g. 4HHB_A
_NCBI_PDB_CHAIN_RE = re.compile(r"[1-9][A-Z0-9]{3}_[A-Z0-9]{1,4}")
_SPECIES_RE = re.compile(r"[a-z][a-z0-9_]*")

#: Allowed NCBI E-utilities database names: the list returned by EInfo (checked live
#: 2026-11-22; ``geo`` is not an E-utilities database, ``gds`` and ``geoprofiles`` are).
NCBI_DATABASES: frozenset[str] = frozenset(
    {
        "pubmed",
        "protein",
        "nuccore",
        "ipg",
        "nucleotide",
        "structure",
        "genome",
        "gap",
        "grasp",
        "annotinfo",
        "assembly",
        "bioproject",
        "biosample",
        "blastdbinfo",
        "books",
        "cdd",
        "clinvar",
        "dbvar",
        "gene",
        "gds",
        "geoprofiles",
        "medgen",
        "mesh",
        "nlmcatalog",
        "omim",
        "orgtrack",
        "pmc",
        "proteinclusters",
        "pcassay",
        "protfam",
        "pccompound",
        "pcsubstance",
        "seqannot",
        "snp",
        "sra",
        "taxonomy",
        "biocollections",
        "gtr",
    }
)


_SERVICE_BY_KIND = {
    "UniProt accession": "uniprot",
    "PDB ID": "rcsb",
    "Ensembl stable ID": "ensembl",
    "species name": "ensembl",
    "NCBI UID": "ncbi",
    "NCBI identifier": "ncbi",
    "NCBI database": "ncbi",
    "gene symbol": "ensembl",
}


def _fail(kind: str, value: object) -> InvalidIdentifierError:
    err = InvalidIdentifierError(
        f"invalid {kind}: {value!r}", service=_SERVICE_BY_KIND.get(kind, "")
    )
    err.kind = kind
    return err


def validate_uniprot_accession(value: str) -> str:
    """Validate a UniProtKB accession (optionally with an isoform suffix ``-N``)."""
    v = value.strip().upper() if isinstance(value, str) and value.isascii() else ""
    if not _UNIPROT_RE.fullmatch(v):
        raise _fail("UniProt accession", value)
    return v


def validate_pdb_id(value: str) -> str:
    """Validate a PDB ID and return the 4-character upper-case form.

    Accepts the classic ID (a digit 1-9 followed by three letters or digits, any case) and the
    extended form ``pdb_0000xxxx`` (12 characters, any case), which is mapped to the classic
    ID: RCSB serves both spellings of the same entry (checked live 2026-11-22) and answers
    with the classic ID in ``rcsb_id``, so requests, URLs and models all use it.
    """
    v = value.strip().upper() if isinstance(value, str) and value.isascii() else ""
    m = _PDB_EXTENDED_RE.fullmatch(v)
    if m:
        return m.group(1)
    if not _PDB_SHORT_RE.fullmatch(v):
        raise _fail("PDB ID", value)
    return v


def validate_ensembl_id(value: str) -> str:
    """Validate an Ensembl stable ID (any ID that ``rest.ensembl.org`` serves).

    The conservative pattern is ``[A-Za-z0-9][A-Za-z0-9_.:()-]{1,63}``, without ``..`` or a
    trailing dot. Case is kept (``FBgn0000008``, ``YAL001C``, ``MGP_BALBcJ_G0000001``) except
    for ENS-style IDs, which are upper-cased. A ``.N`` suffix is a version only for ENS-style
    and LRG IDs; see :func:`split_ensembl_version`."""
    v = value.strip() if isinstance(value, str) and value.isascii() else ""
    if not _ENSEMBL_GENERIC_RE.fullmatch(v) or ".." in v or v.endswith("."):
        raise _fail("Ensembl stable ID", value)
    if _ENS_STYLE_RE.fullmatch(v):
        v = v.upper()
    return v


def split_ensembl_version(eid: str) -> tuple[str, int | None]:
    """``"ENSG00000139618.19"`` -> ``("ENSG00000139618", 19)``. The ``.N`` suffix is split off
    only for ENS-style and LRG IDs; any other ID (``AT1G01010.1``, ``Y74C9A.1``) is returned
    unchanged with ``None``, because there the dot is part of the name."""
    if _ENS_STYLE_RE.fullmatch(eid) or _LRG_STYLE_RE.fullmatch(eid):
        m = _ENS_VERSION_RE.fullmatch(eid)
        if m:
            return m.group("base"), int(m.group("ver"))
    return eid, None


def validate_ncbi_uid(value: str | int) -> str:
    """Validate a numeric NCBI UID; returns it as a string."""
    v = str(value).strip()
    v = v if v.isascii() else ""
    if not _NCBI_UID_RE.fullmatch(v):
        raise _fail("NCBI UID", value)
    return v


def validate_ncbi_id(value: str | int) -> str:
    """Validate an NCBI record identifier: a numeric UID or a GenBank/RefSeq accession."""
    v = str(value).strip()
    v = v if v.isascii() else ""
    if _NCBI_UID_RE.fullmatch(v):
        return v
    v = v.upper()
    if not (
        _NCBI_ACC_RE.fullmatch(v)
        or _NCBI_PDB_CHAIN_RE.fullmatch(v)
        or _UNIPROT_NCBI_RE.fullmatch(v)
    ):
        raise _fail("NCBI identifier", value)
    return v


def validate_ncbi_db(value: str) -> str:
    """Validate an NCBI database name against the allowlist; returns lower case."""
    v = value.strip().lower() if isinstance(value, str) and value.isascii() else ""
    if v not in NCBI_DATABASES:
        raise _fail("NCBI database", value)
    return v


def validate_species(value: str) -> str:
    """Validate an Ensembl species name such as ``homo_sapiens`` (lower-cased)."""
    v = (
        value.strip().lower().replace(" ", "_")
        if isinstance(value, str) and value.isascii()
        else ""
    )
    if not _SPECIES_RE.fullmatch(v):
        raise _fail("species name", value)
    return v
