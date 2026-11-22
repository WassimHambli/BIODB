"""Base model: tolerant of unknown fields, keeps the raw JSON."""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, PrivateAttr, ValidationError, model_validator

from ..errors import ResponseValidationError

T = TypeVar("T", bound="BioModel")


class BioModel(BaseModel):
    """Pydantic base for all biodb models (``extra="allow"``: unknown upstream fields are kept)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    _raw: dict[str, Any] = PrivateAttr(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _none_collections_to_defaults(cls, data: Any) -> Any:
        """Services send ``null`` for empty lists/maps; a field that defaults to a list or dict
        (``default_factory``) then gets its default instead of failing validation."""
        if not isinstance(data, dict):
            return data
        cleaned: dict[str, Any] | None = None
        for name, info in cls.model_fields.items():
            if info.default_factory is None:
                continue
            for key in {name, info.alias} - {None}:
                if key in data and data[key] is None:
                    if cleaned is None:
                        cleaned = dict(data)
                    del cleaned[key]  # type: ignore[arg-type]
        return data if cleaned is None else cleaned

    @property
    def raw(self) -> dict[str, Any]:
        """The unmodified JSON object this model was built from."""
        return self._raw


def parse_model(model: type[T], data: Any, *, service: str, url: str) -> T:
    """Validate ``data`` against ``model``, raising ``ResponseValidationError`` on failure."""
    if not isinstance(data, dict):
        raise ResponseValidationError(
            f"expected a JSON object for {model.__name__}, got {type(data).__name__}",
            service=service,
            url=url,
        )
    try:
        obj = model.model_validate(data)
    except ValidationError as exc:  # ``from None``: its text shows input values
        raise ResponseValidationError(
            f"payload does not match {model.__name__}: {exc.error_count()} error(s): "
            f"{exc.errors(include_url=False, include_input=False)[:3]}",
            service=service,
            url=url,
        ) from None
    obj._raw = data
    return obj
