"""Controls for running sessions: the UI writes commands, sessions carry them out.

One SQLite file (data/control.sqlite) holds a command queue and each running session's heartbeat
and state. A session (quanty paper / quanty live) polls for its own run's commands every second,
applies them to its engine and brokers, and writes back the outcome, so every action is on record.

Actions: halt / resume an instance (or all); disarm / arm a live broker; kill (halt everything);
params (change an instance's strategy params: value = {name: new value}) and reset_params (back to
the experiment file's); save_params writes an instance's changed params into the experiment file
(where the session runs from a project folder, not a container image). Param changes are kept per
run as overrides, so a restarted session picks them up again; the instance limits and broker caps
can't be changed here, so they still bound them.
Halting stops new buys and cancels resting buys but lets exits through; disarming a live broker
refuses every order, exits included, and cancels its resting orders. Arming is only possible for
a session started with `quanty live --arm`.
"""

import asyncio
import os
import time
from collections.abc import Callable, Mapping
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
    delete,
    insert,
    select,
    update,
)

from quanty.brokers.live import LiveBroker
from quanty.core.broker import Broker
from quanty.core.engine import Engine as Trader
from quanty.core.engine import describe
from quanty.core.portfolio import Position
from quanty.core.risk import event_exposure
from quanty.core.types import Side

ACTIONS = (
    "halt",
    "resume",
    "disarm",
    "arm",
    "kill",
    "speed",
    "new_day",
    "params",
    "reset_params",
    "save_params",
)
ALIVE_S = 10  # a session whose heartbeat is older than this is not running

