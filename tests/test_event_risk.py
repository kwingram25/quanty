import pytest
from helpers import Hooks

from quanty.brokers.sim import SimBroker
from quanty.core import Engine, Fill, IndexValue, Instance, Market, MarketOpen, Order, Side
from quanty.core.portfolio import Portfolio, Position
from quanty.core.risk import Limits, Risk, event_exposure
from quanty.core.types import Action, Liquidity


def ladder(n: int = 5, exhaustive: bool = True) -> dict[str, Market]:
    return {
        f"t:B{i}": Market(id=f"t:B{i}", venue="t", series="t:temp", open_ts=0, close_ts=3_600_000,
                          terms={"event": "t:TEMP", "exclusive": True, "exhaustive": exhaustive})
        for i in range(n)
    }  # fmt: skip


def pos(qty: float, cost: float) -> Position:
    return Position(qty, cost)


def test_exposure_of_a_full_ladder_is_the_overpayment() -> None:
    held = [(f"t:B{i}", Side.YES, pos(10, 2.04)) for i in range(5)]  # 10 of every bucket, $10.20 in all
    loss, gain = event_exposure(held, list(ladder()), exhaustive=True)
    assert loss == pytest.approx(0.20) and gain == pytest.approx(-0.20)


def test_partial_ladder_risks_its_full_cost() -> None:
    held = [("t:B1", Side.YES, pos(10, 3.0)), ("t:B2", Side.YES, pos(10, 4.0))]
    loss, gain = event_exposure(held, list(ladder()), exhaustive=True)
    assert loss == pytest.approx(7.0) and gain == pytest.approx(3.0)


def test_no_positions_pay_when_their_bucket_loses() -> None:
    held = [("t:B0", Side.NO, pos(10, 9.0)), ("t:B1", Side.NO, pos(10, 9.0))]  # at most one can win
    loss, gain = event_exposure(held, list(ladder()), exhaustive=True)
    assert loss == pytest.approx(8.0) and gain == pytest.approx(2.0)


def test_non_exhaustive_events_include_none_winning() -> None:
    held = [(f"t:B{i}", Side.YES, pos(10, 2.0)) for i in range(5)]
    assert event_exposure(held, list(ladder()), exhaustive=False)[0] == pytest.approx(10.0)


def fill(market: str, side: Side, price: float, qty: float) -> Fill:
    return Fill(ts=1, order_id="x", market_id=market, side=side, action=Action.BUY, price=price, qty=qty,
                fee=0.0, liquidity=Liquidity.TAKER)  # fmt: skip


def order(market: str, price: float, qty: float) -> Order:
    return Order(
        id="new", instance="s", market_id=market, side=Side.YES, action=Action.BUY, price=price, qty=qty
    )


def test_risk_nets_exclusive_events_and_sums_the_rest() -> None:
    markets = {**ladder(), "t:SOLO": Market(id="t:SOLO", venue="t", series="t:x", open_ts=0, close_ts=1)}
    pf = Portfolio()
    for m in ladder():
        pf.apply("s", fill(m, Side.YES, 0.204, 10))  # every bucket: $10.20 for a sure $10
    pf.apply("s", fill("t:SOLO", Side.YES, 0.5, 4))
    risk = Risk(pf, {}, markets)
    held = [(m, side, p) for _, m, side, p in pf.open_positions("s")]
    assert risk.at_risk(held, []) == pytest.approx(0.20 + 2.0)  # ladder netted, the lone market at cost
    # $12.20 at cost would block a $3 order under a $6 limit; netted ($2.20) it passes
    assert risk.check(order("t:SOLO", 0.5, 6), Limits(max_open_risk=6), "s") is None
    assert risk.check(order("t:SOLO", 0.5, 8), Limits(max_open_risk=6), "s") is not None


def test_outcomes_lists_the_whole_question() -> None:

    seen: list[list[str]] = []
    s = Hooks(data=lambda ctx, ev: seen.append(sorted(m.id for m in ctx.outcomes("t:B2"))))
    e = Engine([Instance("s", s, SimBroker())], clock_ms=None)
    e.run([*(MarketOpen(ts=0, market=m) for m in ladder().values()), IndexValue(ts=1, key="k", value=1.0)])
    assert seen == [[f"t:B{i}" for i in range(5)]]
