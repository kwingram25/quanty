"""Polymarket US (polymarket.us): repeated markets found by slug template, books and trades.

Discovery uses the public gateway (no key); the WebSocket needs a key (POLYMARKET_KEY_ID,
POLYMARKET_SECRET_KEY).
"""

import json
import time
from typing import Any, ClassVar, Literal

import httpx
import orjson
from pydantic import Field, NonNegativeFloat, PositiveInt

from quanty.core.events import Book, Event, MarketOpen, MarketTrade, Settled
from quanty.core.fees import QuadraticFee
from quanty.core.types import Action, Market, Side
from quanty.core.validate import ConfigModel
from quanty.feeds.base import FeedConfig, Link, Raw, Sink, now_ms
from quanty.feeds.parse import iso_ms
from quanty.venues.auth import PolymarketSigner
from quanty.venues.base import VenueConfig, VenueFeed, Window, same_book

VENUE = "polymarket_us"
WS_PATH = "/v1/ws/markets"
RESULT_DELAYS_S = (30, 90, 300, 900, 1800, 3600, 7200, 10800)  # results can be late after halts


class SeriesSpec(ConfigModel):
    name: str = Field(description='Series name in market ids, e.g. "btc_updown_15m"')
    slug_prefix: str = Field(description='Slug before the window start, e.g. "cpc-btc-updown-15m"')
    period_s: PositiveInt = Field(description="Window length; windows start on multiples of it (UTC)")


class PolymarketUSConfig(VenueConfig):
    kind: Literal["polymarket_us"] = "polymarket_us"
    series: tuple[SeriesSpec, ...] = (
        SeriesSpec(name="btc_updown_15m", slug_prefix="cpc-btc-updown-15m", period_s=900),
        SeriesSpec(name="btc_updown_1h", slug_prefix="cpc-btc-updown-1h", period_s=3600),
    )
    poll_s: PositiveInt = Field(60, description="How often to re-check upcoming windows")
    strike_poll_s: PositiveInt = Field(2, description="How often to check an open window for its strike")
    maker_rebate_rate: NonNegativeFloat = Field(0.0125, description="Quadratic maker rebate coefficient")


def slug_for(spec: SeriesSpec, start_ms: int) -> str:
    return time.strftime(f"{spec.slug_prefix}-%Y-%m-%d-%H%Mz", time.gmtime(start_ms / 1000)).lower()


def _price(v: Any) -> float | None:
    return float(v["value"]) if isinstance(v, dict) and v.get("value") not in (None, "") else None


def outcome_of(m: dict[str, Any]) -> Side | None:
    terms = m.get("assetPriceTerms") or {}
    settle, strike = _price(terms.get("settlementPrice")), _price(terms.get("priceToBeat"))
    if settle is not None and strike is not None:
        return Side.YES if settle >= strike else Side.NO
    if m.get("status") == "MARKET_STATUS_RESOLVED":  # resolved without prices, e.g. after a halt
        prices = m.get("outcomePrices")
        prices = json.loads(prices) if isinstance(prices, str) else prices
        if prices in (["1", "0"], ["0", "1"]):
            return Side.YES if prices[0] == "1" else Side.NO
    return None


