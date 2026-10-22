from pathlib import Path
from types import ModuleType

import pytest

from quanty import experiment, plugins, synthetic
from quanty.brokers.sim import SimBroker
from quanty.core import Engine, Limits

ROOT = Path(__file__).parents[1]


@pytest.fixture
def strategies() -> dict[str, ModuleType]:
    return plugins.discover([], local=None)


def test_example_experiment_loads_and_runs(strategies: dict[str, ModuleType]) -> None:
    exp = experiment.load(ROOT / "experiments" / "example.toml")
    assert [i.id for i in exp.instances] == ["fav_90", "fav_97"]
    assert exp.limits == Limits(max_open_risk=200.0)
    instances = experiment.build(exp, strategies, synthetic.FEES)
    assert instances[0].broker is instances[1].broker
    assert isinstance(instances[0].broker, SimBroker) and instances[0].broker.latency_ms == 50
    assert instances[0].strategy.params.min_conf == 0.90
    assert instances[0].limits.max_order_qty == 10
    engine = Engine(instances, limits=exp.limits)
    engine.run(synthetic.events(synthetic.Spec(hours=1)))
    assert {r.instance for r in engine.ledger.summary()} <= {"fav_90", "fav_97"}


def inst(**kw: object) -> dict[str, object]:
    return {"instances": [{"id": "a", "strategy": "example_favourite", **kw}]}


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ({}, "no [[instances]]"),
        ({"instances": [{"strategy": "x"}]}, "'id' is required"),
        (inst(colour="red"), "unknown key"),
        (inst(broker="nope"), "not defined"),
        ({**inst(), "brokers": {"a": {}, "b": {}}}, "not defined"),  # ambiguous: must name one
    ],
)
def test_parse_errors(raw: dict[str, object], error: str) -> None:
    with pytest.raises(ValueError, match=error.replace("[", r"\[")):
        experiment.parse(raw)


def test_build_errors(strategies: dict[str, ModuleType]) -> None:
    with pytest.raises(ValueError, match="no strategy named"):
        experiment.build(experiment.parse({"instances": [{"id": "a", "strategy": "nope"}]}), strategies)
    with pytest.raises(ValueError, match="unknown field"):
        experiment.build(experiment.parse(inst(params={"min_confidence": 1})), strategies)
    with pytest.raises(ValueError, match="unknown kind"):
        experiment.build(experiment.parse({**inst(), "brokers": {"sim": {"kind": "magic"}}}), strategies)


def test_strategy_class_needs_exactly_one(tmp_path: Path) -> None:
    (tmp_path / "none.py").write_text("X = 1\n")
    (tmp_path / "two.py").write_text(
        "from quanty.core import Strategy\nclass A(Strategy):\n    pass\nclass B(Strategy):\n    pass\n"
    )
    found = plugins.discover([tmp_path], include_examples=False, local=None)
    for name in ("none", "two"):
        with pytest.raises(ValueError, match="exactly one"):
            experiment.strategy_class(found[name])
