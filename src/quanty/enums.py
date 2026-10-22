"""Enums shared by the engine and the database models. No imports from the rest of quanty."""

from enum import StrEnum


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