class PolymarketUS(VenueFeed):
    """Markets keyed "polymarket_us:<slug>", in series "polymarket_us:<series name>".

    YES is the first outcome ("Up" for up/down markets). Book updates are full snapshots; trade
    prices are YES prices whatever the taker traded, and the taker's side is its YES-terms side.
    Trades are de-duplicated by id, since connections overlap when subscriptions change.
    """

    kind = "polymarket_us"
    config_type: ClassVar[type[FeedConfig]] = PolymarketUSConfig
    config: PolymarketUSConfig
    default_url = f"wss://api.polymarket.us{WS_PATH}"
    default_rest = "https://gateway.polymarket.us"

    def __init__(self, name: str, config: FeedConfig) -> None:
        super().__init__(name, config)
        self.signer: PolymarketSigner | None = None
        self._spec = {s.slug_prefix: s for s in self.config.series}
        # transport state
        self._next_check: dict[str, int] = {}
        self._strikes: dict[str, bool] = {}
        # parser state
        self._markets: dict[str, Market] = {}
        self._settled: set[str] = set()
        self._last_book: dict[str, Book] = {}
        self._fresh: set[str] = set()  # markets whose post-connect snapshot has arrived
        self._trade_ids: dict[str, None] = {}

    # --- transport ---

    def headers(self) -> dict[str, str]:
        self.signer = self.signer or PolymarketSigner.from_env()
        return self.signer.headers("GET", WS_PATH)

    def keep(self, text: str) -> bool:
        return not text.startswith('{"heartbeat"')

    def subscribe_messages(self, ids: frozenset[str]) -> list[str]:
        slugs = sorted(ids)
        return [
            json.dumps({"subscribe": {"requestId": rid, "subscriptionType": kind, "marketSlugs": slugs}})
            for rid, kind in (("md", "SUBSCRIPTION_TYPE_MARKET_DATA"), ("tr", "SUBSCRIPTION_TYPE_TRADE"))
        ]

    def _candidates(self, now: int) -> list[tuple[str, int, int]]:
        """(slug, start, end) of windows from results_for_s ago to lookahead_s ahead."""
        out = []
        for spec in self.config.series:
            step = spec.period_s * 1000
            first = (now - int(self.config.results_for_s * 1000)) // step * step
            last = now + int(self.config.lookahead_s * 1000)
            out += [(slug_for(spec, t), t, t + step) for t in range(first, last, step)]
        return out

    async def discover(self, client: httpx.AsyncClient, sink: Sink) -> None:
        now = now_ms()
        for slug, start, end in self._candidates(now):
            if not self._due_check(slug, start, end, now):
                continue
            resp = await client.get(f"{self.rest_url()}/v1/market/slug/{slug}")
            if resp.status_code == 404:
                continue  # not listed (yet, or skipped for maintenance)
            resp.raise_for_status()
            body = resp.json()
            sink(self.rest("market", body, slug=slug))
            m = body.get("market") or {}
            if m:
                self.windows[slug] = Window(slug, start, end, outcome_of(m) is not None)
                self._strikes[slug] = _price((m.get("assetPriceTerms") or {}).get("priceToBeat")) is not None

    def _due_check(self, slug: str, start: int, end: int, now: int) -> bool:
        """Upcoming: every poll_s. Open without its strike yet: every strike_poll_s. Closed: on the
        result retry schedule until settled."""
        w = self.windows.get(slug)
        if (w is not None and w.settled) or now < self._next_check.get(slug, 0):
            return False
        if end <= now:
            later = [end + d * 1000 for d in RESULT_DELAYS_S if end + d * 1000 > now]
            self._next_check[slug] = later[0] if later else 2**62
            return True
        open_without_strike = start <= now and not self._strikes.get(slug, False)
        period = self.config.strike_poll_s if open_without_strike else self.config.poll_s
        self._next_check[slug] = now + period * 1000
        return True

    # --- parser ---

    def on_link(self, link: Link) -> None:
        if link.state == "up":
            self._fresh.clear()

    holds_data = False  # books, trades and discovery only (its strikes come with the markets)

    def carries_data(self, text: str) -> bool:
        return False

    def parse(self, raw: Raw) -> list[Event]:
        msg = orjson.loads(raw.text)
        if "rest" in msg:
            return self._parse_market(raw, msg["body"].get("market") or {})
        if "marketData" in msg:
            return self._parse_book(raw, msg["marketData"])
        if "trade" in msg:
            return self._parse_trade(raw, msg["trade"])
        if "error" in msg:
            raise ValueError(f"polymarket_us error: {raw.text[:200]}")
        return []

    def _series_of(self, slug: str) -> str:
        prefix = slug.rsplit("-", 4)[0]
        spec = self._spec.get(prefix)
        return f"{VENUE}:{spec.name if spec else prefix}"

    def _parse_market(self, raw: Raw, m: dict[str, Any]) -> list[Event]:
        if not m:
            return []
        slug = m["slug"]
        terms_in = m.get("assetPriceTerms") or {}
        start = iso_ms(terms_in.get("windowStart") or m["gameStartTime"])
        end = iso_ms(terms_in["windowEnd"]) if terms_in.get("windowEnd") else start + 900_000
        terms = {
            "price_to_beat": _price(terms_in.get("priceToBeat")), "index": terms_in.get("indexSymbol"),
            "horizon": terms_in.get("horizon"), "settlement_price": _price(terms_in.get("settlementPrice")),
            "status": m.get("status"), "slug": slug,
            "title": m.get("title") or m.get("question"), "subtitle": m.get("subtitle"),
            "reference": f"cfbenchmarks:{terms_in['indexSymbol']}" if terms_in.get("indexSymbol") else None,
        }  # fmt: skip
        market = Market(
            id=f"{VENUE}:{slug}", venue=VENUE, series=self._series_of(slug), open_ts=start, close_ts=end,
            tick=float(m.get("orderPriceMinTickSize") or 0.01), qty_step=float(m.get("minimumTradeQty") or 1),
            group=f"{VENUE}:{terms['index'] or slug}",
            fee=QuadraticFee(float(m.get("feeCoefficient") or 0), -self.config.maker_rebate_rate),
            terms={k: v for k, v in terms.items() if v is not None},
        )  # fmt: skip
        out: list[Event] = []
        prev = self._markets.get(market.id)
        if prev is None or prev.terms != market.terms or prev.close_ts != market.close_ts:
            self._markets[market.id] = market
            out.append(MarketOpen(ts=raw.t, market=market))
        outcome = outcome_of(m)
        if outcome is not None and market.id not in self._settled:
            self._settled.add(market.id)
            out.append(Settled(ts=raw.t, market_id=market.id, outcome=outcome))
        return out

    def _parse_book(self, raw: Raw, md: dict[str, Any]) -> list[Event]:
        n = self.config.book_depth
        bids = sorted(
            ((float(x["px"]["value"]), float(x["qty"])) for x in md.get("bids") or ()), reverse=True
        )
        asks = sorted((float(x["px"]["value"]), float(x["qty"])) for x in md.get("offers") or ())
        mid = f"{VENUE}:{md['marketSlug']}"
        # the first snapshot after connecting is timestamped with the market's last change: not lag
        src = iso_ms(md["transactTime"]) if md.get("transactTime") and mid in self._fresh else None
        self._fresh.add(mid)
        book = Book(
            ts=raw.t,
            src_ts=src,
            market_id=f"{VENUE}:{md['marketSlug']}",
            bids=tuple(bids[:n]),
            asks=tuple(asks[:n]),
        )
        if same_book(self._last_book.get(book.market_id), book):
            return []
        self._last_book[book.market_id] = book
        return [book]

    def _parse_trade(self, raw: Raw, tr: dict[str, Any]) -> list[Event]:
        tid = tr.get("id")
        if tid is not None:
            if tid in self._trade_ids:
                return []
            self._trade_ids[tid] = None
            if len(self._trade_ids) > 10_000:
                self._trade_ids.pop(next(iter(self._trade_ids)))
        side = (tr.get("taker") or {}).get("side")
        taker = Action.BUY if side == "ORDER_SIDE_BUY" else Action.SELL if side == "ORDER_SIDE_SELL" else None
        return [MarketTrade(ts=raw.t, src_ts=iso_ms(tr["tradeTime"]) if tr.get("tradeTime") else None,
                            market_id=f"{VENUE}:{tr['marketSlug']}", price=float(tr["price"]["value"]),
                            qty=float(tr["quantity"]["value"]), taker=taker)]  # fmt: skip
