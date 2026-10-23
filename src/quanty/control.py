"""Controls for running sessions: the UI writes commands, sessions carry them out.

One SQLite file (data/control.sqlite) holds a command queue and each running session's heartbeat
and state. A session (quanty paper / quanty live) polls for its own run's commands every second,
applies them to its engine and brokers, and writes back the outcome, so every action is on record.

Actions: halt / resume an instance (or all); disarm / arm a live broker; kill (halt everything).
Halting stops new buys and cancels resting buys but lets exits through; disarming a live broker
refuses every order, exits included, and cancels its resting orders. Arming is only possible for
a session started with `quanty live --arm`.
"""

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    insert,
    select,
    update,
)

from quanty.brokers.live import LiveBroker
from quanty.brokers.shadow import ShadowBroker
from quanty.core.engine import Engine as Trader
from quanty.core.portfolio import Position
from quanty.core.risk import event_exposure
from quanty.core.types import Side

ACTIONS = ("halt", "resume", "disarm", "arm", "kill")
ALIVE_S = 10  # a session whose heartbeat is older than this is not running

meta = MetaData()
commands = Table(
    "commands",
    meta,
    Column("id", Integer, primary_key=True),
    Column("run", String, index=True, nullable=False),
    Column("action", String, nullable=False),
    Column("target", String, nullable=False, default=""),  # instance or broker; "" = all
    Column("created_ts", Integer, nullable=False),
    Column("status", String, nullable=False, default="pending"),  # pending / done / failed
    Column("result", Text, nullable=False, default=""),
    Column("done_ts", Integer),
)
sessions = Table(
    "sessions",
    meta,
    Column("run", String, primary_key=True),
    Column("pid", Integer, nullable=False),
    Column("started_ts", Integer, nullable=False),
    Column("heartbeat_ts", Integer, nullable=False),
    Column("can_arm", Boolean, nullable=False),
    Column("state", JSON, nullable=False),
)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class ControlStore:
    def __init__(self, path: Path, *, create: bool = True) -> None:
        """create=False: only read an existing file (the UI's pages never create it)."""
        self.path = path
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{path}" if create else f"sqlite:///file:{path.resolve()}?mode=ro&uri=true"
        self.engine: Engine = create_engine(url, connect_args={"timeout": 10})
        if create:
            with self.engine.begin() as conn:
                conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            meta.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()

    # --- UI side ---

    def submit(self, run: str, action: str, target: str = "") -> int:
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        with self.engine.begin() as conn:
            res = conn.execute(
                insert(commands).values(run=run, action=action, target=target, created_ts=now_ms())
            )
            key = res.inserted_primary_key
            assert key is not None
            return int(key[0])

    def session(self, run: str) -> dict[str, Any] | None:
        """The run's session, if it is running now."""
        with self.engine.connect() as conn:
            row = conn.execute(select(sessions).where(sessions.c.run == run)).mappings().first()
        if row is None or now_ms() - row["heartbeat_ts"] > ALIVE_S * 1000:
            return None
        return dict(row)

    def history(self, run: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            q = select(commands).where(commands.c.run == run).order_by(commands.c.id.desc()).limit(limit)
            return [dict(r) for r in conn.execute(q).mappings()]

    # --- session side ---

    def pending(self, run: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            q = (
                select(commands)
                .where(commands.c.run == run, commands.c.status == "pending")
                .order_by(commands.c.id)
            )
            return [dict(r) for r in conn.execute(q).mappings()]

    def finish(self, command_id: int, ok: bool, result: str) -> None:
        with self.engine.begin() as conn:
            done = {"status": "done" if ok else "failed", "result": result, "done_ts": now_ms()}
            conn.execute(update(commands).where(commands.c.id == command_id).values(**done))

    def heartbeat(self, run: str, started_ts: int, can_arm: bool, state: dict[str, Any]) -> None:
        row = {
            "pid": os.getpid(),
            "started_ts": started_ts,
            "heartbeat_ts": now_ms(),
            "can_arm": can_arm,
            "state": state,
        }
        with self.engine.begin() as conn:
            if conn.execute(update(sessions).where(sessions.c.run == run).values(**row)).rowcount == 0:
                conn.execute(insert(sessions).values(run=run, **row))


def broker_kind(broker: object) -> str:
    return (
        "live" if isinstance(broker, LiveBroker) else "shadow" if isinstance(broker, ShadowBroker) else "sim"
    )


class Controller:
    """Runs inside a session: heartbeat plus the run's commands, applied to its engine."""

    def __init__(
        self, store: ControlStore, run: str, trader: Trader, *, can_arm: bool, poll_s: float = 1.0
    ) -> None:
        self.store, self.run, self.trader, self.can_arm, self.poll_s = store, run, trader, can_arm, poll_s
        self.started_ts = now_ms()
        self.brokers = {i.broker.name: i.broker for i in trader.instances.values()}

    def state(self) -> dict[str, Any]:
        t = self.trader
        brokers = {}
        for name, b in self.brokers.items():
            info: dict[str, Any] = {"kind": broker_kind(b)}
            if isinstance(b, LiveBroker):
                info.update(armed=b.armed, disabled=b.disabled)
            brokers[name] = info
        instances = {
            iid: {
                "halted": iid in self.trader.halted,
                "broker": inst.broker.name,
                "blocked": self.trader.blocked.get(iid, 0),
            }
            for iid, inst in self.trader.instances.items()
        }
        return {
            "instances": instances,
            "brokers": brokers,
            "now": t.now,
            "markets": self.markets(),
            "exposure": self.exposure(),
        }

    def exposure(self) -> dict[str, dict[str, dict[str, float]]]:
        """Per exclusive event with positions: each instance's worst-case loss and best-case gain."""
        t = self.trader
        held: dict[tuple[str, str], list[tuple[str, Side, Position]]] = {}
        for iid, mid, side, pos in t.portfolio.open_positions():
            ev = t.risk.exclusive_event(mid)
            if ev is not None and pos.qty > 1e-9:
                held.setdefault((ev, iid), []).append((mid, side, pos))
        out: dict[str, dict[str, dict[str, float]]] = {}
        for (ev, iid), holdings in held.items():
            outcomes = [m for m in t.markets if t.risk.exclusive_event(m) == ev]
            exhaustive = all(t.markets[m].terms.get("exhaustive") is True for m in outcomes)
            loss, gain = event_exposure(holdings, outcomes, exhaustive)
            out.setdefault(ev, {})[iid] = {"loss": round(loss, 4), "gain": round(gain, 4)}
        return out

    def markets(self, limit: int = 24) -> list[dict[str, Any]]:
        """Open markets some instance trades, soonest close first: what they are, prices, the value
        they settle against, and our positions."""
        t = self.trader
        out = []
        wanted = [
            m for m in t.open_markets.values() if any(i.strategy.wants(m) for i in t.instances.values())
        ]
        for m in sorted(wanted, key=lambda m: (m.close_ts, m.id))[:limit]:
            book = t.books.get(m.id)
            d = m.describe()
            ref = t.latest.get(str(d["reference"])) if d["reference"] else None
            held = [
                {"instance": iid, "side": str(side), "qty": pos.qty, "avg": pos.avg_cost}
                for iid in t.instances
                for side in Side
                if (pos := t.portfolio.position(iid, m.id, side)).qty > 1e-9
            ]
            out.append(
                {
                    "id": m.id,
                    "series": m.series,
                    "open_ts": m.open_ts,
                    "close_ts": m.close_ts,
                    **d,
                    "reference_value": getattr(ref, "value", None),
                    "bid": book.bids[0][0] if book and book.bids else None,
                    "ask": book.asks[0][0] if book and book.asks else None,
                    "held": held,
                }
            )
        return out

    def tick(self) -> None:
        for cmd in self.store.pending(self.run):
            try:
                ok, result = True, self.apply(cmd["action"], cmd["target"])
            except ValueError as exc:
                ok, result = False, str(exc)
            self.trader.ledger.log(
                self.trader.now,
                "control",
                f"control: {cmd['action']}",
                {"target": cmd["target"], "ok": ok, "result": result},
                level="warning" if ok else "error",
            )
            self.store.finish(cmd["id"], ok, result)
        self.store.heartbeat(self.run, self.started_ts, self.can_arm, self.state())

    def apply(self, action: str, target: str) -> str:
        t = self.trader
        if action in ("halt", "resume"):
            if target and target not in t.instances:
                raise ValueError(f"no instance {target!r}")
            (t.halt if action == "halt" else t.resume)(
                target or None, *(["from the UI"] if action == "halt" else [])
            )
            return f"{'halted' if action == 'halt' else 'resumed'} {target or 'all instances'}"
        if action == "kill":
            t.halt(None, "kill switch")
            return "halted all instances: resting buys canceled, exits still allowed"
        broker = self.brokers.get(target)
        if not isinstance(broker, LiveBroker):
            raise ValueError(f"no live broker {target!r}")
        if action == "disarm":
            broker.armed = False
            n = 0
            for o in list(t.live.values()):
                if t.instances[o.instance].broker is broker:
                    t.cancel(o.id)
                    n += 1
            return f"disarmed {target}; canceling {n} open order(s)"
        if not self.can_arm:
            raise ValueError("this session was not started with quanty live --arm")
        if broker.disabled:
            raise ValueError(
                f"{target} disabled itself ({broker.disabled}); restart the session after checking"
            )
        broker.armed = True
        return f"armed {target}"

    async def run_forever(self) -> None:
        while True:
            self.tick()
            await asyncio.sleep(self.poll_s)
