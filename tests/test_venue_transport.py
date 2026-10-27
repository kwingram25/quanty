"""Venue transports against a mocked REST API and a local WebSocket server."""

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from venue_msgs import MIN, k_snapshot, kalshi_market, pm_book, pm_market, pm_trade
from websockets.asyncio.server import ServerConnection, serve

from quanty import feeds
from quanty.core import MarketOpen, MarketTrade
from quanty.feeds.base import Link, Raw
from quanty.venues.kalshi import Kalshi
from quanty.venues.polymarket_us import PolymarketUS, slug_for


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def mock_rest(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def run_for(feed: Any, seconds: float) -> list[Raw | Link]:
    items: list[Raw | Link] = []
    task = asyncio.create_task(feed.run(items.append))
    await asyncio.sleep(seconds)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return items


def test_kalshi_discovers_paginates_and_updates_subscriptions_in_place() -> None:
    now = now_ms()
    a = kalshi_market("KX-A", now - MIN, now + 15 * MIN)
    b = kalshi_market("KX-B", now + MIN, now + 16 * MIN)
    polls = 0

    def rest(req: httpx.Request) -> httpx.Response:
        nonlocal polls
        polls += 1
        if req.url.params.get("cursor") == "page2":
            return httpx.Response(200, json={"markets": [b] if polls > 2 else [], "cursor": ""})
        return httpx.Response(200, json={"markets": [a], "cursor": "page2"})

    received: list[list[dict[str, Any]]] = []

    async def handler(ws: ServerConnection) -> None:
        msgs: list[dict[str, Any]] = []
        received.append(msgs)
        async for text in ws:
            cmd = json.loads(text)
            msgs.append(cmd)
            if cmd["cmd"] == "subscribe":
                ch = cmd["params"]["channels"][0]
                await ws.send(
                    json.dumps(
                        {"type": "subscribed", "id": cmd["id"], "msg": {"channel": ch, "sid": cmd["id"]}}
                    )
                )
                if ch == "orderbook_delta":
                    await ws.send(k_snapshot("KX-A", yes=[("0.40", "1")], no=[("0.50", "1")], sid=cmd["id"]))

    async def go() -> list[Raw | Link]:
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            feed = feeds.make("kalshi", {"url": f"ws://127.0.0.1:{port}", "poll_s": 0.5})
            assert isinstance(feed, Kalshi)
            feed.transport = mock_rest(rest)
            feed.headers = lambda: {}  # type: ignore[method-assign]
            return await run_for(feed, 3.5)

    items = asyncio.run(go())
    assert len(received) == 1  # one connection: B was added in place
    cmds = received[0]
    subs = [c for c in cmds if c["cmd"] == "subscribe"]
    assert {c["params"]["channels"][0] for c in subs} == {"orderbook_delta", "trade"}
    assert all(c["params"]["market_tickers"] == ["KX-A"] for c in subs)
    adds = [c for c in cmds if c["cmd"] == "update_subscription"]
    assert {c["params"]["sid"] for c in adds} == {c["id"] for c in subs}
    assert all(
        c["params"] == {**c["params"], "market_tickers": ["KX-B"], "action": "add_markets"} for c in adds
    )
    rest_raws = [i for i in items if isinstance(i, Raw) and i.text.startswith('{"rest"')]
    assert len(rest_raws) >= 4  # two pages per poll
    assert [i.state for i in items if isinstance(i, Link)] == ["up"]


def test_kalshi_near_money_follows_strikes_in_play_and_their_neighbors() -> None:
    now = now_ms()
    quotes = {"T1": (0.99, 1.0), "T2": (0.99, 1.0), "T3": (0.97, 0.98), "T4": (0.40, 0.45),
              "T5": (0.01, 0.02), "T6": (0.0, 0.01), "T7": (0.0, 0.01), "T8": (0.0, 1.0)}  # fmt: skip

    def ladder() -> list[dict[str, Any]]:
        return [
            kalshi_market(f"KXL-H1-{t}", now - MIN, now + 59 * MIN, event_ticker="KXL-H1",
                          strike_type="greater", floor_strike=1000.0 + 100 * int(t[1]),
                          yes_bid_dollars=f"{b:.4f}", yes_ask_dollars=f"{a:.4f}")
            for t, (b, a) in quotes.items()
        ]  # fmt: skip

    def rest(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"markets": ladder(), "cursor": ""})

    feed = feeds.make("kalshi", {"series": ["KXL"], "near_money": {"KXL": 1}})
    assert isinstance(feed, Kalshi)
    recorded: list[Raw | Link] = []

    async def poll() -> frozenset[str]:
        feed._next_poll.clear()
        async with httpx.AsyncClient(transport=mock_rest(rest)) as client:
            await feed.discover(client, recorded.append)
        return feed.wanted(now_ms())

    # in play: T3..T5 (T1, T2 at 99¢, T6, T7 at 1¢, T8 an empty book); one neighbor each side
    assert asyncio.run(poll()) == {f"KXL-H1-{t}" for t in ("T2", "T3", "T4", "T5", "T6")}
    quotes.update(T3=(0.99, 1.0), T4=(0.99, 1.0), T5=(0.99, 1.0), T6=(0.97, 0.98), T7=(0.40, 0.45))
    # the price moved up two strikes: T8 joins, and the strikes followed before stay
    assert asyncio.run(poll()) == {f"KXL-H1-{t}" for t in ("T2", "T3", "T4", "T5", "T6", "T7", "T8")}
    # every listed market is still recorded, followed or not, so replays see the whole ladder
    events = [e for r in recorded if isinstance(r, Raw) for e in feed.parse(r)]
    assert len({e.market.id for e in events if isinstance(e, MarketOpen)}) == 8


