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
    strategy = "example_favorite"
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
from typing import Annotated, Any, Literal, Self

from pydantic import Discriminator, Field, NonNegativeInt, PositiveInt, Tag, model_validator

from quanty.brokers.shadow import ShadowBroker
from quanty.brokers.sim import SimBroker
from quanty.core.broker import Broker
from quanty.core.engine import Instance
from quanty.core.fees import FeeModel
from quanty.core.risk import Limits
from quanty.core.strategy import Strategy
from quanty.core.validate import ConfigModel
from quanty.signals import AnySignalConfig, Signal
from quanty.signals import make as make_signal


class SimBrokerConfig(ConfigModel):
    kind: Literal["sim"] = "sim"
    latency_ms: NonNegativeInt = Field(0, description="Delay before orders and cancels reach the venue")
    arrival: Literal["at_latency", "next_event"] = "at_latency"


class ShadowBrokerConfig(SimBrokerConfig):
    """Simulated fills, plus a real venue preview of every order (nothing placed)."""

    kind: Literal["shadow"] = "shadow"  # type: ignore[assignment]


def _broker_kind(v: Any) -> str:
    return str(v.get("kind", "sim") if isinstance(v, dict) else getattr(v, "kind", "sim"))


# `kind` may be left out (= "sim")
BrokerConfig = Annotated[
    Annotated[SimBrokerConfig, Tag("sim")] | Annotated[ShadowBrokerConfig, Tag("shadow")],
    Discriminator(_broker_kind),
]


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
    signals: list[AnySignalConfig] = Field(default_factory=list)

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


def make_broker(name: str, cfg: SimBrokerConfig, fees: Mapping[str, FeeModel]) -> Broker:
    if isinstance(cfg, ShadowBrokerConfig):
        return ShadowBroker(name, latency_ms=cfg.latency_ms, arrival=cfg.arrival, fees=fees)
    return SimBroker(name, latency_ms=cfg.latency_ms, arrival=cfg.arrival, fees=fees)


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


def signals(exp: Experiment) -> list[Signal]:
    return [make_signal(cfg) for cfg in exp.signals]
