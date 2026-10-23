"""The live broker against the fake exchange: real request/response handling, no real orders."""

import asyncio
from collections.abc import Callable

import pytest
from helpers import Hooks
from sqlalchemy import select

from quanty.brokers.live import LiveBroker, now_ms
from quanty.core import Book, Context, Engine, Instance, Market, MarketOpen, Order, Side, Tif
from quanty.core.types import Action
from quanty.db.models import LogRow
from quanty.venues.fake import FakeExchange
from quanty.venues.orders import KalshiOrders, PolymarketOrders


class FakeSigner:
    def headers(self, method: str, path: str) -> dict[str, str]:
        return {"X-Signed": f"{method} {path}"}


def markets() -> tuple[Market, Market]:
    t = now_ms()
    pm = Market(id="polymarket_us:btc-up", venue="polymarket_us", series="pm", open_ts=t - 60_000,
                close_ts=t + 600_000, qty_step=0.01)  # fmt: skip
    kx = Market(id="kalshi:KXBTC-UP", venue="kalshi", series="kx", open_ts=t - 60_000, close_ts=t + 600_000,
                tick_ranges=((0.0, 1.0, 0.001),), qty_step=0.01)  # fmt: skip
    return pm, kx


Action_ = Callable[[Context], object]


class Harness:
    def __init__(self, fake: FakeExchange, act: Action_, **broker_kw: object) -> None:
        self.pm, self.kx = markets()
        self.fake = fake
        apis = {"polymarket_us": PolymarketOrders(FakeSigner()), "kalshi": KalshiOrders(FakeSigner())}
        kw = {"armed": True, "poll_s": 0.02, "final_read_every_s": 0.01, **broker_kw}
        self.broker = LiveBroker(apis=apis, transport=fake.transport(), **kw)  # type: ignore[arg-type]
        self.done = False

        def on_book(ctx: Context, b: Book) -> None:
            if not self.done:
                self.done = True
                act(ctx)

        self.s = Hooks(book=on_book)
        self.engine = Engine([Instance("s", self.s, self.broker)], clock_ms=None, strict=False)

    async def start(self) -> None:
        for m in (self.pm, self.kx):
            self.engine.process(MarketOpen(ts=now_ms(), market=m))
        self.engine.process(Book(ts=now_ms(), market_id=self.pm.id, bids=(), asks=()))

    async def until(self, cond: Callable[[], bool], timeout_s: float = 2.0) -> None:
        end = now_ms() + timeout_s * 1000
        while now_ms() < end:
            self.engine.advance(now_ms())
            if cond():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition not reached")

    def order(self) -> Order:
        (o,) = self.engine.orders.values()
        return o

    def logs(self, msg: str) -> list[LogRow]:
        return list(self.engine.ledger.execute(select(LogRow).where(LogRow.msg == msg)).scalars())


def run(h: Harness, body: Callable[[Harness], object]) -> None:
    async def go() -> None:
        await h.start()
        await body(h)  # type: ignore[misc]
        await h.broker.aclose()

    asyncio.run(go())


def test_ioc_fills_from_the_reply_in_order_terms() -> None:
    fake = FakeExchange(taker_fee=0.01)
    fake.set_book("btc-up", bids=[(0.98, 50)], asks=[(0.99, 5)])

    def act(ctx: Context) -> None:  # buy NO at 2¢ = sell YES at 98¢ into the bid
        ctx.buy("polymarket_us:btc-up", Side.NO, 0.02, 30, tif=Tif.IOC)

    h = Harness(fake, act)

    async def body(h: Harness) -> None:
        await h.until(lambda: h.order().status.done)

    run(h, body)
    (f,) = h.s.fills()
    assert (f.side, f.qty, f.price, round(f.fee, 4)) == (Side.NO, 30, 0.02, 0.30)
    assert h.order().status == "filled"
    sent = fake.requests[0]
    assert sent.headers["X-Signed"] == "POST /v1/orders"
    assert fake.orders["po-1"].buys_yes is False


def test_resting_order_fills_by_polling_then_cancel_settles_it() -> None:
    fake = FakeExchange()
    fake.set_book("KXBTC-UP", bids=[(0.98, 10)], asks=[(0.999, 10)])
    h = Harness(fake, lambda ctx: ctx.buy("kalshi:KXBTC-UP", Side.YES, 0.995, 100, post_only=True))

    async def body(h: Harness) -> None:
        await h.until(lambda: h.order().status == "open")
        fake.trade("KXBTC-UP", 0.995, 40, taker_buys=False)
        await h.until(lambda: h.order().filled == 40)
        h.engine.instances["s"].broker.cancel(h.order())
        await h.until(lambda: h.order().status.done)

    run(h, body)
    assert [(f.qty, f.price, f.liquidity) for f in h.s.fills()] == [(40, 0.995, "maker")]
    assert h.order().status == "canceled" and h.order().filled == 40
    assert fake.orders["ka-1"].canceled


