"""Experiment configs: which strategies run, with which params, brokers and limits.

clock_ms = 1000

[limits]                     # account-wide
max_open_risk = 500

[brokers.sim]
kind = "sim"
latency_ms = 50

[[instances]]
id = "fav_paper"
strategy = "example_favourite"
broker = "sim"               # optional when there is one broker
[instances.params]
min_conf = 0.9
[instances.limits]
max_order_qty = 10
"""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from quanty.brokers.sim import SimBroker
from quanty.core.broker import Broker
from quanty.core.engine import Instance
from quanty.core.fees import FeeModel
from quanty.core.risk import Limits
from quanty.core.strategy import Strategy


@dataclass(frozen=True)
class InstanceSpec:
    id: str
    strategy: str
    broker: str
    params: Mapping[str, object] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)


@dataclass(frozen=True)
class Experiment:
    instances: list[InstanceSpec]
    brokers: Mapping[str, Mapping[str, object]]
    limits: Limits = field(default_factory=Limits)
    clock_ms: int | None = 1000


def load(path: Path) -> Experiment:
    return parse(tomllib.loads(path.read_text()), source=str(path))


def parse(raw: Mapping[str, Any], source: str = "experiment") -> Experiment:
    brokers: dict[str, Mapping[str, object]] = dict(raw.get("brokers") or {"sim": {"kind": "sim"}})
    specs = []
    for i, inst in enumerate(raw.get("instances", [])):
        where = f"{source}: instances[{i}]"
        unknown = set(inst) - {"id", "strategy", "broker", "params", "limits"}
        if unknown:
            raise ValueError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
        for key in ("id", "strategy"):
            if not isinstance(inst.get(key), str):
                raise ValueError(f"{where}: '{key}' is required")
        broker = inst.get("broker") or (next(iter(brokers)) if len(brokers) == 1 else None)
        if broker not in brokers:
            raise ValueError(f"{where}: broker {broker!r} is not defined under [brokers]")
        specs.append(
            InstanceSpec(inst["id"], inst["strategy"], broker, inst.get("params", {}),
                         Limits.from_dict(inst.get("limits", {})))
        )  # fmt: skip
    if not specs:
        raise ValueError(f"{source}: no [[instances]]")
    return Experiment(specs, brokers, Limits.from_dict(raw.get("limits", {})), raw.get("clock_ms", 1000))


def strategy_class(module: ModuleType) -> type[Strategy[Any]]:
    """The one Strategy subclass a strategy module defines."""
    found = [
        v for v in vars(module).values()
        if isinstance(v, type) and issubclass(v, Strategy) and v.__module__ == module.__name__
    ]  # fmt: skip
    if len(found) != 1:
        raise ValueError(f"{module.__name__} must define exactly one Strategy subclass, found {len(found)}")
    return found[0]


def make_broker(name: str, cfg: Mapping[str, object], fees: Mapping[str, FeeModel]) -> Broker:
    kind = cfg.get("kind", "sim")
    opts = {k: v for k, v in cfg.items() if k != "kind"}
    if kind == "sim":
        latency = opts.pop("latency_ms", 0)
        if opts:
            raise ValueError(f"broker {name}: unknown option(s) {', '.join(sorted(opts))}")
        if not isinstance(latency, int):
            raise ValueError(f"broker {name}: latency_ms must be a whole number")
        return SimBroker(name, latency_ms=latency, fees=fees)
    raise ValueError(f"broker {name}: unknown kind {kind!r}")


def build(
    exp: Experiment, strategies: Mapping[str, ModuleType], fees: Mapping[str, FeeModel] | None = None
) -> list[Instance]:
    brokers = {name: make_broker(name, cfg, fees or {}) for name, cfg in exp.brokers.items()}
    out = []
    for spec in exp.instances:
        if spec.strategy not in strategies:
            raise ValueError(f"instance {spec.id}: no strategy named {spec.strategy!r}")
        cls = strategy_class(strategies[spec.strategy])
        params = cls.params_type.from_dict(spec.params)
        out.append(Instance(spec.id, cls(params), brokers[spec.broker], spec.limits))
    return out
