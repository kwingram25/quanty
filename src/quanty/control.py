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
from quanty.brokers.shadow import ShadowBroker
from quanty.core.broker import Broker
from quanty.core.engine import Engine as Trader
from quanty.core.engine import describe
from quanty.core.portfolio import Position
from quanty.core.risk import event_exposure
from quanty.core.types import Side
from quanty.venues.orders import has_keys

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
    "switch",
    "reload",
)
ALIVE_S = 10  # a session whose heartbeat is older than this is not running
DEPLOYED_MARK = ".quanty-deployed"  # left by `quanty deploy` in a server's project folder

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
switches = Table(  # instances moved onto another broker while running, where the file can't say so
    "switches",
    meta,
    Column("run", String, primary_key=True),
    Column("instance", String, primary_key=True),
    Column("broker", String, nullable=False),
    Column("twin", String),  # the paper twin started beside it, if any
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

    def held_changes(self, run_prefix: str = "session/") -> dict[str, dict[str, Any]]:
        """Dashboard changes held here, per run: what `quanty pull` brings into the experiment files."""
        out: dict[str, dict[str, Any]] = {}
        with self.engine.connect() as conn:
            for r in conn.execute(select(overrides).where(overrides.c.run.startswith(run_prefix))).mappings():
                out.setdefault(r["run"], {"params": {}, "switches": {}})["params"][r["instance"]] = dict(
                    r["params"]
                )
            for r in conn.execute(select(switches).where(switches.c.run.startswith(run_prefix))).mappings():
                entry = out.setdefault(r["run"], {"params": {}, "switches": {}})
                entry["switches"][r["instance"]] = {"broker": r["broker"], "twin": r["twin"]}
        return out

    def switches(self, run: str) -> dict[str, tuple[str, str | None]]:
        """instance -> (broker, twin) saved for a run."""
        with self.engine.connect() as conn:
            rows = conn.execute(select(switches).where(switches.c.run == run)).mappings()
            return {r["instance"]: (r["broker"], r["twin"]) for r in rows}

    def set_switch(self, run: str, instance: str, broker: str | None, twin: str | None = None) -> None:
        """Remember (or, with broker None, forget) an instance's switch for the next session."""
        with self.engine.begin() as conn:
            conn.execute(delete(switches).where((switches.c.run == run) & (switches.c.instance == instance)))
            if broker is not None:
                row = {
                    "run": run,
                    "instance": instance,
                    "broker": broker,
                    "twin": twin,
                    "updated_ts": now_ms(),
                }
                conn.execute(insert(switches).values(**row))

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
        self._keys_missing: dict[str, list[str]] = {}
        self.playback = playback
        self.feeds: Callable[[], Mapping[str, str]] | None = None  # feed -> health, set by a live session
        self.experiment: Path | None = None  # the experiment file, set by a live session (not a demo)
        # (experiment, strategies) read afresh, for "reload"; set by a live session
        self.rebuild: Callable[[], tuple[Any, Mapping[str, Any]]] | None = None
        self.retired: set[str] = set()  # instances the experiment no longer has: exits only
        self._saved_switches: dict[str, dict[str, Any]] = {}
        self.started_ts = now_ms()
        self.attach(trader)

    def attach(self, trader: Trader) -> None:
        """Control this engine (a demo's "new day" swaps in a fresh one), with the run's saved param
        overrides applied again."""
        self.trader = trader
        self.brokers = trader.brokers
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
        for iid, (broker, twin) in self.store.switches(self.run).items():  # where the file couldn't say so
            if iid not in trader.instances or trader.instances[iid].broker.name == broker:
                self.store.set_switch(self.run, iid, None)  # gone, or the file says it now
                continue
            try:
                result = self._switch(iid, broker, twin is not None, persist=False)
                kind = trader.brokers[broker].kind
                self._saved_switches[iid] = {"broker": broker, "kind": kind, "twin": twin}
            except ValueError as exc:
                self.store.set_switch(self.run, iid, None)
                result = f"dropped a saved switch that no longer fits: {exc}"
            self._log("control: switch restored", {"target": iid, "ok": True, "result": result})

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
        """How edits reach the experiment file: "file" (written here), "copy" (kept with the run; a
        container image, or a project `quanty deploy` put on a server, whose files the next deploy
        replaces: copy the lines, or `quanty pull` / `quanty deploy` brings them home), or None (no
        file: a demo)."""
        if self.experiment is None:
            return None
        deployed = any((d / DEPLOYED_MARK).exists() for d in self.experiment.resolve().parents[:3])
        writable = os.access(self.experiment, os.W_OK) and os.access(self.experiment.parent, os.W_OK)
        return "copy" if os.environ.get("QUANTY_IN_IMAGE") or deployed or not writable else "file"

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

    def _reload(self) -> str:
        """Bring the running session in line with its experiment file: start instances it added,
        retire ones it dropped (no new entries; what they hold finishes), apply changed params
        (dashboard edits still on top), brokers and live brokers' caps. New brokers, limits or feeds
        need a restart."""
        from quanty.core.engine import Instance
        from quanty.experiment import LiveBrokerConfig, strategy_class

        if self.rebuild is None:
            raise ValueError("this session has no experiment file to reload (a demo?)")
        exp, strategies = self.rebuild()
        t, done = self.trader, []
        for name, cfg in exp.brokers.items():  # caps come from the file only, never the dashboard
            b = t.brokers.get(name)
            caps = (cfg.max_qty, cfg.max_notional) if isinstance(cfg, LiveBrokerConfig) else None
            if caps is not None and isinstance(b, LiveBroker) and (b.max_qty, b.max_notional) != caps:
                b.max_qty, b.max_notional = caps
                done.append(
                    f"{name}: caps " + (f"max_qty {caps[0]}, max_notional {caps[1]}" if b.capped else "none")
                )
        specs = {s.id: s for s in exp.instances}
        for iid, spec in specs.items():
            if spec.strategy not in strategies:
                done.append(f"{iid}: no strategy {spec.strategy!r}, skipped")
                continue
            cls = strategy_class(strategies[spec.strategy])
            params = cls.params_type.from_config(spec.params, where=f"{iid}.params")
            want = t.brokers.get(exp.broker_of(spec))
            if want is None:
                done.append(f"{iid}: broker {exp.broker_of(spec)!r} is new; restart to use it")
                continue
            if iid not in t.instances:
                if want.kind == "live" and not self.can_arm:
                    done.append(f"{iid}: on a live broker, but this session isn't armed; skipped")
                    continue
                strategy = cls(params)
                if spec.series is not None:
                    strategy.series = tuple(spec.series)
                t.add_instance(Instance(iid, strategy, want, spec.limits, spec.twin_of))
                self.file_params[iid], self.file_set[iid] = params, set(params.model_fields_set)
                done.append(f"{iid}: started")
                continue
            if iid in self.retired:  # back in the file
                self.retired.discard(iid)
                t.resume(iid)
                done.append(f"{iid}: back")
            if params.model_dump(mode="json") != self.file_params[iid].model_dump(mode="json"):
                edits = self.edited(iid)
                self.file_params[iid], self.file_set[iid] = params, set(params.model_fields_set)
                self._set_params(iid, {**params.model_dump(mode="json"), **edits})
                done.append(f"{iid}: params from the file")
            if t.instances[iid].broker is not want:
                done.append(self._switch(iid, want.name, twin=False, persist=False))
        for iid in [i for i in t.instances if i not in specs and i not in self.retired]:
            t.halt(iid, "removed from the experiment")
            self.retired.add(iid)
            done.append(f"{iid}: retired (no new entries; what it holds finishes)")
        return "; ".join(done) or "nothing changed"

    def _switch(self, iid: str, broker_name: str, twin: bool, persist: bool = True) -> str:
        """Move an instance onto another broker for the markets it enters from now on (the ones it's
        in finish where they are); going live, start its paper twin beside it. Saved to the
        experiment file where this session can write it, else kept for the next session."""
        from quanty.engine_tools import twin_of

        t = self.trader
        inst = t.instances[iid]
        new = t.brokers.get(broker_name)
        if new is None:
            raise ValueError(f"no broker {broker_name!r} in this session")
        if new.kind == "live":
            if not self.can_arm or not isinstance(new, LiveBroker) or not new.armed:
                raise ValueError("switching to a live broker needs a session started with quanty live --arm")
            if not new.capped:
                raise ValueError(
                    f"{broker_name} has no caps: set max_qty and max_notional under [brokers.{broker_name}] "
                    "in the experiment file, then Reload"
                )
            if new.disabled:
                raise ValueError(f"{broker_name} disabled itself ({new.disabled}); restart after checking")
        old = inst.broker
        has_twin = any(i.twin_of == iid for i in t.instances.values())
        make_twin = new.kind == "live" and old.kind != "live" and twin and not has_twin
        to_file = persist and self.save_mode == "file" and self.experiment is not None
        if to_file:  # first, so a file that can't take it leaves everything as it was
            from quanty import promote

            assert self.experiment is not None
            promote.promote(self.experiment, iid, new.kind, broker=broker_name, twin=make_twin)
        stays = t.switch(iid, broker_name)
        made = None
        if make_twin:
            made = twin_of(t, inst, old)
            self.file_params[made.id] = made.strategy.params
            self.file_set[made.id] = set(made.strategy.params.model_fields_set)
        if persist and not to_file:
            twin_id = made.id if made else self._saved_switches.get(iid, {}).get("twin")
            self.store.set_switch(self.run, iid, broker_name, twin_id)
            self._saved_switches[iid] = {"broker": broker_name, "kind": new.kind, "twin": twin_id}
        where = f"; {len(stays)} market(s) it's in stay on {old.name}" if stays else ""
        twin_note = f"; paper twin {made.id} started on {old.name}" if made else ""
        return f"{iid}: new markets on {broker_name} ({new.kind}){where}{twin_note}"

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
                info.update(armed=b.armed, disabled=b.disabled, capped=b.capped)
            if isinstance(b, ShadowBroker):  # it previews orders with the venue's keys
                if name not in self._keys_missing:  # keys don't change while it runs
                    self._keys_missing[name] = sorted(v for v, api in b.apis.items() if not has_keys(api))
                info["keys_missing"] = self._keys_missing[name]
            brokers[name] = info
        instances = {
            iid: {
                "halted": iid in self.trader.halted,
                "broker": inst.broker.name,
                "blocked": self.trader.blocked.get(iid, 0),
                "params": inst.strategy.params.model_dump(mode="json"),
                "retired": iid in self.retired,
                "switched": self._saved_switches.get(iid),  # kept with the run, not yet in the file
                "edited": sorted(self.edited(iid)),
            }
            for iid, inst in self.trader.instances.items()
        }
        return {
            "actions": list(ACTIONS),  # what this session's quanty can do (the dashboard may be newer)
            "save": self.save_mode,
            "reload": self.rebuild is not None,
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
        if action == "reload":
            return self._reload()
        if action == "switch":
            if target not in t.instances:
                raise ValueError(f"no instance {target!r}")
            v = value if isinstance(value, dict) else {}
            current = t.instances[target].broker
            name = v.get("broker") or next(
                (n for n, b in t.brokers.items() if b.kind == v.get("to") and b is not current), None
            )
            if name is None:
                raise ValueError(f"this session has no {v.get('to') or 'other'} broker to switch {target} to")
            return self._switch(target, str(name), bool(v.get("twin", True)))
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
            if action == "resume" and target in self.retired:
                raise ValueError(f"{target} was removed from the experiment: add it back and reload")
            if action == "halt":
                t.halt(target or None, "from the UI")
            else:  # every instance, but never the retired ones
                for iid in [target] if target else [i for i in t.instances if i not in self.retired]:
                    t.resume(iid)
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
