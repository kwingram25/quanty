"""Build config dataclasses (strategy params, risk limits) from TOML-style mappings, with type checks."""

import dataclasses
import types
import typing
from collections.abc import Mapping
from typing import Any


def build_dataclass[T](cls: type[T], raw: Mapping[str, object]) -> T:
    if not dataclasses.is_dataclass(cls):
        raise TypeError(f"{cls!r} is not a dataclass")
    hints = typing.get_type_hints(cls)
    names = {f.name for f in dataclasses.fields(cls) if f.init}
    unknown = set(raw) - names
    if unknown:
        raise ValueError(f"{cls.__name__}: unknown field(s) {', '.join(sorted(unknown))}")
    values = {k: _coerce(f"{cls.__name__}.{k}", hints[k], v) for k, v in raw.items()}
    return cls(**values)


def _coerce(where: str, hint: Any, value: object) -> object:
    union = typing.get_origin(hint) in (typing.Union, types.UnionType)
    allowed = typing.get_args(hint) if union else (hint,)
    if value is None:
        if type(None) in allowed:
            return None
        raise ValueError(f"{where}: may not be empty")
    for t in allowed:
        if t is bool and isinstance(value, bool):
            return value
        if isinstance(value, bool):
            continue
        if t is int and isinstance(value, int):
            return value
        if t is float and isinstance(value, int | float):
            return float(value)
        if t is str and isinstance(value, str):
            return value
        origin = typing.get_origin(t) or t
        if origin in (tuple, list) and isinstance(value, list | tuple):
            return origin(value)
    raise ValueError(f"{where}: expected {hint}, got {value!r}")
