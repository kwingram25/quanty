"""Record of every order, fill, settlement and strategy log line, per strategy instance.

Writes are buffered and flushed in bulk (fast backtests); `durable=True` flushes on every write
(live). Reads flush first, so they always see everything written.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Executable, Result, case, func, insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from quanty import db
from quanty.core.events import Fill
from quanty.core.orders import Order
from quanty.core.portfolio import Settlement
from quanty.core.types import Action, Market, OrderStatus
from quanty.db.models import FillRow, LogRow, MarketRow, OrderRow, SettlementRow

FLUSH_EVERY = 5000


@dataclass(frozen=True)
class Summary:
    instance: str
    orders: int
    rejected: int
    fills: int
    contracts: float  # bought
    fees: float
    pnl: float  # realized: sells + settlements
    wins: int  # settled positions with positive P&L
    losses: int


class Ledger:
    def __init__(self, path: str | Path = ":memory:", durable: bool = False) -> None:
        self.engine = db.connect(path)
        self.durable = durable
        self._orders: dict[str, dict[str, Any]] = {}  # latest state per order id
        self._markets: dict[str, dict[str, Any]] = {}  # latest description per market id
        self._rows: dict[type[Any], list[dict[str, Any]]] = {FillRow: [], SettlementRow: [], LogRow: []}
        self._pending = 0

    # --- writes ---

    def order(self, o: Order) -> None:
        self._orders[o.id] = {
            "id": o.id,
            "instance": o.instance,
            "market_id": o.market_id,
            "side": o.side,
            "action": o.action,
            "price": o.price,
            "qty": o.qty,
            "tif": o.tif,
            "post_only": o.post_only,
            "tag": o.tag,
            "created_ts": o.created_ts,
            "status": o.status,
            "filled": o.filled,
            "reason": o.reason,
        }
        self._wrote()

    def market(self, m: Market) -> None:
        d = m.describe()
        self._markets[m.id] = {
            "id": m.id,
            "venue": m.venue,
            "series": m.series,
            "open_ts": m.open_ts,
            "close_ts": m.close_ts,
            "title": str(d["title"]),
            "subtitle": str(d["subtitle"]),
            "event": str(d["event"]),
        }
        self._wrote()

    def fill(self, instance: str, f: Fill, realized: float) -> None:
        self._add(
            FillRow,
            ts=f.ts,
            instance=instance,
            order_id=f.order_id,
            market_id=f.market_id,
            side=f.side,
            action=f.action,
            price=f.price,
            qty=f.qty,
            fee=f.fee,
            liquidity=f.liquidity,
            realized=realized,
        )

    def settlement(self, s: Settlement) -> None:
        self._add(
            SettlementRow,
            ts=s.ts,
            instance=s.instance,
            market_id=s.market_id,
            side=s.side,
            qty=s.qty,
            payout=s.payout,
            pnl=s.pnl,
        )

    def log(
        self,
        ts: int,
        instance: str,
        msg: str,
        fields: Mapping[str, object] | None = None,
        level: str = "info",
    ) -> None:
        self._add(LogRow, ts=ts, instance=instance, level=level, msg=msg, fields=_jsonable(fields or {}))

    def commit(self) -> None:
        """Write everything buffered."""
        if not self._pending:
            return
        with self.engine.begin() as conn:
            if self._markets:
                ins = sqlite_insert(MarketRow)
                cols = {c.name: ins.excluded[c.name] for c in MarketRow.__table__.columns if c.name != "id"}
                conn.execute(
                    ins.on_conflict_do_update(index_elements=["id"], set_=cols), list(self._markets.values())
                )
            if self._orders:
                stmt = sqlite_insert(OrderRow)
                updates = {
                    c.name: stmt.excluded[c.name] for c in OrderRow.__table__.columns if c.name != "id"
                }
                conn.execute(
                    stmt.on_conflict_do_update(index_elements=["id"], set_=updates),
                    list(self._orders.values()),
                )
            for model, rows in self._rows.items():
                if rows:
                    conn.execute(insert(model), rows)
        self._orders.clear()
        self._markets.clear()
        for rows in self._rows.values():
            rows.clear()
        self._pending = 0

    def close(self) -> None:
        self.commit()
        self.engine.dispose()

    # --- reads ---

    def execute(self, stmt: Executable) -> Result[Any]:
        """Run a query, e.g. select(FillRow).where(...), against everything written so far."""
        self.commit()
        with Session(self.engine) as session:
            return session.execute(stmt).freeze()()  # detached, usable after the session closes

    def summary(self) -> list[Summary]:
        return summarize(self.execute)

    # --- helpers ---

    def _add(self, model: type[Any], **row: Any) -> None:
        self._rows[model].append(row)
        self._wrote()

    def _wrote(self) -> None:
        self._pending += 1
        if self.durable or self._pending >= FLUSH_EVERY:
            self.commit()


def _jsonable(fields: Mapping[str, object]) -> dict[str, object]:
    return {str(k): _json_value(v) for k, v in fields.items()}


def _json_value(v: object) -> object:
    """JSON values as they are (lists and dicts too, recursively); anything else as its repr."""
    if isinstance(v, str | int | float | bool | None):
        return v
    if isinstance(v, list | tuple):
        return [_json_value(x) for x in v]
    if isinstance(v, Mapping):
        return {str(k): _json_value(x) for k, x in v.items()}
    return repr(v)


def summarize(execute: Callable[[Any], Any]) -> list[Summary]:
    """Per-instance totals from a ledger database (Ledger.execute, or any connection's execute)."""
    orders = {
        r.instance: r
        for r in execute(
            select(
                OrderRow.instance,
                func.count().label("n"),
                func.sum(case((OrderRow.status == OrderStatus.REJECTED, 1), else_=0)).label("rejected"),
            ).group_by(OrderRow.instance)
        )
    }
    fills = {
        r.instance: r
        for r in execute(
            select(
                FillRow.instance,
                func.count().label("n"),
                func.sum(case((FillRow.action == Action.BUY, FillRow.qty), else_=0)).label("contracts"),
                func.sum(FillRow.fee).label("fees"),
                func.sum(FillRow.realized).label("realized"),
            ).group_by(FillRow.instance)
        )
    }
    settled = {
        r.instance: r
        for r in execute(
            select(
                SettlementRow.instance,
                func.sum(SettlementRow.pnl).label("pnl"),
                func.sum(case((SettlementRow.pnl > 0, 1), else_=0)).label("wins"),
                func.sum(case((SettlementRow.pnl < 0, 1), else_=0)).label("losses"),
            ).group_by(SettlementRow.instance)
        )
    }
    out = []
    for inst in sorted(orders.keys() | fills.keys() | settled.keys()):
        o, f, s = orders.get(inst), fills.get(inst), settled.get(inst)
        out.append(
            Summary(
                instance=inst,
                orders=o.n if o else 0,
                rejected=o.rejected if o else 0,
                fills=f.n if f else 0,
                contracts=f.contracts if f else 0,
                fees=f.fees if f else 0.0,
                pnl=(f.realized if f else 0.0) + (s.pnl if s else 0.0),
                wins=s.wins if s else 0,
                losses=s.losses if s else 0,
            )
        )
    return out
