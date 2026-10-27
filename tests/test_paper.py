"""Paper trading end to end: a live (local) venue feed drives strategies in real time with simulated fills."""

import asyncio
import time
from pathlib import Path

import httpx
import pytest
from helpers import Hooks, once
from sqlalchemy import select
from venue_msgs import MIN, pm_book, pm_market
from websockets.asyncio.server import ServerConnection, serve

from quanty import feeds
from quanty.brokers.sim import SimBroker
from quanty.core import Clock, Context, Engine, Instance, Ledger, Side, Tif
from quanty.db.models import FillRow, OrderRow
from quanty.feeds.session import RecordSession
from quanty.venues.polymarket_us import PolymarketUS, slug_for


def test_paper_session_trades_on_a_live_feed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(PolymarketUS, "overlap_s", 0.1)
    feed = feeds.make("polymarket_us", {"poll_s": 1, "results_for_s": 1, "lookahead_s": 60})
    assert isinstance(feed, PolymarketUS)
    spec = feed.config.series[0]
    now = time.time_ns() // 1_000_000
    start = now - now % (spec.period_s * 1000)
    slug = slug_for(spec, start)

    def rest(req: httpx.Request) -> httpx.Response:
        if not req.url.path.endswith(slug):
            return httpx.Response(404)
        return httpx.Response(200, json={"market": pm_market(slug, start, start + 15 * MIN, ptb="1")})

    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.recv()
        await ws.send(pm_book(slug, bids=[("0.40", "5")], offers=[("0.45", "20")]))
        await ws.wait_closed()

    def buy(ctx: Context, b: object) -> None:
        ctx.buy(f"polymarket_us:{slug}", Side.YES, 0.45, 10, tif=Tif.IOC)

    strategy = Hooks(book=once(buy))
    ledger = Ledger(tmp_path / "paper.sqlite", durable=True)
    trader = Engine(
        [Instance("s", strategy, SimBroker(latency_ms=50))], ledger=ledger, clock_ms=200, strict=False
    )

    async def go() -> None:
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            feed.config = feed.config.model_copy(update={"url": f"ws://127.0.0.1:{port}"})
            feed.transport = httpx.MockTransport(rest)
            feed.headers = lambda: {}  # type: ignore[method-assign]
            session = RecordSession({"polymarket_us": feed}, tmp_path / "rec", window_s=3600, trader=trader)
            await session.run(duration_s=3.0)

    asyncio.run(go())
    fills = [(r.qty, r.price) for r in ledger.execute(select(FillRow.qty, FillRow.price))]
    assert fills == [(10.0, 0.45)]
    assert ledger.execute(select(OrderRow.status)).scalar_one() == "filled"
    assert sum(1 for n, a in strategy.seen if n == "clock" and isinstance(a, Clock)) >= 5  # wall-clock ticks
    assert (tmp_path / "rec" / "polymarket_us").is_dir()  # and everything was recorded


def test_live_events_during_the_warm_up_build_state_but_never_trade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PolymarketUS, "overlap_s", 0.1)
    feed = feeds.make("polymarket_us", {"poll_s": 1, "results_for_s": 1, "lookahead_s": 60})
    assert isinstance(feed, PolymarketUS)
    spec = feed.config.series[0]
    now = time.time_ns() // 1_000_000
    start = now - now % (spec.period_s * 1000)
    slug = slug_for(spec, start)

    def rest(req: httpx.Request) -> httpx.Response:
        if not req.url.path.endswith(slug):
            return httpx.Response(404)
        return httpx.Response(200, json={"market": pm_market(slug, start, start + 15 * MIN, ptb="1")})

    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.recv()
        await ws.send(pm_book(slug, bids=[("0.40", "5")], offers=[("0.45", "20")]))  # while warming up
        await asyncio.sleep(1.0)
        await ws.send(pm_book(slug, bids=[("0.40", "5")], offers=[("0.46", "20")]))  # once trading
        await ws.wait_closed()

    def buy(ctx: Context, b: object) -> None:
        ctx.buy(f"polymarket_us:{slug}", Side.YES, 0.46, 10, tif=Tif.IOC)

    strategy = Hooks(book=buy)
    ledger = Ledger(tmp_path / "paper.sqlite", durable=True)
    trader = Engine([Instance("s", strategy, SimBroker())], ledger=ledger, clock_ms=None, strict=False)
    steps: list[str] = []
    sessions: list[RecordSession] = []

    def warmup() -> None:  # in a thread, as the replay of recordings is: still going when the book comes
        for _ in range(100):
            if sessions and any(type(e).__name__ == "Book" for e in sessions[0]._held):
                break
            time.sleep(0.05)
        steps.append("warmed up")

    async def ready() -> None:
        steps.append(f"ready, {len(strategy.seen)} events seen")

    async def go() -> None:
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            feed.config = feed.config.model_copy(update={"url": f"ws://127.0.0.1:{port}"})
            feed.transport = httpx.MockTransport(rest)
            feed.headers = lambda: {}  # type: ignore[method-assign]
            session = RecordSession({"polymarket_us": feed}, tmp_path / "rec", window_s=3600, trader=trader,
                                    warmup=warmup, on_ready=ready)  # fmt: skip
            sessions.append(session)
            await session.run(duration_s=4.0)

    asyncio.run(go())
    books = [a for n, a in strategy.seen if n == "book"]
    assert len(books) == 2 and steps[0] == "warmed up" and steps[1].startswith("ready")
    assert int(steps[1].split()[1]) > 0  # the book from the warm-up had reached the strategy by then
    fills = [(r.qty, r.price) for r in ledger.execute(select(FillRow.qty, FillRow.price))]
    assert fills == [(10.0, 0.46)]  # only the book after it traded; the earlier one's order was refused