def test_polymarket_replaces_connection_with_overlap_and_dedups_trades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(PolymarketUS, "overlap_s", 0.3)
    feed = feeds.make(
        "polymarket_us", {"poll_s": 1, "strike_poll_s": 1, "results_for_s": 1, "lookahead_s": 900}
    )
    assert isinstance(feed, PolymarketUS)
    spec = feed.config.series[0]
    now = now_ms()
    start = now - now % (spec.period_s * 1000)
    cur, nxt = slug_for(spec, start), slug_for(spec, start + 15 * MIN)
    listed = {cur}

    def rest(req: httpx.Request) -> httpx.Response:
        slug = req.url.path.rsplit("/", 1)[1]
        if slug not in listed:
            listed.add(nxt)  # the next window gets listed after the first round of polls
            return httpx.Response(404)
        s = start if slug == cur else start + 15 * MIN
        return httpx.Response(200, json={"market": pm_market(slug, s, s + 15 * MIN, ptb="1")})

    connections: list[list[str]] = []

    async def handler(ws: ServerConnection) -> None:
        sub = json.loads(await ws.recv())["subscribe"]
        await ws.recv()
        connections.append(sub["marketSlugs"])
        await ws.send(pm_book(cur, bids=[("0.5", "1")], offers=[("0.6", "1")]))
        await ws.send(pm_trade(cur, "T1", "0.55", "1", "ORDER_SIDE_BUY"))  # both connections send it
        await ws.wait_closed()

    async def go() -> list[Raw | Link]:
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            feed.config = feed.config.model_copy(update={"url": f"ws://127.0.0.1:{port}"})
            feed.transport = mock_rest(rest)
            feed.headers = lambda: {}  # type: ignore[method-assign]
            return await run_for(feed, 4.5)

    items = asyncio.run(go())
    assert connections[0] == [cur] and connections[1] == sorted([cur, nxt])
    links = [i.state for i in items if isinstance(i, Link)]
    assert links == ["up", "up"]  # the replaced connection closes without a "down"
    fresh = feeds.make("polymarket_us", {})
    events = [e for i in items if isinstance(i, Raw) for e in fresh.parse(i)]
    assert sum(isinstance(e, MarketTrade) for e in events) == 1
    assert {e.market.id for e in events if isinstance(e, MarketOpen)} == {
        f"polymarket_us:{cur}",
        f"polymarket_us:{nxt}",
    }
