"""Kalshi: any series by ticker (crypto windows, weather, economics...), books, trades, CF indexes.

Discovery uses the public REST API (no key); the WebSocket needs a key (KALSHI_KEY_ID, KALSHI_SECRET_KEY).
"""

import json
import re
from datetime import datetime
from typing import Any, ClassVar, Literal

import httpx
import orjson
from pydantic import Field, NonNegativeFloat, PositiveFloat
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


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def market_of(m: dict[str, Any], series: str, cfg: KalshiConfig) -> Market:
    ranges = tuple(
        (float(r["start"]), float(r["end"]), float(r["step"])) for r in m.get("price_ranges") or ()
    )
    terms = {k: m[k] for k in TERM_FIELDS if m.get(k) not in (None, "")}
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
    """

    kind = "kalshi"
    config_type: ClassVar[type[FeedConfig]] = KalshiConfig
    config: KalshiConfig
    default_url = f"wss://api.elections.kalshi.com{WS_PATH}"
    default_rest = "https://api.elections.kalshi.com/trade-api/v2"
    channels: ClassVar[tuple[str, ...]] = ("orderbook_delta", "trade")

    def __init__(self, name: str, config: FeedConfig) -> None:
        super().__init__(name, config)
        self.signer: KalshiSigner | None = None
        # transport state
        self._next_poll: dict[str, int] = {}
        self._ws: ClientConnection | None = None
        self._sids: dict[str, int] = {}
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
        msgs = [
            self._command("subscribe", {"channels": [ch], "market_tickers": sorted(ids)})
            for ch in self.channels
        ]
        if self.config.index_ids:
            msgs.append(
                self._command(
                    "subscribe",
                    {"channels": [self.config.index_channel], "index_ids": list(self.config.index_ids)},
                )
            )
        return msgs

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
        ws = self._ws
        if ws is None or set(self._sids) != set(self.channels):
            return False
        for ch in self.channels:
            for action, tickers in (("add_markets", added), ("delete_markets", removed)):
                if tickers:
                    params = {"sid": self._sids[ch], "market_tickers": sorted(tickers), "action": action}
                    await ws.send(self._command("update_subscription", params))
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
            while True:
                resp = await client.get(f"{self.rest_url()}/markets", params=params)
                resp.raise_for_status()
                body = resp.json()
                sink(self.rest("markets", body, series=series))
                for m in body.get("markets", []):
                    self.windows[m["ticker"]] = Window(m["ticker"], _ts(m["open_time"]), _ts(m["close_time"]),
                                                       m.get("result") in ("yes", "no"))  # fmt: skip
                if not body.get("cursor"):
                    break
                params["cursor"] = body["cursor"]
            waiting = any(_ts(m["open_time"]) <= now < _ts(m["close_time"]) and m.get("floor_strike") is None
                          and m.get("strike_type") != "custom" for m in body.get("markets", []))  # fmt: skip
            period = self.config.strike_poll_s if waiting else self.config.poll_s
            self._next_poll[series] = now + int(period * 1000)

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
        out: list[Event] = []
        book = self._book(raw.t, body.get("ts_ms"), ticker)
        if not same_book(self._last_book.get(ticker), book):
            self._last_book[ticker] = book
            out.append(book)
        return out

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
        taker = Action.BUY if tr.get("taker_side") == "yes" else Action.SELL
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
