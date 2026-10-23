"""Synthetic up/down markets for demos and tests: no keys, no network, deterministic per seed.

An index follows a random walk. Back-to-back windows each ask "will the index be at or above its
value at the open when the window closes?". The market's quotes track the true probability plus
slowly drifting noise, so a strategy with a good model has something to find.
"""

import math
import random
from collections.abc import Iterator
from dataclasses import dataclass

from quanty.core.events import Book, Event, IndexValue, MarketClose, MarketOpen, MarketTrade, Settled
from quanty.core.fees import QuadraticFee
from quanty.core.types import Action, Market, Side

VENUE = "synthetic"
INDEX_KEY = "synthetic:index"
FEES = {VENUE: QuadraticFee(taker_rate=0.07, round_up_cents=True)}


@dataclass(frozen=True)
class Spec:
    hours: float = 6.0
    window_s: int = 300
    start_ms: int = 1_767_225_600_000  # 2026-01-01 00:00 UTC
    index0: float = 100.0
    vol_per_s: float = 0.0005  # log-return sd per second
    noise: float = 0.03  # sd of the market's error vs the true probability
    spread: float = 0.02
    trade_prob: float = 0.3  # chance of a market trade each second
    seed: int = 0

    @property
    def series(self) -> str:
        return f"{VENUE}:updown_{self.window_s // 60}m"


def p_up(index: float, strike: float, seconds_left: float, vol_per_s: float) -> float:
    """P(index at close >= strike) under a driftless log-normal walk."""
    if seconds_left <= 0:
        return 1.0 if index >= strike else 0.0
    z = math.log(index / strike) / (vol_per_s * math.sqrt(seconds_left))
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def events(spec: Spec | None = None) -> Iterator[Event]:
    spec = spec or Spec()
    rng = random.Random(spec.seed)
    index = spec.index0
    n_windows = int(spec.hours * 3600 // spec.window_s)
    for w in range(n_windows):
        open_ts = spec.start_ms + w * spec.window_s * 1000
        close_ts = open_ts + spec.window_s * 1000
        market = Market(
            id=f"{VENUE}:UPDOWN-{open_ts // 1000}",
            venue=VENUE,
            series=spec.series,
            open_ts=open_ts,
            close_ts=close_ts,
            group=spec.series,
            terms={"strike": index},
        )
        strike = index
        yield MarketOpen(ts=open_ts, market=market)
        drift = 0.0
        for s in range(spec.window_s):
            t = open_ts + s * 1000
            index *= math.exp(rng.gauss(0, spec.vol_per_s))
            yield IndexValue(ts=t, src_ts=t, key=INDEX_KEY, value=index)
            drift = 0.95 * drift + rng.gauss(0, spec.noise * math.sqrt(1 - 0.95**2))
            fair = p_up(index, strike, spec.window_s - s, spec.vol_per_s)
            book = _book(market.id, t + 100, fair + drift, spec.spread, rng)
            yield book
            if rng.random() < spec.trade_prob and book.bids and book.asks:
                buy = rng.random() < 0.5
                price = book.asks[0][0] if buy else book.bids[0][0]
                taker = Action.BUY if buy else Action.SELL
                yield MarketTrade(
                    ts=t + 200, market_id=market.id, price=price, qty=rng.randint(1, 50), taker=taker
                )
        index *= math.exp(rng.gauss(0, spec.vol_per_s))
        yield IndexValue(ts=close_ts, src_ts=close_ts, key=INDEX_KEY, value=index)
        yield MarketClose(ts=close_ts, market_id=market.id)
        outcome = Side.YES if index >= strike else Side.NO
        yield Settled(ts=close_ts, market_id=market.id, outcome=outcome)


def events_multi(spec: Spec | None = None, windows: tuple[int, ...] = (300, 900, 3600)) -> Iterator[Event]:
    """Several up/down series on one index at once (5 min, 15 min and 1 h by default): windows open
    on their own clock boundaries, so markets of different lengths are open and closing at different
    times. Deterministic per seed; `events` is unchanged by this."""
    spec = spec or Spec()
    rng = random.Random(spec.seed)
    index = spec.index0
    open_: dict[int, tuple[Market, float, float]] = {}  # window -> (market, strike, drift)
    start_s = spec.start_ms // 1000
    for k in range(int(spec.hours * 3600) + 1):
        now_s = start_s + k
        t = now_s * 1000
        for w in windows:
            held = open_.get(w)
            if held is not None and now_s >= held[0].close_ts // 1000:
                market, strike, _ = held
                yield MarketClose(ts=t, market_id=market.id)
                yield Settled(ts=t, market_id=market.id, outcome=Side.YES if index >= strike else Side.NO)
                del open_[w]
            if now_s % w == 0 and w not in open_:
                length = _length(w)
                market = Market(
                    id=f"{VENUE}:UPDOWN{length.upper()}-{now_s}",
                    venue=VENUE,
                    series=f"{VENUE}:updown_{length}",
                    open_ts=t,
                    close_ts=t + w * 1000,
                    group=f"{VENUE}:index",
                    terms={"strike": index},
                )
                open_[w] = (market, index, 0.0)
                yield MarketOpen(ts=t, market=market)
        index *= math.exp(rng.gauss(0, spec.vol_per_s))
        yield IndexValue(ts=t, src_ts=t, key=INDEX_KEY, value=index)
        trades = []  # after every book of this second, so events stay in time order
        for w, (market, strike, drift) in sorted(open_.items()):
            drift = 0.95 * drift + rng.gauss(0, spec.noise * math.sqrt(1 - 0.95**2))
            open_[w] = (market, strike, drift)
            left = (market.close_ts - t) / 1000
            book = _book(
                market.id, t + 100, p_up(index, strike, left, spec.vol_per_s) + drift, spec.spread, rng
            )
            yield book
            if rng.random() < spec.trade_prob and book.bids and book.asks:
                buy = rng.random() < 0.5
                trades.append(
                    MarketTrade(
                        ts=t + 200,
                        market_id=market.id,
                        price=(book.asks if buy else book.bids)[0][0],
                        qty=rng.randint(1, 50),
                        taker=Action.BUY if buy else Action.SELL,
                    )
                )
        yield from trades


def _length(window_s: int) -> str:
    return f"{window_s // 3600}h" if window_s % 3600 == 0 else f"{window_s // 60}m"


def _book(market_id: str, ts: int, mid: float, spread: float, rng: random.Random) -> Book:
    bid = math.floor((mid - spread / 2) * 100) / 100
    ask = max(bid + 0.01, math.ceil((mid + spread / 2) * 100) / 100)
    bids = tuple(
        (round(bid - i * 0.01, 2), rng.randint(20, 300)) for i in range(3) if bid - i * 0.01 >= 0.01 - 1e-9
    )
    asks = tuple(
        (round(ask + i * 0.01, 2), rng.randint(20, 300)) for i in range(3) if ask + i * 0.01 <= 0.99 + 1e-9
    )
    return Book(ts=ts, market_id=market_id, bids=bids, asks=asks)
