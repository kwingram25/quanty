"""Experiment configs: which strategies run, with which params, brokers and limits.

Example (TOML)::

    clock_ms = 1000

    [limits]                     # account-wide
    max_open_risk = 500

    # every experiment has three brokers, sim, shadow and live; a table only changes settings
    [brokers.sim]
    latency_ms = 50

    [brokers.live]               # real orders need these caps (and a session with --arm)
    max_qty = 5
    max_notional = 5

    [[instances]]
    id = "fav_paper"
    strategy = "example_favorite"
    broker = "sim"               # the default: paper; "shadow" or "live" to start it there
    [instances.params]
    min_conf = 0.9
    [instances.limits]
    max_order_qty = 10
"""

import itertools
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any, Literal, Self

from pydantic import (
    Discriminator,
    Field,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    SerializeAsAny,
    Tag,
    field_validator,
    model_validator,
)

from quanty.brokers.live import LiveBroker
from quanty.brokers.shadow import ShadowBroker
from quanty.brokers.sim import QueueModel, SimBroker
from quanty.core.broker import Broker
from quanty.core.engine import Instance
from quanty.core.fees import FeeModel
from quanty.core.risk import Limits
from quanty.core.strategy import Strategy
from quanty.core.validate import ConfigModel
from quanty.signals import Signal, SignalConfig
from quanty.signals import config as signal_config
from quanty.signals import make as make_signal


class QueueConfig(ConfigModel):
    """How simulated resting orders queue and fill (quanty.brokers.sim.QueueModel); the defaults are
    the realistic model."""

    measure_at: Literal["arrival", "decision"] = Field("arrival", description="When the queue ahead is read")
    credit_cancels: bool = Field(True, description="The queue ahead shrinks when its level shrinks")
    fill_on_cross: bool = Field(True, description="The other side moving onto the order fills it")
    through_fill: Literal["trade_size", "full"] = Field(
        "trade_size", description="A trade through our price fills this much"
    )
    cancel_stops_fills: bool = Field(False, description="No fills once a cancel is sent, vs until it arrives")


class SimBrokerConfig(ConfigModel):
    kind: Literal["sim"] = "sim"
    latency_ms: NonNegativeInt = Field(0, description="Delay before orders and cancels reach the venue")
    arrival: Literal["at_latency", "next_event"] = "at_latency"
    queue: QueueConfig = QueueConfig()


class ShadowBrokerConfig(SimBrokerConfig):
    """Simulated fills, plus a real venue preview of every order (nothing placed)."""

    kind: Literal["shadow"] = "shadow"  # type: ignore[assignment]


class LiveBrokerConfig(ConfigModel):
    """Real orders. Runs only under `quanty live --arm`, and places nothing until both caps are set
    (a fat-finger guard)."""

    kind: Literal["live"] = "live"
    max_qty: PositiveFloat | None = Field(None, description="Largest single order, in contracts")
    max_notional: PositiveFloat | None = Field(None, description="Largest single order, in $ (qty × price)")
    error_limit: PositiveInt = Field(3, description="Consecutive venue rejections that disable the broker")
    poll_s: PositiveFloat = Field(1.0, description="Seconds between reads of resting orders")

    @property
    def capped(self) -> bool:
        return self.max_qty is not None and self.max_notional is not None


def _broker_kind(v: Any) -> str:
    return str(v.get("kind", "sim") if isinstance(v, dict) else getattr(v, "kind", "sim"))


DEFAULT_BROKER = "sim"  # where an instance without `broker =` runs
BUILT_IN_BROKERS = ("sim", "shadow", "live")  # every experiment has these; each is its own kind


# `kind` may be left out (= "sim")
BrokerConfig = Annotated[
    Annotated[SimBrokerConfig, Tag("sim")]
    | Annotated[ShadowBrokerConfig, Tag("shadow")]
    | Annotated[LiveBrokerConfig, Tag("live")],
    Discriminator(_broker_kind),
]


class InstanceSpec(ConfigModel):
    id: str = Field(min_length=1)
    strategy: str = Field(min_length=1)
    broker: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    limits: Limits = Limits()
    # markets to trade in place of the strategy's own series (e.g. synthetic ones, to try it out)
    series: list[str] | None = None
    # this instance is a paper copy of another (`quanty promote` keeps one beside each live
    # instance): the dashboard compares the two
    twin_of: str | None = None


def _default_brokers() -> dict[str, BrokerConfig]:
    return {"sim": SimBrokerConfig(), "shadow": ShadowBrokerConfig(), "live": LiveBrokerConfig()}


