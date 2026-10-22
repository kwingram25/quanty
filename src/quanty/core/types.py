"""Basic trading types. Prices are dollars per contract in [0, 1]; quantities are whole contracts."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

EPS = 1e-9


class Side(StrEnum):
    YES = "yes"
    NO = "no"

    @property
    def other(self) -> "Side":
        return Side.NO if self is Side.YES else Side.YES


class Action(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Tif(StrEnum):
    GTC = "gtc"  # rest until filled, canceled or the market closes
    IOC = "ioc"  # take what is available on arrival, cancel the rest


class OrderStatus(StrEnum):
    PENDING = "pending"  # sent, not yet acknowledged by the venue
    OPEN = "open"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"  # the market closed while the order rested

    @property
    def done(self) -> bool:
        return self not in (OrderStatus.PENDING, OrderStatus.OPEN)


class Liquidity(StrEnum):
    MAKER = "maker"
    TAKER = "taker"


@dataclass(frozen=True)
class Market:
    """One tradeable binary contract, e.g. a single 15-minute window of a repeated series."""

    id: str  # "<venue>:<ticker>", unique across venues
    venue: str
    series: str  # "<venue>:<series>", the repeated market this one belongs to
    open_ts: int
    close_ts: int
    tick: float = 0.01
    group: str = ""  # correlation group for risk limits; empty = the market alone
    terms: Mapping[str, object] = field(default_factory=dict, compare=False, hash=False)

    @property
    def risk_group(self) -> str:
        return self.group or self.id


def to_yes(side: Side, action: Action, price: float) -> tuple[bool, float]:
    """An order in YES terms: (buys YES?, YES price). Buying NO at p is selling YES at 1 − p."""
    buys_yes = (side is Side.YES) == (action is Action.BUY)
    return buys_yes, from_yes(side, price)


def from_yes(side: Side, yes_price: float) -> float:
    """A YES price expressed as the price of `side` (the conversion is its own inverse)."""
    return yes_price if side is Side.YES else round(1.0 - yes_price, 9)