def test_post_only_cross_is_rejected_and_rejections_disable_the_broker() -> None:
    fake = FakeExchange()
    fake.set_book("KXBTC-UP", bids=[], asks=[(0.99, 10)])

    def act(ctx: Context) -> None:
        for _ in range(3):
            ctx.buy("kalshi:KXBTC-UP", Side.YES, 0.995, 1, post_only=True)

    h = Harness(fake, act, error_limit=3)

    async def body(h: Harness) -> None:
        await h.until(lambda: all(o.status.done for o in h.engine.orders.values()))

    run(h, body)
    assert [o.status for o in h.engine.orders.values()] == ["rejected"] * 3
    assert "post_only_cross" in next(iter(h.engine.orders.values())).reason
    assert h.broker.disabled == "3 consecutive rejections"
    assert len(h.logs("live broker disabled")) == 1


def test_timeout_leaves_the_order_pending_and_disables() -> None:
    fake = FakeExchange()
    fake.set_book("btc-up", bids=[], asks=[(0.50, 100)])
    fake.fail_next("timeout")
    h = Harness(fake, lambda ctx: ctx.buy("polymarket_us:btc-up", Side.YES, 0.50, 10, tif=Tif.IOC))

    async def body(h: Harness) -> None:
        await h.until(lambda: bool(h.broker.disabled))
        h.engine.advance(now_ms())

    run(h, body)
    assert h.order().status == "pending"  # not assumed rejected: the venue did fill it
    assert fake.orders["po-1"].filled == 10
    assert "outcome unknown" in h.broker.disabled
    assert h.logs("live order outcome unknown")


def test_lost_reads_are_retried() -> None:
    fake = FakeExchange()
    fake.set_book("btc-up", bids=[], asks=[(0.995, 10)])
    h = Harness(fake, lambda ctx: ctx.buy("polymarket_us:btc-up", Side.YES, 0.99, 20, post_only=True))

    async def body(h: Harness) -> None:
        await h.until(lambda: h.order().status == "open")
        fake.fail_next("lost_read")
        fake.fail_next("lost_read")
        fake.trade("btc-up", 0.99, 20, taker_buys=False)
        await h.until(lambda: h.order().status.done)

    run(h, body)
    assert h.order().status == "filled" and [f.qty for f in h.s.fills()] == [20]


def test_cancel_during_placement_is_sent_once_placed() -> None:
    fake = FakeExchange()
    fake.set_book("btc-up", bids=[], asks=[(0.995, 10)])

    def act(ctx: Context) -> None:
        o = ctx.buy("polymarket_us:btc-up", Side.YES, 0.99, 20, post_only=True)
        assert o is not None
        ctx.cancel(o)

    h = Harness(fake, act)

    async def body(h: Harness) -> None:
        await h.until(lambda: h.order().status.done)

    run(h, body)
    assert h.order().status == "canceled" and fake.orders["po-1"].canceled


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"armed": False}, "not armed"),
        ({"max_qty": 5}, "over max_qty"),
        ({"max_notional": 1.0}, "over max_notional"),
    ],
)
def test_refusals(kw: dict[str, object], reason: str) -> None:
    fake = FakeExchange()
    h = Harness(fake, lambda ctx: ctx.buy("polymarket_us:btc-up", Side.YES, 0.5, 10, tif=Tif.IOC), **kw)

    async def body(h: Harness) -> None:
        await h.until(lambda: h.order().status.done)

    run(h, body)
    assert h.order().status == "rejected" and reason in h.order().reason
    assert fake.requests == []


def test_sell_order_terms() -> None:
    o = Order(
        id="x", instance="s", market_id="kalshi:KXBTC-UP", side=Side.NO, action=Action.SELL, price=0.05, qty=3
    )
    _, kx = markets()
    b = KalshiOrders(FakeSigner()).place_request(o, kx).body
    assert (b["side"], b["price"]) == ("bid", "0.9500")  # selling NO at ≥ 5¢ = buying YES at ≤ 95¢


def test_polymarket_rejection_inside_a_200_reply() -> None:
    fake = FakeExchange()
    fake.set_book("btc-up", bids=[], asks=[(0.99, 10)])
    h = Harness(fake, lambda ctx: ctx.buy("polymarket_us:btc-up", Side.YES, 0.99, 5, post_only=True))

    async def body(h: Harness) -> None:
        await h.until(lambda: h.order().status.done)

    run(h, body)
    assert h.order().status == "rejected" and "would match" in h.order().reason
