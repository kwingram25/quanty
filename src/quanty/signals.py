"""Signals: values derived from data events, which strategies read by name (ctx.signal("btc_vol")).

A signal updates only from event timestamps, never wall-clock time, so a replay of recorded
data rebuilds exactly the values seen live. Configure them in an experiment:

    [[signals]]
    kind = "realized_vol"
    name = "btc_vol"
    key = "coinbase:BTC-USD"
    window_s = 300
"""

import math
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Mapping
from functools import cache
from typing import Any, ClassVar, Literal

from pydantic import Field, PositiveFloat

from quanty.core.events import DataEvent, IndexValue, Observation, Quote, Trade
from quanty.core.validate import ConfigModel


def price_of(ev: DataEvent) -> float | None:
    """A single price for an event: trade price, quote mid, index or observed value."""
    match ev:
        case Trade():
            return ev.price
        case Quote():
            return (ev.bid + ev.ask) / 2 if ev.bid > 0 and ev.ask > 0 else None
        case IndexValue() | Observation():
            return ev.value
    return None


class SignalConfig(ConfigModel):
    name: str = Field(min_length=1, description="What strategies call it: ctx.signal(name)")
    key: str = Field(min_length=1, description='Data key it reads, e.g. "coinbase:BTC-USD"')


class Signal(ABC):
    config_type: ClassVar[type[SignalConfig]]

    def __init__(self, config: SignalConfig) -> None:
        self.name = config.name
        self.key = config.key
        self.value: float | None = None

    @abstractmethod
    def update(self, ev: DataEvent) -> None: ...


class LastConfig(SignalConfig):
    kind: Literal["last"] = "last"


class Last(Signal):
    """The latest price."""

    config_type = LastConfig

    def update(self, ev: DataEvent) -> None:
        p = price_of(ev)
        if p is not None:
            self.value = p


class EwmaConfig(SignalConfig):
    kind: Literal["ewma"] = "ewma"
    halflife_s: PositiveFloat = Field(description="Weight of a price halves after this long")


class Ewma(Signal):
    """Exponentially weighted moving average of price, weighted by time between updates."""

    config_type = EwmaConfig

    def __init__(self, config: EwmaConfig) -> None:
        super().__init__(config)
        self.halflife_ms = config.halflife_s * 1000
        self._t: int | None = None

    def update(self, ev: DataEvent) -> None:
        p = price_of(ev)
        if p is None:
            return
        if self.value is None or self._t is None:
            self.value = p
        else:
            alpha = 1 - 0.5 ** (max(0, ev.ts - self._t) / self.halflife_ms)
            self.value += alpha * (p - self.value)
        self._t = ev.ts


class RealizedVolConfig(SignalConfig):
    kind: Literal["realized_vol"] = "realized_vol"
    window_s: PositiveFloat = Field(300, description="Lookback")
    step_s: PositiveFloat = Field(1, description="Sampling interval for returns")


class RealizedVol(Signal):
    """Realized volatility: sd of log returns per second, sampled every step_s over window_s.

    The price at each sample time is the last price before it, so quiet periods count as zero
    returns rather than being skipped.
    """

    config_type = RealizedVolConfig

    def __init__(self, config: RealizedVolConfig) -> None:
        super().__init__(config)
        self.step = int(config.step_s * 1000)
        self.window = int(config.window_s * 1000)
        self._last: float | None = None
        self._next: int | None = None  # next sample time
        self._prev_sample: float | None = None
        self._rets: deque[tuple[int, float]] = deque()
        self._sum_sq = 0.0

    def update(self, ev: DataEvent) -> None:
        p = price_of(ev)
        if p is None or p <= 0:
            return
        if self._next is None:
            self._next = (ev.ts // self.step + 1) * self.step
        while self._last is not None and ev.ts >= self._next:
            self._sample(self._next, self._last)
            self._next += self.step
        self._last = p
        n = len(self._rets)
        self.value = math.sqrt(self._sum_sq / (n * self.step / 1000)) if n >= 2 else None

    def _sample(self, t: int, price: float) -> None:
        if self._prev_sample is not None:
            r = math.log(price / self._prev_sample)
            self._rets.append((t, r))
            self._sum_sq += r * r
        self._prev_sample = price
        while self._rets and self._rets[0][0] <= t - self.window:
            self._sum_sq -= self._rets.popleft()[1] ** 2
        self._sum_sq = max(self._sum_sq, 0.0)


def kind_of(cls: type[Signal]) -> str | None:
    """A signal class's kind: the default of its config's `kind` field."""
    field = cls.config_type.model_fields.get("kind")
    return field.default if field is not None and isinstance(field.default, str) else None


@cache
def kinds() -> dict[str, type[Signal]]:
    """Signal kind -> class: the built-in ones, then extensions' (quanty.extensions)."""
    from quanty import extensions

    built_in = {kind_of(cls) or "": cls for cls in (Last, Ewma, RealizedVol)}
    extra = [(k, cls) for cls in extensions.classes(Signal) if (k := kind_of(cls)) is not None]  # type: ignore[type-abstract]
    return extensions.register("signal kind", built_in, extra)


def config(raw: Mapping[str, Any], where: str = "signals") -> SignalConfig:
    """A [[signals]] table, validated by its kind's config."""
    kind = raw.get("kind")
    cls = kinds().get(kind) if isinstance(kind, str) else None
    if cls is None:
        raise ValueError(f"{where}.kind: unknown signal kind {kind!r} (known: {', '.join(sorted(kinds()))})")
    return cls.config_type.from_config(raw, where)


def make(config: SignalConfig) -> Signal:
    for cls in kinds().values():
        if type(config) is cls.config_type:
            return cls(config)
    raise ValueError(f"no signal kind takes {type(config).__name__}")
