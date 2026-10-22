"""Event types. Every input (venue data, external data, market lifecycle, order results) is one of these.

`ts` is when we see the event (receive time live, the recorded receive time in replay) and orders
the stream; `src_ts` is the source's own timestamp when it has one. Both are epoch milliseconds.
Venue book and trade prices are in YES terms.
"""

from dataclasses import dataclass

from quanty.core.types import Action, Liquidity, Market, OrderStatus, Side, from_yes

Level = tuple[float, int]  # (price, size)


@dataclass(frozen=True, kw_only=True)
class Event:
    ts: int
    src_ts: int | None = None


@dataclass(frozen=True, kw_only=True)
class Clock(Event):
    """Engine heartbeat every clock_ms, for strategies that act on time rather than data."""


# --- market lifecycle ---


@dataclass(frozen=True, kw_only=True)
class MarketOpen(Event):
    market: Market


@dataclass(frozen=True, kw_only=True)
class MarketClose(Event):
    market_id: str


@dataclass(frozen=True, kw_only=True)
class Settled(Event):
    market_id: str
    outcome: Side


# --- venue market data ---


@dataclass(frozen=True, kw_only=True)
class Book(Event):
    market_id: str
    bids: tuple[Level, ...]  # best (highest) first
    asks: tuple[Level, ...]  # best (lowest) first

    def ask(self, side: Side) -> float | None:
        """Price to buy `side` now: YES at the best ask, NO at 1 − the best bid."""
        levels = self.asks if side is Side.YES else self.bids
        return from_yes(side, levels[0][0]) if levels else None

    def bid(self, side: Side) -> float | None:
        """Price to sell `side` now: YES at the best bid, NO at 1 − the best ask."""
        levels = self.bids if side is Side.YES else self.asks
        return from_yes(side, levels[0][0]) if levels else None


@dataclass(frozen=True, kw_only=True)
class MarketTrade(Event):
    market_id: str
    price: float  # YES price
    qty: int
    taker: Action | None = None  # BUY = the taker bought YES


# --- external data (feeds) ---


@dataclass(frozen=True, kw_only=True)
class DataEvent(Event):
    key: str  # "<feed>:<instrument>", e.g. "coinbase:BTC-USD"


@dataclass(frozen=True, kw_only=True)
class Quote(DataEvent):
    bid: float
    ask: float
    bid_size: float | None = None
    ask_size: float | None = None


@dataclass(frozen=True, kw_only=True)
class Trade(DataEvent):
    price: float
    size: float
    taker: Action | None = None


@dataclass(frozen=True, kw_only=True)
class IndexValue(DataEvent):
    value: float


@dataclass(frozen=True, kw_only=True)
class Observation(DataEvent):
    value: float
    unit: str = ""


@dataclass(frozen=True, kw_only=True)
class Forecast(DataEvent):
    value: float
    valid_ts: int
    model: str = ""


@dataclass(frozen=True, kw_only=True)
class Release(DataEvent):
    value: float
    consensus: float | None = None


@dataclass(frozen=True, kw_only=True)
class Status(DataEvent):
    text: str


# --- order results (broker -> engine) ---


@dataclass(frozen=True, kw_only=True)
class OrderUpdate(Event):
    order_id: str
    status: OrderStatus
    filled: int
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class Fill(Event):
    order_id: str
    market_id: str
    side: Side
    action: Action
    price: float  # in the order's own side terms
    qty: int
    fee: float  # negative = rebate
    liquidity: Liquidity


@dataclass(frozen=True, kw_only=True)
class BrokerEvent(Event):
    """Internal to one broker (e.g. a simulated order reaching the venue); routed only to it."""

    broker: str
