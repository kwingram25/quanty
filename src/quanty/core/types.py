"""Basic trading types. Prices are dollars per contract in [0, 1]; quantities are contracts, which
some venues allow in fractions (Market.qty_step)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from quanty.enums import Action, Liquidity, OrderStatus, Side, Tif

__all__ = ["EPS", "Action", "Liquidity", "Market", "OrderStatus", "Side", "Tif", "from_yes", "to_yes"]

EPS = 1e-9

if TYPE_CHECKING:
    from quanty.core.fees import FeeModel

TickRange = tuple[float, float, float]  # [start, end) of a price band and its tick


@dataclass(frozen=True)
class Market:
    """One tradeable binary contract, e.g. a single 15-minute window of a repeated series."""

    id: str  # "<venue>:<ticker>", unique across venues
    venue: str
    series: str  # "<venue>:<series>", the repeated market this one belongs to
    open_ts: int
    close_ts: int
    tick: float = 0.01
    tick_ranges: tuple[
        TickRange, ...
    ] = ()  # finer ticks in some bands (Kalshi: 0.1¢ below 10¢ and above 90¢)
    qty_step: float = 1.0  # smallest order size increment
    group: str = ""  # correlation group for risk limits; empty = the market alone
    fee: "FeeModel | None" = field(default=None, compare=False)  # None = the broker's venue default
    terms: Mapping[str, object] = field(default_factory=dict, compare=False, hash=False)

    @property
    def risk_group(self) -> str:
        return self.group or self.id

    def tick_at(self, price: float) -> float:
        for start, end, step in self.tick_ranges:
            if start - EPS <= price < end - EPS:
                return step
        return self.tick

    def on_tick(self, price: float) -> bool:
        return _multiple(price, self.tick_at(price))

    def valid_qty(self, qty: float) -> bool:
        return qty > EPS and _multiple(qty, self.qty_step)


def _multiple(x: float, step: float) -> bool:
    n = x / step
    return abs(n - round(n)) < 1e-6


def to_yes(side: Side, action: Action, price: float) -> tuple[bool, float]:
    """An order in YES terms: (buys YES?, YES price). Buying NO at p is selling YES at 1 − p."""
    buys_yes = (side is Side.YES) == (action is Action.BUY)
    return buys_yes, from_yes(side, price)


def from_yes(side: Side, yes_price: float) -> float:
    """A YES price expressed as the price of `side` (the conversion is its own inverse)."""
    return yes_price if side is Side.YES else round(1.0 - yes_price, 9)
