import pytest
from helpers import M2, Hooks, M, book, make, once, opened
from sqlalchemy import select

from quanty.core import (
    Book,
    Clock,
    Context,
    IndexValue,
    Limits,
    Market,
    MarketClose,
    MarketTrade,
    Order,
    OrderStatus,
    Settled,
    Side,
    Tif,
)
from quanty.db.models import LogRow


def test_clock_ticks_between_events() -> None:
    s = Hooks()
    make(s, clock_ms=1000).run([*opened(), IndexValue(ts=3500, key="x", value=1.0)])
    assert [a.ts for n, a in s.seen if n == "clock" and isinstance(a, Clock)] == [1000, 2000, 3000]


def test_data_events_reach_strategies_and_latest() -> None:
    def check(ctx: Context, ev: object) -> None:
        assert ctx.latest("x") is ev

    s = Hooks(data=check)
    make(s).run([IndexValue(ts=1, key="x", value=2.0)])
    assert len(s.seen) == 1


@pytest.mark.parametrize(
    ("market", "price", "qty", "reason"),
    [
        ("t:nope", 0.5, 1, "not open"),
        (M.id, 0.505, 1, "tick"),
        (M.id, 1.0, 1, "between 0 and 1"),
        (M.id, 0.5, 0, "positive"),
    ],
)
def test_invalid_orders_are_rejected(market: str, price: float, qty: int, reason: str) -> None:
    got: list[Order] = []
    s = Hooks(book=once(lambda ctx, b: got.append(ctx.buy(market, Side.YES, price, qty))))
    make(s).run([*opened(), book(1, [], [(0.5, 1)])])
    assert got[0].status is OrderStatus.REJECTED
    assert reason in got[0].reason


def test_settlement_pnl_reaches_ledger() -> None:
    s = Hooks(book=once(lambda ctx, b: ctx.buy(M.id, Side.YES, 0.40, 10, tif=Tif.IOC)))
    e = make(s)
    e.run([*opened(), book(1, [], [(0.40, 10)]), Settled(ts=5, market_id=M.id, outcome=Side.YES)])
    (row,) = e.ledger.summary()
    assert (row.fills, row.contracts, row.wins, row.losses) == (1, 10, 1, 0)
    assert row.pnl == pytest.approx(6.0)
    assert ("settled", Side.YES) in s.seen
    assert e.portfolio.position("s0", M.id, Side.YES).qty == 0


def test_selling_realizes_pnl_and_cannot_exceed_position() -> None:
    sells: list[Order] = []

    def on_fill(ctx: Context, _: object) -> None:
        if not sells:
            sells.append(ctx.sell(M.id, Side.YES, 0.55, 11, tif=Tif.IOC))  # more than held
            sells.append(ctx.sell(M.id, Side.YES, 0.55, 10, tif=Tif.IOC))

    s = Hooks(book=once(lambda ctx, b: ctx.buy(M.id, Side.YES, 0.40, 10, tif=Tif.IOC)), fill=on_fill)
    e = make(s)
    e.run([*opened(), book(1, [(0.55, 20)], [(0.40, 10)])])
    assert sells[0].status is OrderStatus.REJECTED
    assert sells[1].status is OrderStatus.FILLED
    assert e.portfolio.realized["s0"] == pytest.approx(1.5)


def test_fills_arrive_before_the_next_input_at_the_same_ts() -> None:
    s = Hooks(book=once(lambda ctx, b: ctx.buy(M.id, Side.YES, 0.40, 1, tif=Tif.IOC)))
    make(s).run([*opened(), book(1, [], [(0.40, 1)]), MarketTrade(ts=1, market_id=M.id, price=0.4, qty=1)])
    assert [n for n, _ in s.seen] == ["book", "fill", "order", "trade"]


def test_strategy_errors_raise_when_strict() -> None:
    def boom(ctx: Context, _: object) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        make(Hooks(book=boom)).run([*opened(), book(1, [], [])])


def test_strategy_errors_halt_when_not_strict() -> None:
    got: list[Order] = []

    def first_rest_then_boom(ctx: Context, b: object) -> None:
        if not got:
            got.append(ctx.buy(M.id, Side.YES, 0.30, 5))
            return
        raise RuntimeError("boom")

    e = make(Hooks(book=first_rest_then_boom), strict=False)
    e.run([*opened(), book(1, [], [(0.4, 1)]), book(2, [], [(0.4, 1)])])
    assert "s0" in e.halted
    assert got[0].status is OrderStatus.CANCELED
    levels = list(
        e.ledger.execute(select(LogRow.level).where(LogRow.msg != "strategy").order_by(LogRow.id)).scalars()
    )
    assert levels == ["error", "warning"]


def test_halt_blocks_buys_but_allows_sells() -> None:
    got: list[Order] = []

    def step(ctx: Context, b: object) -> None:
        if ctx.position(M.id, Side.YES) and not got:
            e.halt("s0")
            got.append(ctx.buy(M.id, Side.YES, 0.40, 1, tif=Tif.IOC))
            got.append(ctx.sell(M.id, Side.YES, 0.39, 5, tif=Tif.IOC))
        elif not ctx.position(M.id, Side.YES):
            ctx.buy(M.id, Side.YES, 0.40, 5, tif=Tif.IOC)

    s = Hooks(book=step)
    e = make(s)
    e.run([*opened(), book(1, [(0.39, 10)], [(0.40, 10)]), book(2, [(0.39, 10)], [(0.40, 10)])])
    assert got[0].status is OrderStatus.REJECTED and got[0].reason == "halted"
    assert not any(o.reason == "halted" for o in e.orders.values())  # counted, not kept order by order
    assert e.blocked == {"s0": 1}
    assert got[1].status is OrderStatus.FILLED


