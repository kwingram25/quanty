import re
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
    return {"instances": [{"id": "a", "strategy": "example_favorite", **kw}]}


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ({}, "exp.instances: Field required"),
        ({"instances": []}, "exp.instances: List should have at least 1 item"),
        ({"instances": [{"strategy": "x"}]}, "exp.instances.0.id: Field required"),
        (inst(color="red"), "exp.instances.0.color: Extra inputs are not permitted"),
        (inst(broker="nope"), "broker 'nope' is not defined"),
        (inst(broker="live"), "on live, which has no caps"),
        ({**inst(), "brokers": {"live": {"kind": "sim"}}}, "built-in live broker"),
        ({"instances": [{"id": "a", "strategy": "s"}, {"id": "a", "strategy": "s"}]}, "unique"),
        ({**inst(), "brokers": {"x": {"kind": "magic"}}}, "exp.brokers.x: Input tag 'magic'"),
        ({**inst(), "brokers": {"sim": {"latency_ms": -1}}}, "exp.brokers.sim.sim.latency_ms"),
        ({**inst(), "clock_ms": 0}, "exp.clock_ms"),
    ],
)
def test_parse_errors(raw: dict[str, object], error: str) -> None:
    with pytest.raises(ValueError, match=re.escape(error)):
        experiment.parse(raw, source="exp")


def test_build_errors(strategies: dict[str, ModuleType]) -> None:
    with pytest.raises(ValueError, match="no strategy named"):
        experiment.build(experiment.parse({"instances": [{"id": "a", "strategy": "nope"}]}), strategies)
    with pytest.raises(ValueError, match=r"a\.params\.min_confidence: Extra inputs"):
        experiment.build(experiment.parse(inst(params={"min_confidence": 1})), strategies)
    with pytest.raises(ValueError, match=r"a\.params\.size: Input should be a valid integer"):
        experiment.build(experiment.parse(inst(params={"size": 2.5})), strategies)


def test_strategy_class_needs_exactly_one(tmp_path: Path) -> None:
    (tmp_path / "none.py").write_text("X = 1\n")
    (tmp_path / "two.py").write_text(
        "from quanty.core import Strategy\nclass A(Strategy):\n    pass\nclass B(Strategy):\n    pass\n"
    )
    found = plugins.discover([tmp_path], include_examples=False, local=None)
    for name in ("none", "two"):
        with pytest.raises(ValueError, match="exactly one"):
            experiment.strategy_class(found[name])


def test_an_instance_can_trade_other_series(strategies: dict[str, ModuleType]) -> None:
    raw = {
        "instances": [
            {"id": "a", "strategy": "example_favorite", "series": ["synthetic:updown_5m"]},
            {"id": "b", "strategy": "example_favorite"},
        ]
    }
    a, b = experiment.build(experiment.parse(raw), strategies)
    assert a.strategy.series == ("synthetic:updown_5m",)
    assert b.strategy.series == type(b.strategy).series and len(b.strategy.series) == 3  # the file's own


def test_grids_expand_into_one_instance_per_combination(strategies: dict[str, ModuleType]) -> None:
    raw = {
        "instances": [
            {
                "id": "fav_{min_conf}_{enter_last_s}",
                "strategy": "example_favorite",
                "params": {"size": 5},
                "grid": {"min_conf": [0.9, 0.95], "enter_last_s": [30, 60]},
            },
            {"id": "plain", "strategy": "example_favorite", "grid": {"min_conf": [0.97]}},
        ]
    }
    exp = experiment.parse(raw)
    assert [i.id for i in exp.instances] == [
        "fav_0.9_30",
        "fav_0.9_60",
        "fav_0.95_30",
        "fav_0.95_60",
        "plain_0.97",
    ]
    assert exp.instances[1].params == {"size": 5, "min_conf": 0.9, "enter_last_s": 60}
    built = experiment.build(exp, strategies)
    assert built[2].strategy.params.min_conf == 0.95 and built[2].strategy.params.size == 5
    with pytest.raises(ValueError, match="grid is"):
        experiment.parse(
            {"instances": [{"id": "x", "strategy": "example_favorite", "grid": {"min_conf": []}}]}
        )


def test_a_broker_queue_model_comes_from_the_file() -> None:
    from quanty.brokers.sim import QueueModel, SimBroker
    from quanty.experiment import make_broker

    exp = experiment.parse(
        {
            **inst(),
            "brokers": {
                "maker": {"latency_ms": 300, "queue": {"measure_at": "decision", "fill_on_cross": False}}
            },
        }
    )
    b = make_broker("maker", exp.brokers["maker"], {})
    assert isinstance(b, SimBroker) and b.queue == QueueModel(measure_at="decision", fill_on_cross=False)
    assert make_broker("sim", exp.brokers["sim"], {}).queue == QueueModel()  # type: ignore[attr-defined]
