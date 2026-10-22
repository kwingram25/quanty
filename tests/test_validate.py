from dataclasses import dataclass

import pytest

from quanty.core import Limits, Params


@dataclass(frozen=True)
class P(Params):
    a: float = 1.0
    n: int = 1
    flag: bool = False
    name: str = "x"
    cap: float | None = None
    tags: tuple[str, ...] = ()


def test_defaults_and_coercion() -> None:
    p = P.from_dict({"a": 2, "cap": 3, "tags": ["u", "v"]})
    assert p == P(a=2.0, cap=3.0, tags=("u", "v"))
    assert isinstance(p.a, float)


@pytest.mark.parametrize(
    "raw",
    [{"nope": 1}, {"n": 1.5}, {"n": True}, {"flag": 1}, {"name": 3}, {"a": None}, {"a": "1"}],
)
def test_rejects_bad_values(raw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        P.from_dict(raw)


def test_optional_accepts_none() -> None:
    assert P.from_dict({"cap": None}).cap is None


def test_limits_from_dict() -> None:
    assert Limits.from_dict({"max_open_risk": 100}).max_open_risk == 100.0
    with pytest.raises(ValueError):
        Limits.from_dict({"max_risk": 1})
