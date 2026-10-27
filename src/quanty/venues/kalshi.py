"""Kalshi: any series by ticker (crypto windows, weather, economics...), books, trades, CF indexes.

Discovery uses the public REST API (no key); the WebSocket needs a key (KALSHI_KEY_ID, KALSHI_SECRET_KEY).
"""

import json
import re
from collections import defaultdict
from datetime import datetime
from typing import Any, ClassVar, Literal

import httpx
import orjson
from pydantic import Field, NonNegativeFloat, NonNegativeInt, PositiveFloat
from sortedcontainers import SortedDict
from websockets.asyncio.client import ClientConnection

from quanty.core.events import Book, Event, IndexValue, MarketOpen, MarketTrade, Settled, Status
from quanty.core.fees import QuadraticFee
from quanty.core.types import Action, Market, Side
from quanty.feeds.base import FeedConfig, Link, Raw, Sink, now_ms
from quanty.venues.auth import KalshiSigner
from quanty.venues.base import VenueConfig, VenueFeed, Window, same_book, top

VENUE = "kalshi"
WS_PATH = "/trade-api/ws/v2"
_BOOK_SEQ = re.compile(r'\{\s*"type"\s*:\s*"(orderbook_\w+)"\s*,\s*"sid"\s*:\s*(\d+)\s*,\s*"seq"\s*:\s*(\d+)')
TERM_FIELDS = (
    "floor_strike",
    "cap_strike",
    "strike_type",
    "event_ticker",
    "title",
    "yes_sub_title",
    "expiration_value",
)


class KalshiConfig(VenueConfig):
    kind: Literal["kalshi"] = "kalshi"
    series: tuple[str, ...] = Field(("KXBTC15M",), description='Series tickers, e.g. "KXBTC15M", "KXHIGHNY"')
    index_ids: tuple[str, ...] = Field((), description='CF Benchmarks indexes to stream, e.g. "BRTI"')
    index_channel: Literal["cfbenchmarks_value_5hz", "cfbenchmarks_value"] = Field(
        "cfbenchmarks_value_5hz", description="5 Hz prints, or 1/s values with trailing 60 s averages"
    )
    poll_s: PositiveFloat = Field(30, description="Discovery poll interval per series")
    strike_poll_s: PositiveFloat = Field(
        2, description="Poll interval while an open market has no strike yet"
    )
    taker_fee_rate: NonNegativeFloat = Field(0.07, description="Quadratic taker fee coefficient")
    maker_fee_rate: NonNegativeFloat = Field(
        0.0, description="Quadratic maker fee coefficient (series-dependent)"
    )
    top_only: tuple[str, ...] = Field(
        (),
        description="Series followed by their best bid and ask only (Kalshi's ticker channel, about once a "
        "second) instead of the full order book: far fewer messages, e.g. for a wide strike ladder",
    )
    near_money: dict[str, NonNegativeInt] = Field(
        {},
        description="Strike ladders: series -> n, subscribe only to each event's strikes still in play "
        "(YES bid < 99¢ and ask > 1¢) plus n either side, e.g. {KXBTCD = 2}; other series get every market",
    )


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def in_play(m: dict[str, Any]) -> bool:
    """A market whose price still says either outcome can happen: not quoted at 99¢+ or 1¢-, and
    not an empty book (Kalshi lists those as bid 0, ask 1)."""
    bid, ask = float(m.get("yes_bid_dollars") or 0), float(m.get("yes_ask_dollars") or 0)
    return bid < 0.99 and ask > 0.01 and not (bid == 0 and ask >= 1)


