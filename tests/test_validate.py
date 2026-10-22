import pytest
from pydantic import Field

from quanty.core import Limits, Params


class P(Params):
    a: float = 1.0
    n: int = Field(1, gt=0)
    flag: bool = False
    name: str = "x"
    cap: float | None = None
    tags: tuple[str, ...] = ()


def test_defaults_and_coercion() -> None:
    p = P.from_config({"a": 2, "cap": 3, "tags": ["u", "v"]})
    assert p == P(a=2.0, cap=3.0, tags=("u", "v"))
    assert isinstance(p.a, float)


@pytest.mark.parametrize(
    ("raw", "field"),
    [
        ({"nope": 1}, "nope"),
        ({"n": 1.5}, "n"),
        ({"n": True}, "n"),
        ({"n": 0}, "n"),  # constraint
        ({"flag": 1}, "flag"),
        ({"name": 3}, "name"),
        ({"a": None}, "a"),
        ({"a": "1"}, "a"),
    ],
)
def test_rejects_bad_values_naming_the_field(raw: dict[str, object], field: str) -> None:
    with pytest.raises(ValueError, match=rf"^inst\.params\.{field}: "):
        P.from_config(raw, where="inst.params")


def test_optional_accepts_none_and_params_are_frozen() -> None:
    p = P.from_config({"cap": None})
    assert p.cap is None
    with pytest.raises(ValueError):
        p.a = 2.0  # type: ignore[misc]


def test_schema_has_descriptions_for_the_ui() -> None:
    class Q(Params):
        x: float = Field(0.5, description="what x does")

    assert Q.model_json_schema()["properties"]["x"] == {
        "default": 0.5, "description": "what x does", "title": "X", "type": "number",
    }  # fmt: skip


def test_limits() -> None:
    assert Limits.from_config({"max_open_risk": 100}).max_open_risk == 100.0
    with pytest.raises(ValueError, match="max_risk"):
        Limits.from_config({"max_risk": 1})
