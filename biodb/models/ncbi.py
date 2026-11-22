"""NCBI E-utilities models."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ._base import BioModel


class ESearchResult(BioModel):
    """The ``esearchresult`` object of an esearch JSON response."""

    count: int
    retmax: int = 0
    retstart: int = 0
    ids: list[str] = Field(default_factory=list, alias="idlist")
    #: How NCBI actually interpreted the query (``querytranslation``). Compare with your term:
    #: NCBI silently repairs unbalanced queries and widens unknown field tags to ``[All Fields]``.
    query_translation: str | None = Field(default=None, alias="querytranslation")
    error_list: dict[str, Any] = Field(default_factory=dict, alias="errorlist")
    warning_list: dict[str, Any] = Field(default_factory=dict, alias="warninglist")

    @property
    def warnings(self) -> list[str]:
        """Non-empty ``errorlist`` / ``warninglist`` entries as ``"<kind>: <item>"`` strings
        (e.g. ``"phrasesnotfound: zzzqqxx9988"``)."""
        out: list[str] = []
        for group in (self.error_list, self.warning_list):
            for kind, items in group.items():
                if isinstance(items, list):
                    out.extend(f"{kind}: {item}" for item in items)
                elif items:
                    out.append(f"{kind}: {items}")
        return out


class NCBISummary(BioModel):
    """One esummary document (fields vary by database; use ``.raw`` / extras)."""

    uid: str
