"""Experiment configs: which strategies run, with which params, brokers and limits.

Example (TOML)::

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
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, Self

from pydantic import Field, NonNegativeInt, PositiveInt, model_validator

from quanty.brokers.sim import SimBroker
from quanty.core.broker import Broker
from quanty.core.engine import Instance
from quanty.core.fees import FeeModel
from quanty.core.risk import Limits
from quanty.core.strategy import Strategy
from quanty.core.validate import ConfigModel


class SimBrokerConfig(ConfigModel):
    kind: Literal["sim"] = "sim"
    latency_ms: NonNegativeInt = Field(0, description="Delay before orders and cancels reach the venue")


BrokerConfig = SimBrokerConfig  # becomes a union discriminated on `kind` as broker kinds are added


class InstanceSpec(ConfigModel):
    id: str = Field(min_length=1)
    strategy: str = Field(min_length=1)
    broker: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    limits: Limits = Limits()


class Experiment(ConfigModel):
    clock_ms: PositiveInt | None = 1000
    limits: Limits = Limits()
    brokers: dict[str, BrokerConfig] = Field(default_factory=lambda: {"sim": SimBrokerConfig()}, min_length=1)
    instances: list[InstanceSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _brokers_resolve(self) -> Self:
        ids = [i.id for i in self.instances]
        if len(set(ids)) != len(ids):
            raise ValueError("instance ids must be unique")
        for inst in self.instances:
            name = inst.broker or (next(iter(self.brokers)) if len(self.brokers) == 1 else None)
            if name not in self.brokers:
                raise ValueError(f"instance {inst.id}: broker {inst.broker!r} is not defined under [brokers]")
        return self

    def broker_of(self, inst: InstanceSpec) -> str:
        return inst.broker or next(iter(self.brokers))


def load(path: Path) -> Experiment:
    return parse(tomllib.loads(path.read_text()), source=path.name)


def parse(raw: Mapping[str, Any], source: str = "experiment") -> Experiment:
    return Experiment.from_config(raw, where=source)


def strategy_class(module: ModuleType) -> type[Strategy[Any]]:
    """The one Strategy subclass a strategy module defines."""
    found = [
        v for v in vars(module).values()
        if isinstance(v, type) and issubclass(v, Strategy) and v.__module__ == module.__name__
    ]  # fmt: skip
    if len(found) != 1:
        raise ValueError(f"{module.__name__} must define exactly one Strategy subclass, found {len(found)}")
    return found[0]


def make_broker(name: str, cfg: BrokerConfig, fees: Mapping[str, FeeModel]) -> Broker:
    return SimBroker(name, latency_ms=cfg.latency_ms, fees=fees)


def build(
    exp: Experiment, strategies: Mapping[str, ModuleType], fees: Mapping[str, FeeModel] | None = None
) -> list[Instance]:
    brokers = {name: make_broker(name, cfg, fees or {}) for name, cfg in exp.brokers.items()}
    out = []
    for spec in exp.instances:
        if spec.strategy not in strategies:
            raise ValueError(f"instance {spec.id}: no strategy named {spec.strategy!r}")
        cls = strategy_class(strategies[spec.strategy])
        params = cls.params_type.from_config(spec.params, where=f"{spec.id}.params")
        out.append(Instance(spec.id, cls(params), brokers[exp.broker_of(spec)], spec.limits))
    return out
