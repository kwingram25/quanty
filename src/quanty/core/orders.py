from dataclasses import dataclass

from quanty.core.types import Action, OrderStatus, Side, Tif, to_yes


@dataclass
class Order:
    """An order as the engine tracks it. Only the engine mutates it, from broker events."""

    id: str
    instance: str
    market_id: str
    side: Side
    action: Action
    price: float  # limit, in `side` terms
    qty: float
    tif: Tif = Tif.GTC
    post_only: bool = False
    tag: str = ""
    created_ts: int = 0
    status: OrderStatus = OrderStatus.PENDING
    filled: float = 0.0
    reason: str = ""

    @property
    def remaining(self) -> float:
        return max(0.0, round(self.qty - self.filled, 9))

    @property
    def live(self) -> bool:
        return not self.status.done

    def in_yes(self) -> tuple[bool, float]:
        return to_yes(self.side, self.action, self.price)
