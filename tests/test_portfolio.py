import pytest
from hypothesis import given
from hypothesis import strategies as st

from quanty.core import Action, Fill, Liquidity, Side
from quanty.core.portfolio import DAY_MS, Portfolio


def fill(action: Action, side: Side, price: float, qty: float, fee: float = 0.0, ts: int = 0) -> Fill:
    return Fill(ts=ts, order_id="o", market_id="m", side=side, action=action, price=price, qty=qty, fee=fee,
                liquidity=Liquidity.TAKER)  # fmt: skip


def test_buy_sell_settle() -> None:
    p = Portfolio()
    p.apply("a", fill(Action.BUY, Side.YES, 0.40, 10, fee=0.10))
    assert p.apply("a", fill(Action.SELL, Side.YES, 0.50, 4, fee=0.04)) == pytest.approx(
        2.0 - 0.04 - 4 * 0.41
    )
    (s,) = p.settle("m", Side.YES, ts=1)
    assert (s.qty, s.payout) == (6, 6.0)
    assert s.pnl == pytest.approx(6.0 - 6 * 0.41)
    assert p.realized["a"] == pytest.approx(-4.10 + 1.96 + 6.0)  # cash out, in, payout


def test_losing_side_pays_nothing() -> None:
    p = Portfolio()
    p.apply("a", fill(Action.BUY, Side.NO, 0.30, 10))
    (s,) = p.settle("m", Side.YES, ts=1)
    assert s.payout == 0.0 and s.pnl == pytest.approx(-3.0)


def test_cannot_sell_more_than_held() -> None:
    p = Portfolio()
    p.apply("a", fill(Action.BUY, Side.YES, 0.40, 1))
    with pytest.raises(ValueError):
        p.apply("a", fill(Action.SELL, Side.YES, 0.40, 2))


def test_daily_pnl_by_utc_day() -> None:
    p = Portfolio()
    p.apply("a", fill(Action.BUY, Side.YES, 0.50, 10))
    p.apply("a", fill(Action.SELL, Side.YES, 0.60, 5, ts=10))
    p.apply("a", fill(Action.SELL, Side.YES, 0.40, 5, ts=DAY_MS + 10))
    assert p.daily_pnl("a", 0) == pytest.approx(0.5)
    assert p.daily_pnl(None, DAY_MS) == pytest.approx(-0.5)


trade = st.tuples(
    st.sampled_from([Side.YES, Side.NO]),
    st.booleans(),  # True = buy
    st.integers(1, 99).map(lambda c: c / 100),
    st.integers(1, 50),
    st.integers(0, 20).map(lambda c: c / 100),
)


@given(st.lists(trade, max_size=40), st.sampled_from([Side.YES, Side.NO]))
def test_realized_pnl_equals_cash_flow(
    trades: list[tuple[Side, bool, float, float, float]], outcome: Side
) -> None:
    """After settlement, every dollar in or out is realized P&L: nothing lost or double counted."""
    p = Portfolio()
    cash = 0.0
    for side, is_buy, price, qty, fee in trades:
        if is_buy:
            p.apply("a", fill(Action.BUY, side, price, qty, fee))
            cash -= price * qty + fee
        else:
            qty = min(qty, p.position("a", "m", side).qty)
            if qty:
                p.apply("a", fill(Action.SELL, side, price, qty, fee))
                cash += price * qty - fee
    for s in p.settle("m", outcome, ts=0):
        cash += s.payout
    assert p.realized["a"] == pytest.approx(cash, abs=1e-6)
    assert list(p.open_positions()) == []
