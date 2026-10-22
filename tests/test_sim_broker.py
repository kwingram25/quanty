import pytest
from helpers import Hooks, M, book, make, once, opened

from quanty.core import Action, Context, Liquidity, MarketClose, MarketTrade, QuadraticFee, Side, Tif


def buy(side: Side, price: float, qty: int, **kw: object) -> Hooks:
    return Hooks(book=once(lambda ctx, b: ctx.buy(M.id, side, price, qty, **kw)))


def trade(ts: int, price: float, qty: int, taker: Action | None = Action.SELL) -> MarketTrade:
    return MarketTrade(ts=ts, market_id=M.id, price=price, qty=qty, taker=taker)


def test_taker_walks_levels_at_level_prices() -> None:
    s = buy(Side.YES, 0.41, 12, tif=Tif.IOC)
    e = make(s)
    e.run([*opened(), book(1, [(0.39, 10)], [(0.40, 5), (0.41, 10), (0.42, 10)])])
    assert [(f.price, f.qty, f.liquidity) for f in s.fills()] == [
        (0.40, 5, Liquidity.TAKER),
        (0.41, 7, Liquidity.TAKER),
    ]
    assert s.statuses() == ["filled"]
    pos = e.portfolio.position("s0", M.id, Side.YES)
    assert pos.qty == 12
    assert pos.cost == pytest.approx(0.40 * 5 + 0.41 * 7)


def test_ioc_remainder_is_cancelled() -> None:
    s = buy(Side.YES, 0.41, 20, tif=Tif.IOC)
    make(s).run([*opened(), book(1, [], [(0.40, 5), (0.41, 10)])])
    assert sum(f.qty for f in s.fills()) == 15
    assert s.statuses() == ["cancelled"]


def test_buying_no_takes_yes_bids() -> None:
    s = buy(Side.NO, 0.41, 10, tif=Tif.IOC)
    make(s).run([*opened(), book(1, [(0.60, 4), (0.59, 10)], [(0.62, 10)])])
    assert [(f.side, f.price, f.qty) for f in s.fills()] == [(Side.NO, 0.40, 4), (Side.NO, 0.41, 6)]


def test_post_only_that_would_take_is_rejected() -> None:
    s = buy(Side.YES, 0.40, 5, post_only=True)
    make(s).run([*opened(), book(1, [], [(0.40, 5)])])
    assert s.statuses() == ["rejected"]
    assert s.fills() == []


def test_resting_order_waits_for_queue_then_fills() -> None:
    s = buy(Side.YES, 0.50, 10, post_only=True)
    make(s).run(
        [
            *opened(),
            book(1, [(0.50, 30)], [(0.52, 10)]),
            trade(2, 0.50, 20),  # queue 30 -> 10
            trade(3, 0.50, 15),  # queue 10 -> 0, we get 5
            trade(4, 0.50, 3, taker=Action.BUY),  # a buyer lifting asks never hits our bid
            trade(5, 0.49, 100),  # prints through us: the rest
        ]
    )
    assert [(f.ts, f.qty, f.liquidity) for f in s.fills()] == [
        (3, 5, Liquidity.MAKER),
        (5, 5, Liquidity.MAKER),
    ]
    assert s.statuses() == ["open", "filled"]


def test_queue_shrinks_when_the_level_shrinks() -> None:
    s = buy(Side.YES, 0.50, 10)
    make(s).run([*opened(), book(1, [(0.50, 30)], []), book(2, [(0.50, 4)], []), trade(3, 0.50, 6)])
    assert [f.qty for f in s.fills()] == [2]


def test_book_moving_onto_resting_order_fills_it_at_its_price() -> None:
    s = buy(Side.YES, 0.50, 10)
    make(s).run([*opened(), book(1, [(0.48, 5)], [(0.53, 5)]), book(2, [(0.48, 5)], [(0.49, 3), (0.50, 2)])])
    assert [(f.price, f.qty, f.liquidity) for f in s.fills()] == [
        (0.50, 3, Liquidity.MAKER),
        (0.50, 2, Liquidity.MAKER),
    ]


def test_latency_order_meets_the_book_at_arrival() -> None:
    s = buy(Side.YES, 0.42, 5, tif=Tif.IOC)
    make(s, latency_ms=100).run([*opened(), book(1000, [], [(0.40, 50)]), book(1050, [], [(0.45, 50)])])
    assert s.fills() == []
    assert s.statuses() == ["cancelled"]


def test_taken_liquidity_is_not_available_twice() -> None:
    a, b = buy(Side.YES, 0.40, 8, tif=Tif.IOC), buy(Side.YES, 0.40, 8, tif=Tif.IOC)
    make(a, b).run([*opened(), book(1, [], [(0.40, 10)])])
    assert sum(f.qty for f in a.fills() + b.fills()) == 10


def test_cancel_and_late_cancel() -> None:
    def place_then_cancel(ctx: Context, _: object) -> None:
        ctx.cancel(ctx.buy(M.id, Side.YES, 0.30, 5))

    s = Hooks(book=once(place_then_cancel))
    make(s, latency_ms=10).run([*opened(), book(1, [(0.30, 1)], [(0.35, 1)])])
    assert s.statuses() == ["open", "cancelled"]

    filled = Hooks(book=once(lambda ctx, b: ctx.cancel(ctx.buy(M.id, Side.YES, 0.35, 1, tif=Tif.IOC))))
    make(filled, latency_ms=10).run([*opened(), book(1, [], [(0.35, 1)])])
    assert filled.statuses() == ["filled"]


def test_resting_orders_expire_at_close() -> None:
    s = buy(Side.YES, 0.30, 5)
    e = make(s)
    e.run([*opened(), book(1, [], [(0.35, 1)]), MarketClose(ts=10, market_id=M.id)])
    assert s.statuses() == ["open", "expired"]
    assert e.live == {}


def test_fees_by_venue() -> None:
    s = buy(Side.YES, 0.40, 10, tif=Tif.IOC)
    make(s, fees={"t": QuadraticFee(0.07, round_up_cents=True)}).run([*opened(), book(1, [], [(0.40, 10)])])
    assert s.fills()[0].fee == pytest.approx(0.17)  # 0.07 × 10 × 0.4 × 0.6 = 0.168 -> 0.17