def market_of(m: dict[str, Any], series: str, cfg: KalshiConfig) -> Market:
    ranges = tuple(
        (float(r["start"]), float(r["end"]), float(r["step"])) for r in m.get("price_ranges") or ()
    )
    terms: dict[str, object] = {k: m[k] for k in TERM_FIELDS if m.get(k) not in (None, "")}
    if series.startswith("KXBTC"):  # Kalshi's BTC markets settle on CF Benchmarks' BRTI
        terms["reference"] = "cfbenchmarks:BRTI"
    return Market(
        id=f"{VENUE}:{m['ticker']}", venue=VENUE, series=f"{VENUE}:{series}",
        open_ts=_ts(m["open_time"]), close_ts=_ts(m["close_time"]), tick=0.01, tick_ranges=ranges,
        qty_step=0.01, group=f"{VENUE}:{m.get('event_ticker') or m['ticker']}",
        fee=QuadraticFee(cfg.taker_fee_rate, cfg.maker_fee_rate, round_up_cents=True), terms=terms,
    )  # fmt: skip


class Kalshi(VenueFeed):
    """Markets keyed "kalshi:<ticker>"; CF indexes keyed "cfbenchmarks:<index>".

    Books are rebuilt from the snapshot and deltas Kalshi sends (bids only on each side: YES
    asks are 1 − NO bids) and emitted when the top book_depth levels change. A sequence gap is
    reported as a Status event keyed "<feed>:seq_gap", since books may be wrong until the next snapshot.

    A wide strike ladder (KXBTCD lists dozens of strikes an hour) can be followed near the money only
    (near_money): discovery still records every market, but only the strikes in play and their
    neighbors get books and trades.
    """

    kind = "kalshi"
    config_type: ClassVar[type[FeedConfig]] = KalshiConfig
    config: KalshiConfig
    default_url = f"wss://api.elections.kalshi.com{WS_PATH}"
    default_rest = "https://api.elections.kalshi.com/trade-api/v2"
    channels: ClassVar[tuple[str, ...]] = ("orderbook_delta", "trade", "ticker")

    def __init__(self, name: str, config: FeedConfig) -> None:
        super().__init__(name, config)
        self.signer: KalshiSigner | None = None
        # transport state
        self._next_poll: dict[str, int] = {}
        self._followed: dict[str, set[str]] = {}  # near_money series -> tickers subscribed so far
        self._ws: ClientConnection | None = None
        self._sids: dict[str, int] = {}
        self._subscribed: frozenset[str] = frozenset()  # markets the open subscriptions cover
        self._cmd = 0
        # parser state
        self._markets: dict[str, Market] = {}
        self._settled: set[str] = set()
        self._books: dict[str, tuple[SortedDict, SortedDict]] = {}  # ticker -> (yes bids, no bids)
        self._last_book: dict[str, Book] = {}
        self._seq: dict[int, int] = {}
        self._trade_ids: dict[str, None] = {}
        self._ticker_marks = tuple(f'"{s}-' for s in self.config.series)  # tickers start "<series>-"

    # --- transport ---

    def headers(self) -> dict[str, str]:
        self.signer = self.signer or KalshiSigner.from_env()
        return self.signer.headers("GET", WS_PATH)

    def subscribe_messages(self, ids: frozenset[str]) -> list[str]:
        self._sids = {}
        self._subscribed = ids
        msgs = [
            self._command("subscribe", {"channels": [ch], "market_tickers": sorted(tickers)})
            for ch, tickers in self._by_channel(ids).items()
            if tickers
        ]
        if self.config.index_ids:
            msgs.append(
                self._command(
                    "subscribe",
                    {"channels": [self.config.index_channel], "index_ids": list(self.config.index_ids)},
                )
            )
        return msgs

    def _by_channel(self, ids: frozenset[str]) -> dict[str, frozenset[str]]:
        """Each market's channels: full books, or the ticker (best bid and ask) for top_only series;
        trades for all."""
        top = frozenset(t for t in ids if t.split("-", 1)[0] in self.config.top_only)
        return {"orderbook_delta": ids - top, "trade": ids, "ticker": top}

    def _command(self, cmd: str, params: dict[str, Any]) -> str:
        self._cmd += 1
        return json.dumps({"id": self._cmd, "cmd": cmd, "params": params})

    def on_connected(self, ws: ClientConnection) -> None:
        self._ws = ws

    def keep(self, text: str) -> bool:
        if '"subscribed"' in text[:40]:
            msg = orjson.loads(text).get("msg") or {}
            if msg.get("channel") in self.channels:
                self._sids[msg["channel"]] = msg["sid"]
        return True

    async def update_in_place(self, added: frozenset[str], removed: frozenset[str]) -> bool:
        """Add and remove markets on the open subscriptions. A subscription is never left with no
        markets: Kalshi then sends every market it lists (for the ticker channel, about 1,000
        messages a second). One that would empty is unsubscribed; one not open is a resubscribe."""
        ws = self._ws
        if ws is None:
            return False
        now = (self._subscribed | added) - removed
        before, after = self._by_channel(self._subscribed), self._by_channel(now)
        cmds: list[tuple[str, dict[str, Any]]] = []
        closing: list[str] = []
        for ch in self.channels:
            adds, drops = after[ch] - before[ch], before[ch] - after[ch]
            if not adds and not drops:
                continue
            sid = self._sids.get(ch)
            if sid is None:
                return False  # not open on this connection: resubscribe instead
            if not after[ch]:
                cmds.append(("unsubscribe", {"sids": [sid]}))
                closing.append(ch)
                continue
            for action, tickers in (("add_markets", adds), ("delete_markets", drops)):  # adds first
                if tickers:
                    cmds.append(
                        (
                            "update_subscription",
                            {"sid": sid, "market_tickers": sorted(tickers), "action": action},
                        )
                    )
        for cmd, params in cmds:
            await ws.send(self._command(cmd, params))
        for ch in closing:
            del self._sids[ch]
        self._subscribed = now
        return True

    async def discover(self, client: httpx.AsyncClient, sink: Sink) -> None:
        now = now_ms()
        for series in self.config.series:
            if now < self._next_poll.get(series, 0):
                continue
            params: dict[str, Any] = {
                "series_ticker": series, "limit": 1000,
                "min_close_ts": (now - int(self.config.results_for_s * 1000)) // 1000,
                "max_close_ts": (now + int(self.config.lookahead_s * 1000)) // 1000,
            }  # fmt: skip
            listed: list[dict[str, Any]] = []
            while True:
                resp = await client.get(f"{self.rest_url()}/markets", params=params)
                resp.raise_for_status()
                body = resp.json()
                sink(self.rest("markets", body, series=series))
                listed += body.get("markets", [])
                if not body.get("cursor"):
                    break
                params["cursor"] = body["cursor"]
            follow = self._near_money(series, listed)
            for m in listed:
                if follow is None or m["ticker"] in follow:
                    self.windows[m["ticker"]] = Window(m["ticker"], _ts(m["open_time"]), _ts(m["close_time"]),
                                                       m.get("result") in ("yes", "no"))  # fmt: skip
            waiting = any(_ts(m["open_time"]) <= now < _ts(m["close_time"]) and m.get("floor_strike") is None
                          and m.get("strike_type") != "custom" for m in listed)  # fmt: skip
            period = self.config.strike_poll_s if waiting else self.config.poll_s
            self._next_poll[series] = now + int(period * 1000)

    def _near_money(self, series: str, markets: list[dict[str, Any]]) -> set[str] | None:
        """Tickers to subscribe in a near_money series (None: not one, subscribe everything).

        Per event, strikes sorted by value: the ones in play, plus n either side for the price to
        move into between polls. A strike stays followed while it is listed, so a price moving back
        and forth doesn't churn subscriptions; markets without a strike are always followed.
        """
        n = self.config.near_money.get(series)
        if n is None:
            return None
        listed = {m["ticker"] for m in markets}
        kept = self._followed[series] = self._followed.get(series, set()) & listed
        ladders: dict[str, list[tuple[float, dict[str, Any]]]] = defaultdict(list)
        for m in markets:
            k = m.get("floor_strike") if m.get("floor_strike") is not None else m.get("cap_strike")
            if k is None:
                kept.add(m["ticker"])
            else:
                ladders[m.get("event_ticker") or m["ticker"]].append((float(k), m))
        for rungs in ladders.values():
            rungs.sort(key=lambda r: r[0])
            live = [i for i, (_, m) in enumerate(rungs) if in_play(m)]
            if live:
                kept.update(m["ticker"] for _, m in rungs[max(0, live[0] - n) : live[-1] + n + 1])
        return kept

    # --- parser ---

    def on_link(self, link: Link) -> None:
        if link.state == "up":  # a new connection sends fresh snapshots
            self._books.clear()
            self._last_book.clear()
            self._seq.clear()

    def parse(self, raw: Raw) -> list[Event]:
        text = raw.text
        gap = self._seq_gap(
            text
        )  # every book message counts, including other series' (sequences are per subscription)
        # only this feed's series: skip other markets' books and trades before decoding them
        skip = "market_ticker" in text and not any(m in text for m in self._ticker_marks)
        events = [] if skip else self._decode(raw, text)
        if gap:
            events.append(Status(ts=raw.t, key=f"{self.name}:seq_gap", text=gap))
        return events

    def carries_data(self, text: str) -> bool:
        return '"cfbenchmarks_value' in text[:80]  # the index channels; everything else is markets

    def _seq_gap(self, text: str) -> str:
        """Description of a sequence gap on an order book subscription, or ""."""
        m = _BOOK_SEQ.match(text)
        if m is None:
            return ""
        kind, sid, seq = m[1], int(m[2]), int(m[3])
        prev = self._seq.get(sid)
        self._seq[sid] = seq
        # seq 1 starts a fresh subscription (also how a reconnect shows in recordings without link events)
        if kind == "orderbook_delta" and prev is not None and seq not in (1, prev + 1):
            return f"sid {sid}: seq {prev} -> {seq}; books may be stale until the next snapshot"
        return ""

    def _decode(self, raw: Raw, text: str) -> list[Event]:
        msg = orjson.loads(text)
        if "rest" in msg:
            if msg["params"].get("series") not in self.config.series:
                return []
            return self._parse_markets(raw, msg)
        kind = msg.get("type")
        if kind in ("orderbook_snapshot", "orderbook_delta"):
            return self._parse_book(raw, msg)
        if kind == "trade":
            return self._parse_trade(raw, msg["msg"])
        if kind == "ticker":
            return self._parse_ticker(raw, msg["msg"])
        if kind == "cfbenchmarks_value_5hz":
            body = msg["msg"]
            key = f"cfbenchmarks:{body['index_id']}"
            return [
                IndexValue(ts=raw.t, src_ts=body.get("source_ts_ms"), key=key, value=float(body["value_usd"]))
            ]
        if kind == "cfbenchmarks_value":
            data = orjson.loads(msg["msg"]["data"])
            return [
                IndexValue(
                    ts=raw.t,
                    src_ts=data["time"],
                    key=f"cfbenchmarks:{data['id']}",
                    value=float(data["value"]),
                )
            ]
        if kind == "error":
            raise ValueError(f"kalshi error: {raw.text[:200]}")
        return []  # subscribed, ok, ticker

    def _parse_markets(self, raw: Raw, msg: dict[str, Any]) -> list[Event]:
        out: list[Event] = []
        series = msg["params"]["series"]
        for m in msg["body"].get("markets", []):
            market = market_of(m, series, self.config)
            prev = self._markets.get(market.id)
            if prev is None or prev.terms != market.terms or prev.close_ts != market.close_ts:
                self._markets[market.id] = market
                out.append(MarketOpen(ts=raw.t, market=market))
            if m.get("result") in ("yes", "no") and market.id not in self._settled:
                self._settled.add(market.id)
                out.append(
                    Settled(
                        ts=raw.t, market_id=market.id, outcome=Side.YES if m["result"] == "yes" else Side.NO
                    )
                )
        return out

    def _parse_book(self, raw: Raw, msg: dict[str, Any]) -> list[Event]:
        body = msg["msg"]
        ticker = body["market_ticker"]
        if msg["type"] == "orderbook_snapshot":
            self._books[ticker] = (
                SortedDict({round(float(p), 4): float(q) for p, q in body.get("yes_dollars_fp", [])}),
                SortedDict({round(float(p), 4): float(q) for p, q in body.get("no_dollars_fp", [])}),
            )
        else:
            books = self._books.get(ticker)
            if books is None:
                return []  # deltas before this connection's snapshot (e.g. a replay starting mid-stream)
            side = books[0] if body["side"] == "yes" else books[1]
            p = round(float(body["price_dollars"]), 4)
            q = round(side.get(p, 0.0) + float(body["delta_fp"]), 6)
            if q > 1e-6:
                side[p] = q
            else:
                side.pop(p, None)
            n = self.config.book_depth  # each side's best levels are its highest prices
            if len(side) >= n and p < side.keys()[-n]:
                return []  # below the top book_depth levels: the book emitted doesn't change
        out: list[Event] = []
        book = self._book(raw.t, body.get("ts_ms"), ticker)
        if not same_book(self._last_book.get(ticker), book):
            self._last_book[ticker] = book
            out.append(book)
        return out

    def _parse_ticker(self, raw: Raw, body: dict[str, Any]) -> list[Event]:
        """A top_only market's best bid and ask (with sizes) as a one-level book."""
        ticker = body["market_ticker"]
        bid, ask = float(body.get("yes_bid_dollars") or 0), float(body.get("yes_ask_dollars") or 0)
        bid_q, ask_q = float(body.get("yes_bid_size_fp") or 0), float(body.get("yes_ask_size_fp") or 0)
        book = Book(
            ts=raw.t,
            # none: Kalshi batches this channel (sent up to ~1 s after the change), so its ts_ms says how
            # stale the best price is, not how late the connection is, which feed lag measures
            src_ts=None,
            market_id=f"{VENUE}:{ticker}",
            bids=((round(bid, 4), bid_q),) if bid > 0 and bid_q > 0 else (),
            asks=((round(ask, 4), ask_q),) if 0 < ask < 1 and ask_q > 0 else (),
        )
        if same_book(self._last_book.get(ticker), book):
            return []
        self._last_book[ticker] = book
        return [book]

    def _book(self, t: int, src: int | None, ticker: str) -> Book:
        yes, no = self._books[ticker]
        n = self.config.book_depth
        asks = tuple((round(1 - p, 4), q) for p, q in top(no, n, reverse=True))
        return Book(
            ts=t, src_ts=src, market_id=f"{VENUE}:{ticker}", bids=top(yes, n, reverse=True), asks=asks
        )

    def _parse_trade(self, raw: Raw, tr: dict[str, Any]) -> list[Event]:
        tid = tr.get("trade_id")
        if tid is not None:
            if tid in self._trade_ids:
                return []
            self._trade_ids[tid] = None
            if len(self._trade_ids) > 10_000:
                self._trade_ids.pop(next(iter(self._trade_ids)))
        # taker_outcome_side is canonical; taker_side (deprecated) is all older recordings have
        taker = Action.BUY if tr.get("taker_outcome_side", tr.get("taker_side")) == "yes" else Action.SELL
        return [
            MarketTrade(
                ts=raw.t,
                src_ts=tr.get("ts_ms"),
                market_id=f"{VENUE}:{tr['market_ticker']}",
                price=float(tr["yes_price_dollars"]),
                qty=float(tr["count_fp"]),
                taker=taker,
            )
        ]