class Experiment(ConfigModel):
    clock_ms: PositiveInt | None = 1000
    # paper/live sessions: replay this much of the recent recordings first (orders off), so models
    # and signals that need history (an index's last hours) start warm instead of cold
    # None (the default): what its strategies declare (Strategy.warmup_s), the longest of them
    warmup_s: NonNegativeInt | None = None
    # what the warm-up replays: everything, or "data" only (index values and data feeds; no books),
    # which is all most models need and many times faster; None: "all" if a strategy needs it
    warmup_events: Literal["all", "data"] | None = None
    limits: Limits = Limits()
    brokers: dict[str, BrokerConfig] = Field(default_factory=_default_brokers, min_length=1)
    instances: list[InstanceSpec] = Field(min_length=1)
    signals: list[SerializeAsAny[SignalConfig]] = Field(default_factory=list)  # by kind, extensions' too

    @field_validator("signals", mode="before")
    @classmethod
    def _signal_kinds(cls, raw: Any) -> Any:
        if not isinstance(raw, list):
            return raw
        return [signal_config(s, f"signals.{i}") if isinstance(s, dict) else s for i, s in enumerate(raw)]

    @model_validator(mode="before")
    @classmethod
    def _built_in_brokers(cls, raw: Any) -> Any:
        """sim, shadow and live always exist; [brokers.<name>] tables change their settings (the
        kind follows the name) or add more brokers (kind = "sim" unless given)."""
        if not isinstance(raw, dict):
            return raw
        given = raw.get("brokers") or {}
        if not isinstance(given, dict):
            return raw
        out: dict[str, Any] = {name: {"kind": name} for name in BUILT_IN_BROKERS}
        for name, cfg in given.items():
            if name in BUILT_IN_BROKERS and isinstance(cfg, dict):
                if cfg.get("kind", name) != name:
                    raise ValueError(f"brokers.{name} is the built-in {name} broker; name another one")
                cfg = {**cfg, "kind": name}
            out[name] = cfg
        return {**raw, "brokers": out}

    @model_validator(mode="before")
    @classmethod
    def _expand_grids(cls, raw: Any) -> Any:
        """An [[instances]] entry with a `grid` ({param: [values]}) becomes one instance per
        combination, its params merged in. The id can name them ("fav_{min_conf}_{last_s}"); without
        placeholders the values are appended ("fav" -> "fav_0.995_420")."""
        if not isinstance(raw, dict) or not isinstance(raw.get("instances"), list):
            return raw
        out = []
        for entry in raw["instances"]:
            grid = entry.get("grid") if isinstance(entry, dict) else None
            if not grid:
                out.append(entry)
                continue
            if not isinstance(grid, dict) or not all(isinstance(v, list) and v for v in grid.values()):
                raise ValueError(f"instance {entry.get('id')}: grid is {{param: [values, ...]}}")
            base = {k: v for k, v in entry.items() if k != "grid"}
            names = list(grid)
            for combo in itertools.product(*(grid[n] for n in names)):
                values = dict(zip(names, combo, strict=True))
                template = str(base.get("id", ""))
                if "{" in template:
                    iid = template.format(**values)
                else:
                    iid = "_".join([template, *(f"{v}" for v in combo)])
                out.append({**base, "id": iid, "params": {**base.get("params", {}), **values}})
        return {**raw, "instances": out}

    @model_validator(mode="after")
    def _brokers_resolve(self) -> Self:
        ids = [i.id for i in self.instances]
        if len(set(ids)) != len(ids):
            raise ValueError("instance ids must be unique")
        for inst in self.instances:
            if inst.twin_of is not None and inst.twin_of not in ids:
                raise ValueError(f"instance {inst.id}: twin_of {inst.twin_of!r} is not an instance")
        for inst in self.instances:
            name = self.broker_of(inst)
            if name not in self.brokers:
                raise ValueError(f"instance {inst.id}: broker {inst.broker!r} is not defined under [brokers]")
            cfg = self.brokers[name]
            if isinstance(cfg, LiveBrokerConfig) and not cfg.capped:
                raise ValueError(
                    f"instance {inst.id} is on {name}, which has no caps: set max_qty and max_notional "
                    f"under [brokers.{name}] (the largest single order, contracts and $)"
                )
        return self

    def broker_of(self, inst: InstanceSpec) -> str:
        return inst.broker or DEFAULT_BROKER


def load(path: Path) -> Experiment:
    return parse(tomllib.loads(path.read_text()), source=path.name)


