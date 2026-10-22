"""SQLite record of every order, fill, settlement and strategy log line, per strategy instance."""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from quanty.core.events import Fill
from quanty.core.orders import Order
from quanty.core.portfolio import Settlement

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, instance TEXT, market_id TEXT, side TEXT, action TEXT, price REAL,
    qty INTEGER, tif TEXT, post_only INTEGER, tag TEXT, created_ts INTEGER, status TEXT,
    filled INTEGER, reason TEXT);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY, ts INTEGER, instance TEXT, order_id TEXT, market_id TEXT, side TEXT,
    action TEXT, price REAL, qty INTEGER, fee REAL, liquidity TEXT, realized REAL);
CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY, ts INTEGER, instance TEXT, market_id TEXT, side TEXT, qty INTEGER,
    payout REAL, pnl REAL);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY, ts INTEGER, instance TEXT, level TEXT, msg TEXT, fields TEXT);
"""


@dataclass(frozen=True)
class Summary:
    instance: str
    orders: int
    rejected: int
    fills: int
    contracts: int  # bought
    fees: float
    pnl: float  # realized: sells + settlements
    wins: int  # settled positions with positive P&L
    losses: int


class Ledger:
    def __init__(self, path: str | Path = ":memory:", durable: bool = False) -> None:
        """durable commits after every write (live); otherwise call commit() (backtests)."""
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)
        self.durable = durable

    def order(self, o: Order) -> None:
        self._write(
            "INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (o.id, o.instance, o.market_id, o.side, o.action, o.price, o.qty, o.tif, o.post_only,
             o.tag, o.created_ts, o.status, o.filled, o.reason),
        )  # fmt: skip

    def fill(self, instance: str, f: Fill, realized: float) -> None:
        self._write(
            "INSERT INTO fills VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?)",
            (f.ts, instance, f.order_id, f.market_id, f.side, f.action, f.price, f.qty, f.fee,
             f.liquidity, realized),
        )  # fmt: skip

    def settlement(self, s: Settlement) -> None:
        self._write(
            "INSERT INTO settlements VALUES (NULL,?,?,?,?,?,?,?)",
            (s.ts, s.instance, s.market_id, s.side, s.qty, s.payout, s.pnl),
        )

    def log(
        self,
        ts: int,
        instance: str,
        msg: str,
        fields: Mapping[str, object] | None = None,
        level: str = "info",
    ) -> None:
        self._write(
            "INSERT INTO logs VALUES (NULL,?,?,?,?,?)",
            (ts, instance, level, msg, json.dumps(dict(fields or {}), default=str)),
        )

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def summary(self) -> list[Summary]:
        q = self.db.execute
        orders = dict(q("SELECT instance, COUNT(*) FROM orders GROUP BY instance").fetchall())
        rejected = dict(
            q("SELECT instance, COUNT(*) FROM orders WHERE status='rejected' GROUP BY instance").fetchall()
        )
        fills = {
            r[0]: r[1:]
            for r in q(
                "SELECT instance, COUNT(*), SUM(CASE WHEN action='buy' THEN qty ELSE 0 END), SUM(fee),"
                " SUM(realized) FROM fills GROUP BY instance"
            )
        }
        settled = {
            r[0]: r[1:]
            for r in q(
                "SELECT instance, SUM(pnl), SUM(pnl > 0), SUM(pnl < 0) FROM settlements GROUP BY instance"
            )
        }
        out = []
        for inst in sorted(set(orders) | set(fills) | set(settled)):
            n_fills, contracts, fees, sell_pnl = fills.get(inst, (0, 0, 0.0, 0.0))
            settle_pnl, wins, losses = settled.get(inst, (0.0, 0, 0))
            out.append(
                Summary(inst, orders.get(inst, 0), rejected.get(inst, 0), n_fills, contracts or 0,
                        fees or 0.0, (sell_pnl or 0.0) + (settle_pnl or 0.0), wins or 0, losses or 0)
            )  # fmt: skip
        return out

    def _write(self, sql: str, args: tuple[object, ...]) -> None:
        self.db.execute(sql, args)
        if self.durable:
            self.db.commit()
