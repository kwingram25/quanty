"""Strict config models (strategy params, risk limits, experiments) on Pydantic.

Config values come from TOML or JSON, so they are validated in JSON mode: strict types (no bools
as numbers, no "1" for 1, no unknown keys), with arrays accepted for tuples and ISO strings for
datetimes.
"""

import json
from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, ValidationError


class ConfigModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    @classmethod
    def from_config(cls, raw: Mapping[str, Any], where: str = "") -> Self:
        """Validate a config mapping; errors are a ValueError naming each bad field."""
        try:
            return cls.model_validate_json(json.dumps(raw, default=str))
        except ValidationError as exc:
            raise ValueError(describe(exc, where or cls.__name__)) from None


def describe(exc: ValidationError, where: str) -> str:
    """One line per error: `where.field.path: message`."""
    lines = []
    for err in exc.errors():
        path = ".".join(str(p) for p in (where, *err["loc"]) if p != "")
        lines.append(f"{path}: {err['msg']}")
    return "\n".join(lines)