meta = MetaData()
commands = Table(
    "commands",
    meta,
    Column("id", Integer, primary_key=True),
    Column("run", String, index=True, nullable=False),
    Column("action", String, nullable=False),
    Column("target", String, nullable=False, default=""),  # instance or broker; "" = all
    Column("value", JSON),  # the action's argument, if any (params: {name: value})
    Column("created_ts", Integer, nullable=False),
    Column("status", String, nullable=False, default="pending"),  # pending / done / failed
    Column("result", Text, nullable=False, default=""),
    Column("done_ts", Integer),
)
overrides = Table(
    "overrides",
    meta,
    Column("run", String, primary_key=True),
    Column("instance", String, primary_key=True),
    Column("params", JSON, nullable=False),  # {name: value} that differ from the experiment file
    Column("updated_ts", Integer, nullable=False),
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
            with self.engine.begin() as conn:  # files from before commands had a value
                cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(commands)")}
                if "value" not in cols:
                    conn.exec_driver_sql("ALTER TABLE commands ADD COLUMN value JSON")

    def close(self) -> None:
        self.engine.dispose()

    # --- UI side ---

    def submit(self, run: str, action: str, target: str = "", value: Any = None) -> int:
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        with self.engine.begin() as conn:
            res = conn.execute(
                insert(commands).values(
                    run=run, action=action, target=target, value=value, created_ts=now_ms()
                )
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

    def overrides(self, run: str) -> dict[str, dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(overrides).where(overrides.c.run == run)).mappings()
            return {r["instance"]: dict(r["params"]) for r in rows}

    def set_override(self, run: str, instance: str, params: dict[str, Any]) -> None:
        """Save an instance's param overrides; empty = none (back to the experiment file)."""
        with self.engine.begin() as conn:
            where = (overrides.c.run == run) & (overrides.c.instance == instance)
            conn.execute(delete(overrides).where(where))
            if params:
                conn.execute(
                    insert(overrides).values(run=run, instance=instance, params=params, updated_ts=now_ms())
                )

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


class Playback:
    """Paces recorded or synthetic events against the wall clock at a speed that can change while
    running (the demo's fast / real-time toggle). Changing it re-anchors, so time never jumps."""

    def __init__(self, speed: float, options: tuple[float, ...]) -> None:
        self.speed, self.options = speed, options
        self.catching_up = False  # playing the history before now, as fast as it can
        self.progress = 0.0  # how much of that history has played, 0 to 1
        self.seed: int | None = None  # the synthetic day being played (a demo)
        self.on_new_day: Callable[[], int] | None = None  # starts over on a new day; returns its seed
        self._anchor: tuple[float, int] | None = None  # (monotonic s, event ms) at the last change

    def set(self, speed: float) -> None:
        self.speed = speed
        self._anchor = None  # re-anchored at the next event

    async def wait_for(self, ts: int) -> None:
        if self._anchor is None:
            self._anchor = (time.monotonic(), ts)
        wall0, ev0 = self._anchor
        delay = (ts - ev0) / 1000 / self.speed - (time.monotonic() - wall0)
        if delay > 0:
            await asyncio.sleep(delay)


def broker_kind(broker: Broker) -> str:
    return broker.kind


class Controller:
    """Runs inside a session: heartbeat plus the run's commands, applied to its engine."""

    def __init__(
        self,
        store: ControlStore,
        run: str,
        trader: Trader,
        *,
        can_arm: bool,
        poll_s: float = 1.0,
        playback: Playback | None = None,
    ) -> None:
        """playback: a demo's pacing, which the dashboard can switch between speeds."""
        self.store, self.run, self.trader, self.can_arm, self.poll_s = store, run, trader, can_arm, poll_s
        self.playback = playback
        self.feeds: Callable[[], Mapping[str, str]] | None = None  # feed -> health, set by a live session
        self.experiment: Path | None = None  # the experiment file, set by a live session (not a demo)
        self.started_ts = now_ms()
        self.attach(trader)

    def attach(self, trader: Trader) -> None:
        """Control this engine (a demo's "new day" swaps in a fresh one), with the run's saved param
        overrides applied again."""
        self.trader = trader
        self.brokers = {i.broker.name: i.broker for i in trader.instances.values()}
        self.file_params = {iid: i.strategy.params for iid, i in trader.instances.items()}
        self.file_set = {iid: set(i.strategy.params.model_fields_set) for iid, i in trader.instances.items()}
        for iid, changes in self.store.overrides(self.run).items():
            if iid not in trader.instances:
                continue
            try:
                result = self._set_params(iid, changes)
            except ValueError as exc:  # the strategy's params changed since: drop what no longer fits
                self.store.set_override(self.run, iid, {})
                result = f"dropped saved changes that no longer fit: {exc}"
            self._log("control: params restored", {"target": iid, "ok": True, "result": result})

    def _log(self, msg: str, fields: dict[str, Any], level: str = "warning") -> None:
        if self.trader.ledger is not None:
            self.trader.ledger.log(self.trader.now, "control", msg, fields, level=level)

    def edited(self, iid: str) -> dict[str, Any]:
        """The instance's params that differ from the experiment file's."""
        cur = self.trader.instances[iid].strategy.params.model_dump(mode="json")
        base = self.file_params[iid].model_dump(mode="json")
        return {k: v for k, v in cur.items() if base.get(k) != v}

    @property
    def save_mode(self) -> str | None:
        """How edits reach the experiment file: "file" (written here), "copy" (this runs from a
        container image, whose copy of the file a redeploy replaces: copy the lines instead), or None
        (no file: a demo)."""
        if self.experiment is None:
            return None
        writable = os.access(self.experiment, os.W_OK) and os.access(self.experiment.parent, os.W_OK)
        return "copy" if os.environ.get("QUANTY_IN_IMAGE") or not writable else "file"

    def _save_params(self, iid: str) -> str:
        from quanty import experiment

        if self.save_mode != "file" or self.experiment is None:
            raise ValueError(
                "this session can't write its experiment file (it runs from a container image or a "
                "read-only folder): copy the lines into the file on your machine, then deploy"
            )
        edited = self.edited(iid)
        if not edited:
            return f"{iid}: nothing changed from {self.experiment.name}"
        experiment.save_params(self.experiment, iid, edited)
        inst = self.trader.instances[iid]
        self.file_params[iid] = inst.strategy.params  # the file says this now
        self.file_set[iid] |= set(edited)
        self.store.set_override(self.run, iid, {})
        if self.trader.ledger is not None:
            self.trader.ledger.log(
                self.trader.now,
                iid,
                "strategy",
                {**describe(inst), "params_set": sorted(self.file_set[iid]), "edited": []},
            )
        return f"{iid}: saved {', '.join(f'{k} = {v}' for k, v in edited.items())} to {self.experiment.name}"

    def _set_params(self, iid: str, changes: Mapping[str, Any]) -> str:
        inst = self.trader.instances[iid]
        current = inst.strategy.params
        unknown = set(changes) - set(type(current).model_fields)
        if unknown:
            raise ValueError(f"{iid} has no param {', '.join(sorted(unknown))}")
        new = type(current).from_config({**current.model_dump(mode="json"), **changes}, where=f"{iid}.params")
        before = current.model_dump(mode="json")
        self.trader.set_params(iid, new)
        edited = self.edited(iid)
        self.store.set_override(self.run, iid, edited)
        if self.trader.ledger is not None:  # the dashboard shows the latest description
            self.trader.ledger.log(
                self.trader.now,
                iid,
                "strategy",
                {
                    **describe(inst),
                    "params_set": sorted(self.file_set[iid]),  # still the file's
                    "edited": sorted(edited),
                },
            )
        after = new.model_dump(mode="json")
        diff = [f"{k} {before[k]} → {after[k]}" for k in after if before.get(k) != after[k]]
        return f"{iid}: {', '.join(diff) or 'no change'}"

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
                "params": inst.strategy.params.model_dump(mode="json"),
                "edited": sorted(self.edited(iid)),
            }
            for iid, inst in self.trader.instances.items()
        }
        return {
            "actions": list(ACTIONS),  # what this session's quanty can do (the dashboard may be newer)
            "save": self.save_mode,
            "instances": instances,
            "brokers": brokers,
            "now": t.now,
            "markets": self.markets(),
            "exposure": self.exposure(),
            "platforms": self.platforms(),
            "speed": None
            if self.playback is None
            else {
                "value": self.playback.speed,
                "options": list(self.playback.options),
                "catching_up": self.playback.catching_up,
                "progress": round(self.playback.progress, 3),
                "seed": self.playback.seed,
                "new_day": self.playback.on_new_day is not None,
            },
        }

    def platforms(self) -> list[dict[str, str]]:
        """What this session is connected to: each feed and its health, or, with no feeds (a demo or
        replay), the venues of its markets as simulated."""
        if self.feeds is not None:
            return [{"name": name, "status": str(status)} for name, status in sorted(self.feeds().items())]
        venues = sorted({m.venue for m in self.trader.markets.values()})
        return [{"name": v, "status": "simulated"} for v in venues]

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
                    "estimates": {i: round(p, 4) for i, p in t.estimates.get(m.id, {}).items()},
                }
            )
        return out

    def tick(self) -> None:
        for cmd in self.store.pending(self.run):
            try:
                ok, result = True, self.apply(cmd["action"], cmd["target"], cmd.get("value"))
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

    def apply(self, action: str, target: str, value: Any = None) -> str:
        t = self.trader
        if action in ("params", "reset_params", "save_params"):
            if target not in t.instances:
                raise ValueError(f"no instance {target!r}")
            if action == "save_params":
                return self._save_params(target)
            if action == "reset_params":
                base = self.file_params[target].model_dump(mode="json")
                return self._set_params(target, base) + " (back to the experiment file)"
            if not isinstance(value, dict) or not value:
                raise ValueError("params needs {name: value}")
            return self._set_params(target, value)
        if action == "new_day":
            if self.playback is None or self.playback.on_new_day is None:
                raise ValueError("only demos can start a new day")
            return f"started a new day (seed {self.playback.on_new_day()})"
        if action == "speed":
            if self.playback is None:
                raise ValueError("this session runs in real time; only demos change speed")
            try:
                speed = float(target)
            except ValueError:
                raise ValueError(f"speed {target!r} is not a number") from None
            if speed not in self.playback.options:
                raise ValueError(
                    f"speed {speed:g} is not one of {', '.join(f'{o:g}' for o in self.playback.options)}"
                )
            self.playback.set(speed)
            return "real time" if speed == 1 else f"{speed:g}x real time"
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
