from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass

from quanty.core.events import Fill
from quanty.core.types import EPS, Action, Side

DAY_MS = 86_400_000


@dataclass
class Position:
    qty: float = 0.0
    cost: float = 0.0  # cash paid for the open qty, fees included

    @property
    def avg_cost(self) -> float:
        return self.cost / self.qty if self.qty else 0.0


@dataclass(frozen=True)
class Settlement:
    instance: str
    market_id: str
    side: Side
    qty: float
    payout: float
    pnl: float
    ts: int


class Portfolio:
    """Positions and realized P&L per strategy instance, built only from fills and settlements.

    A position is long contracts of one side; there is no shorting (sell YES = buy NO is an
    order-level equivalence, not a position one). Buy fees go into cost, so P&L is realized on
    sells and at settlement.
    """

    def __init__(self) -> None:
        self._pos: dict[tuple[str, str, Side], Position] = {}
        self._daily: dict[tuple[str, int], float] = defaultdict(float)
        self.realized: dict[str, float] = defaultdict(float)

    def position(self, instance: str, market_id: str, side: Side) -> Position:
        p = self._pos.get((instance, market_id, side))
        return Position(p.qty, p.cost) if p else Position()

    def open_positions(self, instance: str | None = None) -> Iterator[tuple[str, str, Side, Position]]:
        for (inst, market_id, side), p in self._pos.items():
            if instance is None or inst == instance:
                yield inst, market_id, side, p

    def apply(self, instance: str, fill: Fill) -> float:
        """Book a fill and return its realized P&L (non-zero only for sells)."""
        key = (instance, fill.market_id, fill.side)
        pos = self._pos.setdefault(key, Position())
        if fill.action is Action.BUY:
            pos.qty = round(pos.qty + fill.qty, 9)
            pos.cost += fill.price * fill.qty + fill.fee
            return 0.0
        if fill.qty > pos.qty + EPS:
            raise ValueError(f"{instance}: sell of {fill.qty} {fill.side} exceeds position {pos.qty}")
        basis = pos.avg_cost * fill.qty
        pnl = fill.price * fill.qty - fill.fee - basis
        pos.qty = round(pos.qty - fill.qty, 9)
        pos.cost -= basis
        if pos.qty <= EPS:
            del self._pos[key]
        self._book(instance, fill.ts, pnl)
        return pnl

    def settle(self, market_id: str, outcome: Side, ts: int) -> list[Settlement]:
        """Pay out every open position in the market: $1 per winning contract."""
        out = []
        for key in [k for k in self._pos if k[1] == market_id]:
            instance, _, side = key
            pos = self._pos.pop(key)
            payout = float(pos.qty) if side is outcome else 0.0
            pnl = payout - pos.cost
            self._book(instance, ts, pnl)
            out.append(Settlement(instance, market_id, side, pos.qty, payout, pnl, ts))
        return out

    def restore_settlement(self, instance: str, market_id: str, side: Side, pnl: float, ts: int) -> None:
        """Replay a recorded settlement: the position is gone, its P&L booked (rebuilding from a ledger)."""
        self._pos.pop((instance, market_id, side), None)
        self._book(instance, ts, pnl)

    def daily_pnl(self, instance: str | None, ts: int) -> float:
        """Realized P&L on the UTC day of `ts`, for one instance or all."""
        day = ts // DAY_MS
        return sum(v for (inst, d), v in self._daily.items() if d == day and instance in (None, inst))

    def _book(self, instance: str, ts: int, pnl: float) -> None:
        self.realized[instance] += pnl
        self._daily[(instance, ts // DAY_MS)] += pnl
