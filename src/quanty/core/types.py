"""Basic trading types. Prices are dollars per contract in [0, 1]; quantities are whole contracts."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from quanty.enums import Action, Liquidity, OrderStatus, Side, Tif

__all__ = ["EPS", "Action", "Liquidity", "Market", "OrderStatus", "Side", "Tif", "from_yes", "to_yes"]

EPS = 1e-9


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