# --- risk limits ---


def run_buys(orders: list[tuple[str, float, int]], limits: Limits) -> list[Order]:
    got: list[Order] = []

    def go(ctx: Context, _: object) -> None:
        got.extend(ctx.buy(m, Side.YES, p, q) for m, p, q in orders)

    make(Hooks(book=once(go)), limits=limits).run([*opened(M, M2), book(1, [], [])])
    return got


def test_max_order_qty() -> None:
    got = run_buys([(M.id, 0.5, 5), (M.id, 0.5, 6)], limits=Limits(max_order_qty=5))
    assert [o.status for o in got] == [OrderStatus.OPEN, OrderStatus.REJECTED]
    assert "max_order_qty" in got[1].reason


def test_max_position_counts_resting_buys() -> None:
    got = run_buys(
        [(M.id, 0.5, 6), (M.id, 0.5, 4), (M.id, 0.5, 1), (M2.id, 0.5, 10)], limits=Limits(max_position=10)
    )
    assert [o.status is OrderStatus.REJECTED for o in got] == [False, False, True, False]


def test_max_open_risk_and_group_risk() -> None:
    got = run_buys([(M.id, 0.5, 10), (M2.id, 0.5, 10), (M2.id, 0.5, 1)], limits=Limits(max_open_risk=10.0))
    assert [o.status is OrderStatus.REJECTED for o in got] == [False, False, True]
    got = run_buys(
        [(M.id, 0.5, 10), (M2.id, 0.5, 1)], limits=Limits(max_group_risk=5.0)
    )  # M, M2 share group g
    assert [o.status is OrderStatus.REJECTED for o in got] == [False, True]


def test_account_limits_span_instances() -> None:
    def go(ctx: Context, _: object) -> None:
        ctx.buy(M.id, Side.YES, 0.5, 8)

    a, b = Hooks(book=once(go)), Hooks(book=once(go))
    e = make(a, b, account=Limits(max_position=10))
    e.run([*opened(), book(1, [], [])])
    statuses = sorted(o.status for o in e.orders.values())
    assert statuses == [OrderStatus.OPEN, OrderStatus.REJECTED]


def test_daily_loss_blocks_buys_until_the_next_utc_day() -> None:
    day = 86_400_000
    markets = [Market(id=f"t:D{i}", venue="t", series="t:s", open_ts=0, close_ts=3 * day) for i in range(3)]
    results: list[Order] = []

    def go(ctx: Context, b: Book) -> None:
        mid = b.market_id
        if not ctx.position(mid, Side.YES):
            results.append(ctx.buy(mid, Side.YES, 0.50, 10, tif=Tif.IOC))

    e = make(Hooks(book=go), limits=Limits(max_daily_loss=4.0))
    e.run(
        [
            *opened(*markets),
            book(1, [], [(0.5, 10)], markets[0]),
            Settled(ts=2, market_id=markets[0].id, outcome=Side.NO),  # −5
            book(3, [], [(0.5, 10)], markets[1]),  # same day: blocked
            book(day + 1, [], [(0.5, 10)], markets[2]),  # next day: allowed
        ]
    )
    assert [o.status for o in results] == [OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.FILLED]


def test_orders_after_close_are_rejected() -> None:
    got: list[Order] = []
    s = Hooks(trade=lambda ctx, t: got.append(ctx.buy(M.id, Side.YES, 0.5, 1)))
    e = make(s)
    e.run([*opened(), MarketClose(ts=5, market_id=M.id), MarketTrade(ts=6, market_id=M.id, price=0.5, qty=1)])
    assert got[0].status is OrderStatus.REJECTED
    assert "not open" in got[0].reason
    assert M.id not in e.open_markets


def test_entry_price_is_the_average_cost_held() -> None:
    from quanty.core import Liquidity, Tif

    class CentEach:  # a fee of 1¢ per contract
        def fee(self, price: float, qty: float, liquidity: Liquidity) -> float:
            return 0.01 * qty

    seen: list[float | None] = []

    def buy_twice(ctx: Context, b: Book) -> None:
        seen.append(ctx.entry_price(M.id, Side.YES))  # nothing held yet
        ctx.buy(M.id, Side.YES, 0.60, 10, tif=Tif.IOC)
        ctx.buy(M.id, Side.YES, 0.70, 10, tif=Tif.IOC)

    s = Hooks(book=once(buy_twice), fill=lambda ctx, f: seen.append(ctx.entry_price(M.id, Side.YES)))
    make(s, fees={"t": CentEach()}).run([*opened(), book(1, [(0.55, 50)], [(0.60, 10), (0.70, 10)])])
    assert seen[0] is None and len(s.fills()) == 2
    assert seen[1] == pytest.approx(0.61) and seen[2] == pytest.approx(0.66)  # (6.10 + 7.10) / 20, fees in
