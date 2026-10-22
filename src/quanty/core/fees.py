import math
from dataclasses import dataclass
from typing import Protocol

from quanty.core.types import Liquidity


class FeeModel(Protocol):
    def fee(self, price: float, qty: float, liquidity: Liquidity) -> float:
        """Fee in dollars for one fill; negative is a rebate."""
        ...


@dataclass(frozen=True)
class NoFee:
    def fee(self, price: float, qty: float, liquidity: Liquidity) -> float:
        return 0.0


@dataclass(frozen=True)
class QuadraticFee:
    """rate × qty × p × (1 − p), the shape Kalshi and Polymarket US both publish.

    A negative maker_rate is a maker rebate. round_up_cents rounds each positive fee up to a cent.
    """

    taker_rate: float
    maker_rate: float = 0.0
    round_up_cents: bool = False

    def fee(self, price: float, qty: float, liquidity: Liquidity) -> float:
        rate = self.taker_rate if liquidity is Liquidity.TAKER else self.maker_rate
        raw = rate * qty * price * (1.0 - price)
        if self.round_up_cents and raw > 0:
            return math.ceil(round(raw * 100, 6)) / 100
        return raw