def save_params(path: Path, instance_id: str, values: Mapping[str, Any]) -> None:
    """Write params into an instance's [instances.params] in an experiment file, keeping its comments
    and layout; the file must still load afterwards, or it is left as it was."""
    import tomlkit

    before = path.read_text()
    doc = tomlkit.parse(before)
    entry = next((i for i in doc.get("instances", []) if i.get("id") == instance_id), None)
    if entry is None:
        raise ValueError(f"{path.name} has no [[instances]] entry with id = {instance_id!r}")
    if "params" not in entry:
        entry["params"] = tomlkit.table()
    for k, v in values.items():
        entry["params"][k] = v
    after = tomlkit.dumps(doc)
    try:
        parse(tomllib.loads(after), source=path.name)
    except ValueError as exc:
        raise ValueError(f"not saved: {path.name} wouldn't load afterwards ({exc})") from None
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(after)
    tmp.replace(path)  # atomic: never a half-written experiment


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


def make_broker(
    name: str, cfg: SimBrokerConfig | LiveBrokerConfig, fees: Mapping[str, FeeModel], *, armed: bool = False
) -> Broker:
    if isinstance(cfg, LiveBrokerConfig):
        return LiveBroker(name, armed=armed, max_qty=cfg.max_qty, max_notional=cfg.max_notional,
                          error_limit=cfg.error_limit, poll_s=cfg.poll_s)  # fmt: skip
    queue = QueueModel(**cfg.queue.model_dump())
    if isinstance(cfg, ShadowBrokerConfig):
        return ShadowBroker(name, latency_ms=cfg.latency_ms, arrival=cfg.arrival, fees=fees, queue=queue)
    return SimBroker(name, latency_ms=cfg.latency_ms, arrival=cfg.arrival, fees=fees, queue=queue)


def live_brokers(exp: Experiment) -> list[str]:
    """The live brokers some instance uses (a live broker block no instance uses is just parked)."""
    used = {exp.broker_of(i) for i in exp.instances}
    return [name for name, cfg in exp.brokers.items() if isinstance(cfg, LiveBrokerConfig) and name in used]


def build(
    exp: Experiment,
    strategies: Mapping[str, ModuleType],
    fees: Mapping[str, FeeModel] | None = None,
    *,
    armed: bool = False,
) -> list[Instance]:
    """armed: let live brokers place real orders (only `quanty live --arm` passes True)."""
    return build_session(exp, strategies, fees, armed=armed)[0]


def build_session(
    exp: Experiment,
    strategies: Mapping[str, ModuleType],
    fees: Mapping[str, FeeModel] | None = None,
    *,
    armed: bool = False,
) -> tuple[list[Instance], list[Broker]]:
    """The instances, and the experiment's other brokers (spares an instance can be switched onto
    while running). Live spares only when armed: an unarmed session can't use them."""
    brokers = {name: make_broker(name, cfg, fees or {}, armed=armed) for name, cfg in exp.brokers.items()}
    out = []
    for spec in exp.instances:
        if spec.strategy not in strategies:
            raise ValueError(f"instance {spec.id}: no strategy named {spec.strategy!r}")
        cls = strategy_class(strategies[spec.strategy])
        params = cls.params_type.from_config(spec.params, where=f"{spec.id}.params")
        strategy = cls(params)
        if spec.series is not None:
            strategy.series = tuple(spec.series)
        out.append(Instance(spec.id, strategy, brokers[exp.broker_of(spec)], spec.limits, spec.twin_of))
    used = {i.broker.name for i in out}
    spares = [b for n, b in brokers.items() if n not in used and (armed or b.kind != "live")]
    return out, spares


def parked_live_brokers(exp: Experiment) -> list[str]:
    """Live brokers with caps that no instance uses yet: an armed session can switch onto them."""
    used = set(live_brokers(exp))
    return [
        n for n, c in exp.brokers.items() if isinstance(c, LiveBrokerConfig) and c.capped and n not in used
    ]


def signals(exp: Experiment) -> list[Signal]:
    return [make_signal(cfg) for cfg in exp.signals]


def warmup(exp: Experiment, strategies: Iterable[Strategy[Any]]) -> tuple[int, bool]:
    """(seconds to replay before trading, whether data only): the experiment's settings where it
    makes them, else what its strategies need (the longest; books too if any needs them)."""
    needing = [s for s in strategies if s.warmup_s]
    seconds = exp.warmup_s if exp.warmup_s is not None else max((s.warmup_s for s in needing), default=0)
    events = exp.warmup_events or ("all" if any(s.warmup_events == "all" for s in needing) else "data")
    return seconds, events == "data"
