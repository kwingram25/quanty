import itertools
import math

import pytest
from helpers import Hooks

from quanty import experiment, signals
from quanty.brokers.sim import SimBroker
from quanty.core import Context, Engine, IndexValue, Instance, Quote, Trade
from quanty.signals import Ewma, EwmaConfig, Last, LastConfig, RealizedVol, RealizedVolConfig


def iv(t: int, v: float, key: str = "k") -> IndexValue:
    return IndexValue(ts=t, key=key, value=v)


def test_last_uses_trade_quote_mid_and_index() -> None:
    s = Last(LastConfig(name="x", key="k"))
    s.update(Trade(ts=0, key="k", price=10.0, size=1))
    assert s.value == 10.0
    s.update(Quote(ts=1, key="k", bid=11.0, ask=13.0))
    assert s.value == 12.0
    s.update(Quote(ts=2, key="k", bid=0.0, ask=13.0))  # one-sided: ignored
    assert s.value == 12.0
    s.update(iv(3, 7.0))
    assert s.value == 7.0


def test_ewma_halflife() -> None:
    s = Ewma(EwmaConfig(name="x", key="k", halflife_s=1))
    s.update(iv(0, 0.0))
    s.update(iv(1000, 10.0))  # one half-life: halfway
    assert s.value == pytest.approx(5.0)
    s.update(iv(1000, 10.0))  # no time passed: no change
    assert s.value == pytest.approx(5.0)


def test_realized_vol_matches_hand_computation() -> None:
    s = RealizedVol(RealizedVolConfig(name="v", key="k", window_s=10, step_s=1))
    prices = [100, 101, 99, 100, 102]
    for i, p in enumerate(prices):
        s.update(iv(i * 1000 + 500, p))
    s.update(iv(5500, 102))  # flushes the sample at t=5000
    rets = [math.log(b / a) for a, b in itertools.pairwise(prices)]
    assert s.value == pytest.approx(math.sqrt(sum(r * r for r in rets) / len(rets)))


def test_realized_vol_window_drops_old_returns_and_counts_quiet_seconds() -> None:
    s = RealizedVol(RealizedVolConfig(name="v", key="k", window_s=3, step_s=1))
    s.update(iv(500, 100))
    s.update(iv(1500, 110))  # big move early
    s.update(iv(10_500, 110))  # long quiet stretch: zero returns, old move aged out
    assert s.value == pytest.approx(0.0)


def test_engine_exposes_signals_by_name() -> None:
    seen: list[float | None] = []

    def on_data(ctx: Context, ev: object) -> None:
        seen.append(ctx.signal("px"))

    sig = Last(LastConfig(name="px", key="k"))
    e = Engine([Instance("s", Hooks(data=on_data), SimBroker())], signals=[sig])
    e.run([iv(1, 5.0), iv(2, 6.0, key="other")])
    assert seen == [5.0, 5.0]  # the signal updates before strategies see the event; other keys don't move it
    with pytest.raises(ValueError, match="unique"):
        Engine([], signals=[sig, Last(LastConfig(name="px", key="k2"))])


def test_experiment_signal_config() -> None:
    base = {"instances": [{"id": "a", "strategy": "s"}]}
    exp = experiment.parse(
        {**base, "signals": [{"kind": "realized_vol", "name": "v", "key": "k", "window_s": 60}]}
    )
    (sig,) = experiment.signals(exp)
    assert isinstance(sig, RealizedVol) and sig.window == 60_000
    with pytest.raises(ValueError, match=r"signals\.0"):
        experiment.parse({**base, "signals": [{"kind": "magic", "name": "v", "key": "k"}]})
    with pytest.raises(ValueError, match="halflife_s"):
        experiment.parse({**base, "signals": [{"kind": "ewma", "name": "v", "key": "k"}]})
    assert set(signals.kinds()) == {"last", "ewma", "realized_vol"}
