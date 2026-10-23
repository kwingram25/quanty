import asyncio
import json

import httpx
import pytest
from helpers import Hooks, book, once
from sqlalchemy import select

from quanty.brokers import ShadowBroker
from quanty.core import Engine, Instance, Market, MarketOpen, Order, Side, Tif
from quanty.core.types import Action
from quanty.db.models import LogRow
from quanty.venues.orders import KalshiOrders, PolymarketOrders, Preview

PM = Market(id="polymarket_us:cpc-btc-updown-15m-2026-10-23-0000z", venue="polymarket_us",
            series="polymarket_us:btc_updown_15m", open_ts=1_792_713_600_000, close_ts=1_792_714_500_000,
            qty_step=0.01)  # fmt: skip
KX = Market(id="kalshi:KXBTC15M-26OCT222015-15", venue="kalshi", series="kalshi:KXBTC15M",
            open_ts=1_792_713_600_000, close_ts=1_792_714_500_000, tick_ranges=((0.9, 1.0, 0.001),),
            qty_step=0.01)  # fmt: skip


class FakeSigner:
    def headers(self, method: str, path: str) -> dict[str, str]:
        return {"X-Signed": f"{method} {path}"}


def order(side: Side, action: Action, price: float, qty: float, **kw: object) -> Order:
    return Order(id="o1", instance="s", market_id=PM.id, side=side, action=action, price=price, qty=qty, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("side", "action", "price", "intent", "yes_price"),
    [
        (Side.YES, Action.BUY, 0.99, "ORDER_INTENT_BUY_LONG", "0.99"),
        (Side.NO, Action.BUY, 0.99, "ORDER_INTENT_BUY_SHORT", "0.01"),
        (Side.YES, Action.SELL, 0.05, "ORDER_INTENT_SELL_LONG", "0.05"),
        (Side.NO, Action.SELL, 0.05, "ORDER_INTENT_SELL_SHORT", "0.95"),
    ],
)
def test_polymarket_request_in_yes_terms(
    side: Side, action: Action, price: float, intent: str, yes_price: str
) -> None:
    req = PolymarketOrders(FakeSigner()).place_request(order(side, action, price, 100, tif=Tif.IOC), PM)
    assert (req.method, req.path) == ("POST", "/v1/orders")
    b = req.body
    assert (b["intent"], b["price"]["value"], b["quantity"]) == (intent, yes_price, 100)
    assert (
        b["marketSlug"] == "cpc-btc-updown-15m-2026-10-23-0000z"
        and b["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    )


def test_polymarket_resting_post_only_order() -> None:
    req = PolymarketOrders(FakeSigner()).place_request(
        order(Side.YES, Action.BUY, 0.99, 12.5, post_only=True), PM
    )
    b = req.body
    assert b["tif"] == "TIME_IN_FORCE_GOOD_TILL_DATE" and b["goodTillTime"] == "2026-10-23T00:14:58Z"
    assert b["participateDontInitiate"] is True and b["quantity"] == 12.5


def test_kalshi_request_in_yes_terms() -> None:
    o = Order(id="o1", instance="s", market_id=KX.id, side=Side.NO, action=Action.BUY, price=0.995, qty=99.98,
              post_only=True)  # fmt: skip
    b = KalshiOrders(FakeSigner()).place_request(o, KX).body
    assert (b["ticker"], b["side"], b["price"], b["count"]) == (
        "KXBTC15M-26OCT222015-15",
        "ask",
        "0.0050",
        "99.98",
    )
    assert b["time_in_force"] == "good_till_canceled" and b["expiration_time"] == 1_792_714_498
    assert b["post_only"] is True and b["self_trade_prevention_type"] == "maker"


def test_polymarket_preview_calls_the_preview_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if json.loads(req.content)["request"]["quantity"] == 7:
            return httpx.Response(400, json={"error": "quantity too small"})
        return httpx.Response(200, json={"order": {"state": "PREVIEW"}})

    async def go() -> tuple[bool, bool]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            api = PolymarketOrders(FakeSigner())
            ok = await api.preview(c, order(Side.YES, Action.BUY, 0.99, 100), PM)
            bad = await api.preview(c, order(Side.YES, Action.BUY, 0.99, 7), PM)
            return ok.ok and ok.rtt_ms is not None, (not bad.ok) and "too small" in bad.detail

    assert asyncio.run(go()) == (True, True)
    assert seen[0].url.path == "/v1/order/preview" and seen[0].headers["X-Signed"] == "POST /v1/order/preview"
    assert json.loads(seen[0].content)["request"]["intent"] == "ORDER_INTENT_BUY_LONG"


def test_kalshi_preview_is_local() -> None:
    async def go() -> tuple[bool, str]:
        async with httpx.AsyncClient() as c:
            o = Order(
                id="o1", instance="s", market_id=KX.id, side=Side.YES, action=Action.BUY, price=0.9955, qty=1
            )
            p = await KalshiOrders(FakeSigner()).preview(c, o, KX)
            return p.ok, p.detail

    ok, detail = asyncio.run(go())
    assert not ok and "off tick" in detail


def shadow_engine(api: PolymarketOrders) -> tuple[Engine, Hooks]:
    s = Hooks(book=once(lambda ctx, b: ctx.buy(PM.id, Side.YES, 0.45, 10, tif=Tif.IOC)))
    broker = ShadowBroker(apis={"polymarket_us": api})
    return Engine([Instance("s", s, broker)], clock_ms=None), s


class AcceptAll(PolymarketOrders):
    async def preview(self, client: httpx.AsyncClient, order: Order, market: Market) -> Preview:
        return Preview(True, self.place_request(order, market), 200, 12.3)


def test_shadow_previews_in_a_live_session_and_still_simulates() -> None:
    api = AcceptAll(FakeSigner())
    engine, s = shadow_engine(api)

    async def go() -> None:
        engine.run([MarketOpen(ts=PM.open_ts, market=PM), book(PM.open_ts + 1, [], [(0.45, 20)], market=PM)])
        broker = engine.instances["s"].broker
        assert isinstance(broker, ShadowBroker)
        await broker.aclose()

    asyncio.run(go())
    assert [f.qty for f in s.fills()] == [10]  # simulated fill as usual
    (row,) = engine.ledger.execute(select(LogRow).where(LogRow.msg == "shadow preview")).scalars()
    assert (
        row.fields["ok"] is True and row.fields["rtt_ms"] == 12.3 and row.fields["venue"] == "polymarket_us"
    )


def test_shadow_in_a_backtest_only_simulates() -> None:
    engine, s = shadow_engine(PolymarketOrders(FakeSigner()))
    engine.run([MarketOpen(ts=PM.open_ts, market=PM), book(PM.open_ts + 1, [], [(0.45, 20)], market=PM)])
    assert [f.qty for f in s.fills()] == [10]
    assert list(engine.ledger.execute(select(LogRow).where(LogRow.msg == "shadow preview")).scalars()) == []
