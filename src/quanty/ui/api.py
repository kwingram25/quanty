"""JSON API and live stream for the dashboard. Reads databases read-only; writes only control
commands (data/control.sqlite), behind a per-server token and a same-origin check."""

import asyncio
import csv
import dataclasses
import io
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from quanty.control import ControlStore
from quanty.core.ledger import Summary
from quanty.db.models import FillRow, LogRow, OrderRow, SettlementRow
from quanty.ui import queries
from quanty.ui.auth import Auth
from quanty.venues.orders import OrderAPI, VenueError, default_apis

STREAM_POLL_S = 0.5
BALANCE_EVERY_S = 30  # account balances: asked at most this often per venue
BALANCE_BACKOFF_S = 120  # after a refusal (rate limit, error): leave the venue alone this long


def _row(obj: Any) -> dict[str, Any]:
    """An ORM row or dataclass as plain JSON-able values."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    return {c.key: _plain(getattr(obj, c.key)) for c in obj.__table__.columns}


def _plain(v: Any) -> Any:
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return _row(v)
    if isinstance(v, Path):
        return str(v)
    if hasattr(v, "value") and isinstance(v.value, str):  # enums
        return str(v)
    return v


# sessions started before they reported their actions could do these
BEFORE_ACTIONS_LISTED = ("halt", "resume", "disarm", "arm", "kill", "speed", "new_day")


class Command(BaseModel):
    action: str
    target: str = ""
    value: dict[str, Any] | None = None  # params: {name: new value}
    confirm: str = ""


def _csv(filename: str, rows: list[dict[str, Any]], names: dict[str, dict[str, str]]) -> Response:
    """Rows as a CSV download, with each market's title and subtitle next to its id; times as UTC."""
    out = io.StringIO()
    if rows:
        cols = list(rows[0])
        w = csv.writer(out)
        w.writerow([*cols, "market_title", "market_subtitle"])
        for row in rows:
            n = names.get(row["market_id"], {})
            w.writerow([_cell(k, row[k]) for k in cols] + [n.get("title", ""), n.get("subtitle", "")])
    return Response(
        out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _cell(key: str, v: Any) -> Any:
    if v is not None and (key.endswith("_ts") or key in ("opened", "closed")):
        return datetime.fromtimestamp(v / 1000, UTC).isoformat(timespec="milliseconds")
    return v


def _activity(a: queries.Activity) -> dict[str, Any]:
    return {
        **_row(a.order),
        "fills": a.fills,
        "avg_price": a.avg_price,
        "fees": a.fees,
        "realized": a.realized,
        "liquidity": a.liquidity,
        "result": a.result,
    }


class Api:
    def __init__(self, data: Path, auth: "Auth | None" = None) -> None:
        self.data = data
        self.auth = auth  # the dashboard login, when one is set
        self.token = secrets.token_urlsafe(24)
        self.control_path = data / "control.sqlite"
        self.apis: dict[str, OrderAPI] = default_apis()
        self._balances: dict[str, dict[str, Any]] = {}  # venue -> last answer (or error), with when
        self._balance_lock = asyncio.Lock()

    # --- reads ---

    def control(self, run: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        if not self.control_path.exists():
            return None, []
        store = ControlStore(self.control_path, create=False)
        try:
            return store.session(run), store.history(run)
        finally:
            store.close()

    def runs(self) -> list[dict[str, Any]]:
        out = []
        for r in queries.find_runs(self.data):
            v = queries.view(r)
            session, _ = self.control(r.name)
            out.append(
                {
                    "name": r.name,
                    "mode": r.mode,
                    "pnl": v.pnl,
                    "fills": v.fills,
                    "errors": v.errors,
                    "strategies": len(v.summaries),
                    "last_ts": v.last_ts,
                    "running": session is not None,
                    **self._money_split(r, v),
                }
            )
        return out

    def run_or_404(self, mode: str, name: str) -> queries.Run:
        r = queries.run_by_name(self.data, f"{mode}/{name}")
        if r is None:
            raise HTTPException(404, f"no run {mode}/{name}")
        return r

    def kinds(
        self, r: queries.Run, info: dict[str, Any] | None = None, state: dict[str, Any] | None = None
    ) -> dict[str, str]:
        """Each instance's broker kind (live = real money): as recorded when the run started, else as
        the running session reports it; in a live run of a ledger too old to say, assumed live."""
        info = queries.strategy_info(r) if info is None else info
        if state is None:
            session, _ = self.control(r.name)
            state = (session or {}).get("state", {})
        brokers = state.get("brokers", {})
        out = {}
        for iid in set(info) | set(state.get("instances", {})):
            k = info.get(iid, {}).get("broker_kind")
            if k is None:
                b = state.get("instances", {}).get(iid, {}).get("broker")
                k = brokers.get(b, {}).get("kind") if b else None
            out[iid] = str(k or ("live" if r.mode == "live" else "sim"))
        return out

    def money(
        self, r: queries.Run, money: str | None, instance: str | None
    ) -> tuple[str | list[str] | None, bool | None]:
        """The filters for a history query: (instances, real). Runs that record each order's kind
        filter by it (an instance can be paper first and live later); older runs by instance."""
        if not money:
            return instance, None
        if money not in ("real", "simulated"):
            raise ValueError("money is real or simulated")
        if queries.kinds_recorded(r):
            return instance, money == "real"
        ids = [i for i, k in self.kinds(r).items() if (k == "live") == (money == "real")]
        return ([i for i in ids if i == instance] if instance else ids), None

    def _money_split(self, r: queries.Run, v: queries.RunView) -> dict[str, Any]:
        """For the runs list: whether the run is live (has real money) and, if so, its real and
        simulated P&L apart; a session run is "live" or "paper" by its strategies."""
        kinds = self.kinds(r)
        if queries.kinds_recorded(r):
            by_kind = queries.pnl_by_kind(r)
            real = "live" in kinds.values() or any("live" in k for k in by_kind.values())
            split = {
                "pnl_real": sum(k.get("live", 0.0) for k in by_kind.values()),
                "pnl_simulated": sum(x for k in by_kind.values() for kind, x in k.items() if kind != "live"),
            }
        else:
            real = r.mode == "live" or "live" in kinds.values()
            summaries = {s.instance: s for s in v.summaries}
            split = {
                "pnl_real": sum(
                    s.pnl
                    for k, s in summaries.items()
                    if kinds.get(k, "live" if r.mode == "live" else "sim") == "live"
                ),
                "pnl_simulated": sum(s.pnl for k, s in summaries.items() if kinds.get(k, "sim") != "live"),
            }
        label = r.mode if r.mode in ("backtest",) else ("live" if real else "paper")
        return {"label": label, **(split if real else {})}

    def twins(self, r: queries.Run, info: dict[str, Any] | None = None) -> dict[str, str]:
        """Live id -> its paper twin's id, from the twin_of each strategy recorded."""
        info = queries.strategy_info(r) if info is None else info
        return {
            str(i["twin_of"]): iid for iid, i in info.items() if i.get("twin_of") and i["twin_of"] in info
        }

    def is_real(self, r: queries.Run) -> bool:
        """Whether a run trades real money now: actions on it need the run's name typed."""
        if r.mode == "live":
            return True
        session, _ = self.control(r.name)
        brokers = (session or {}).get("state", {}).get("brokers", {})
        return any(b.get("kind") == "live" for b in brokers.values())

    def snapshot(self, mode: str, name: str) -> dict[str, Any]:
        r = self.run_or_404(mode, name)
        v = queries.view(r)
        session, history = self.control(r.name)
        summaries = {s.instance: s for s in v.summaries}
        info = queries.strategy_info(r)
        state = (session or {}).get("state", {})
        for iid in state.get("instances", {}):
            summaries.setdefault(iid, Summary(iid, 0, 0, 0, 0.0, 0.0, 0.0, 0, 0))
        snap: dict[str, Any] = {
            "run": {"name": r.name, "mode": r.mode, "file": f"{r.mode}/{r.path.name}", "last_ts": v.last_ts},
            "stats": {"pnl": v.pnl, "fills": v.fills, "errors": v.errors},
            "strategies": [{**_row(summaries[k]), "info": info.get(k, {})} for k in sorted(summaries)],
            "curves": queries.pnl_curves(r),
            "positions": [_row(p) for p in queries.positions(r)],
            "activity": [_activity(a) for a in queries.activity(r)],
            "order_issues": [_activity(a) for a in queries.activity(r, needs_look=True)],
            "order_stats": queries.order_stats(r),
            "tape": queries.tape(r),
            "session": None
            if session is None
            else {
                "pid": session["pid"],
                "heartbeat_ts": session["heartbeat_ts"],
                "can_arm": session["can_arm"],
                "actions": state.get("actions", list(BEFORE_ACTIONS_LISTED)),
                "save": state.get("save"),
                "instances": state.get("instances", {}),
                "brokers": state.get("brokers", {}),
                "now": state.get("now"),
                "markets": state.get("markets", []),
                "exposure": state.get("exposure", {}),
                "speed": state.get("speed"),
                "platforms": state.get("platforms", []),
            },
            "commands": history,
        }
        shown = {a["market_id"] for a in snap["activity"] + snap["order_issues"]}
        shown |= {p["market_id"] for p in snap["positions"]}
        shown |= {i["market_id"] for i in snap["tape"] if "market_id" in i}
        snap["trades"] = queries.trades(r)
        snap["trade_total"] = queries.trade_summary(r)["trades"]
        kinds = self.kinds(r, info, state)
        snap["kinds"] = kinds
        if queries.kinds_recorded(r):  # by what each fill and settlement was
            by_kind = queries.pnl_by_kind(r)
            snap["pnl_kinds"] = by_kind
            real_pnl = sum(k.get("live", 0.0) for k in by_kind.values())
            sim_pnl = sum(v for k in by_kind.values() for kind, v in k.items() if kind != "live")
            has_real = "live" in kinds.values() or any("live" in k for k in by_kind.values())
        else:  # older runs: by each instance's broker
            real_pnl = sum(s.pnl for k, s in summaries.items() if kinds.get(k) == "live")
            sim_pnl = sum(s.pnl for k, s in summaries.items() if kinds.get(k) != "live")
            has_real = "live" in kinds.values()
        if has_real:  # real money and simulated, never one total
            snap["stats"]["pnl_real"] = real_pnl
            snap["stats"]["pnl_simulated"] = sim_pnl
        snap["real"] = has_real
        # the server's rule for actions (a session with a live broker, armed or spare): the page asks for
        # the run's name by the same one
        snap["needs_confirm"] = has_real or self.is_real(r)
        snap["twins"] = self.twins(r, info)
        shown |= {t["market_id"] for t in snap["trades"]}
        snap["names"] = queries.names(r, shown)
        return snap

    def cursor(self, mode: str, name: str) -> dict[str, float]:
        """The latest row ids, to find what is new since the last push."""
        r = queries.run_by_name(self.data, f"{mode}/{name}")
        if r is None:
            return {}
        engine = queries._open(r.path)
        try:
            with Session(engine) as s:
                n_orders, filled = s.execute(select(func.count(), func.sum(OrderRow.filled))).one()
                return {
                    "fill": s.execute(select(func.max(FillRow.id))).scalar() or 0,
                    "settlement": s.execute(select(func.max(SettlementRow.id))).scalar() or 0,
                    "log": s.execute(select(func.max(LogRow.id))).scalar() or 0,
                    "orders": n_orders,
                    "filled": filled or 0.0,
                }
        finally:
            engine.dispose()

    def new_rows(self, mode: str, name: str, since: dict[str, float]) -> dict[str, list[dict[str, Any]]]:
        r = queries.run_by_name(self.data, f"{mode}/{name}")
        assert r is not None
        engine = queries._open(r.path)
        try:
            with Session(engine) as s:
                fills = s.execute(
                    select(FillRow).where(FillRow.id > since.get("fill", 0)).order_by(FillRow.id)
                ).scalars()
                settles = s.execute(
                    select(SettlementRow)
                    .where(SettlementRow.id > since.get("settlement", 0))
                    .order_by(SettlementRow.id)
                ).scalars()
                logs = s.execute(
                    select(LogRow).where(LogRow.id > since.get("log", 0)).order_by(LogRow.id)
                ).scalars()
                return {
                    "fills": [_row(x) for x in fills],
                    "settlements": [_row(x) for x in settles],
                    "logs": [_row(x) for x in logs],
                }
        finally:
            engine.dispose()

    async def accounts(self) -> list[dict[str, Any]]:
        """Each venue with API keys on this machine: its balance, read-only and cached (venues share
        rate limits with anything else trading the account, so this asks rarely)."""
        async with self._balance_lock:
            now = time.time()
            due = []
            for venue, api in self.apis.items():
                last = self._balances.get(venue)
                wait = BALANCE_BACKOFF_S if last and last.get("error") else BALANCE_EVERY_S
                if last is None or now - last["asked"] >= wait:
                    due.append((venue, api))
            if due:
                async with httpx.AsyncClient(timeout=10) as client:
                    for venue, api in due:
                        self._balances[venue] = await self._read_balance(client, venue, api, now)
        return [
            {k: v for k, v in b.items() if k != "asked"}
            for b in self._balances.values()
            if not b.get("no_keys")
        ]

    async def _read_balance(
        self, client: httpx.AsyncClient, venue: str, api: OrderAPI, now: float
    ) -> dict[str, Any]:
        last = self._balances.get(venue, {})
        base = {"venue": venue, "label": api.label or venue, "asked": now}
        try:
            api.signer  # noqa: B018 - raises when this venue's keys aren't configured
        except RuntimeError:
            return {**base, "no_keys": True}
        try:
            bal = await api.balance(client)
        except VenueError as exc:
            busy = exc.status == 429
            return {
                **base,
                **{k: last.get(k) for k in ("cash", "buying_power", "positions_value", "updated")},
                "error": "rate limited: retrying in 2 min" if busy else exc.detail[:200],
                "busy": busy,
            }
        except httpx.HTTPError as exc:
            return {
                **base,
                **{k: last.get(k) for k in ("cash", "buying_power", "positions_value", "updated")},
                "error": f"unreachable ({type(exc).__name__})",
                "busy": False,
            }
        return {
            **base,
            "cash": bal.cash,
            "buying_power": bal.buying_power,
            "positions_value": bal.positions_value,
            "updated": int(now * 1000),
            "error": None,
            "busy": False,
        }

    # --- control ---

    async def command(self, request: Request, mode: str, name: str, cmd: Command) -> dict[str, Any]:
        origin = request.headers.get("origin")
        if origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}":
            raise HTTPException(403, "cross-site request refused")
        if request.headers.get("x-quanty-token") != self.token:
            raise HTTPException(403, "missing or stale token: reload the page")
        run = f"{mode}/{name}"
        if queries.run_by_name(self.data, run) is None:
            raise HTTPException(404, f"no run {run}")
        if self.is_real(queries.run_by_name(self.data, run)) and cmd.confirm != name:  # type: ignore[arg-type]
            raise HTTPException(400, f"type {name} to confirm actions on a live run")
        store = ControlStore(self.control_path)
        try:
            session = store.session(run)
            if session is None:
                raise HTTPException(409, "this run is not running")
            known = session["state"].get("actions", BEFORE_ACTIONS_LISTED)
            if cmd.action not in known:
                raise HTTPException(
                    409,
                    f"this session's quanty is older than the dashboard and can't {cmd.action}: restart it",
                )
            try:
                cid = store.submit(run, cmd.action, cmd.target, cmd.value)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            for _ in range(30):  # sessions poll every second; wait briefly for the outcome
                await asyncio.sleep(0.1)
                row = next((c for c in store.history(run) if c["id"] == cid), None)
                if row is not None and row["status"] != "pending":
                    return {"ok": row["status"] == "done", "result": row["result"]}
            return {"ok": True, "result": f"sent {cmd.action} {cmd.target or 'all'}; waiting for the session"}
        finally:
            store.close()

    # --- live stream ---

    async def stream(self, ws: WebSocket, mode: str, name: str) -> None:
        """Pushes a snapshot whenever anything changes, plus the new fills, settlements and log
        rows since the last push (the dashboard's callouts)."""
        if self.auth is not None and not self.auth.valid(ws):
            await ws.close(code=1008)  # policy violation: not signed in
            return
        await ws.accept()
        last: dict[str, float] = {}
        beat = None
        try:
            while True:
                cur = await asyncio.to_thread(self.cursor, mode, name)
                session, _ = await asyncio.to_thread(self.control, f"{mode}/{name}")
                hb = session["heartbeat_ts"] if session else None
                if cur != last or hb != beat:
                    snap = await asyncio.to_thread(self.snapshot, mode, name)
                    new = await asyncio.to_thread(self.new_rows, mode, name, last) if last else None
                    await ws.send_json({"type": "snapshot", "data": snap, "new": new})
                    last, beat = cur, hb
                await asyncio.sleep(STREAM_POLL_S)
        except (WebSocketDisconnect, HTTPException):
            return


def router(api: Api) -> APIRouter:
    r = APIRouter(prefix="/api")

    @r.get("/runs")
    def runs() -> list[dict[str, Any]]:
        return api.runs()

    @r.get("/runs/{mode}/{name}")
    def run(mode: str, name: str) -> dict[str, Any]:
        return api.snapshot(mode, name)

    @r.get("/runs/{mode}/{name}/trades")
    def trades(
        mode: str,
        name: str,
        offset: int = 0,
        limit: int = Query(100, le=5000),
        instance: str | None = None,
        money: str | None = None,
        outcome: str | None = None,
        since: int | None = None,
        until: int | None = None,
        format: str = "json",
    ) -> Any:
        """The run's whole trade history, filtered and paged; format=csv downloads every match."""
        run = api.run_or_404(mode, name)
        try:
            who, real = api.money(run, money, instance)
            f: dict[str, Any] = {
                "instance": who,
                "real": real,
                "outcome": outcome or None,
                "since": since,
                "until": until,
            }
            if format == "csv":
                rows = queries.trades(run, limit=10**9, **f)
                return _csv(f"{name}-trades.csv", rows, queries.names(run, {t["market_id"] for t in rows}))
            rows = queries.trades(run, limit=limit, offset=offset, **f)
            return {
                "rows": rows,
                "summary": queries.trade_summary(run, **f),
                "names": queries.names(run, {t["market_id"] for t in rows}),
            }
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @r.get("/runs/{mode}/{name}/orders")
    def orders(
        mode: str,
        name: str,
        offset: int = 0,
        limit: int = Query(100, le=5000),
        instance: str | None = None,
        money: str | None = None,
        view: str | None = None,
        since: int | None = None,
        until: int | None = None,
        format: str = "json",
    ) -> Any:
        """The run's whole order history, filtered (view: look, stops, or one result) and paged."""
        run = api.run_or_404(mode, name)
        try:
            who, real = api.money(run, money, instance)
            f: dict[str, Any] = {"instance": who, "real": real, "since": since, "until": until}
            if format == "csv":
                rows = [_activity(a) for a in queries.activity(run, limit=10**9, view=view or None, **f)]
                return _csv(f"{name}-orders.csv", rows, queries.names(run, {a["market_id"] for a in rows}))
            rows = [
                _activity(a)
                for a in queries.activity(run, limit=limit, offset=offset, view=view or None, **f)
            ]
            return {
                "rows": rows,
                "stats": queries.order_stats(run, **f),
                "names": queries.names(run, {a["market_id"] for a in rows}),
            }
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @r.get("/runs/{mode}/{name}/compare")
    def compare(mode: str, name: str) -> dict[str, Any]:
        """Each live strategy beside its paper twin, in this run."""
        run = api.run_or_404(mode, name)
        twins = api.twins(run)
        if not twins:
            raise HTTPException(
                404, f"no live strategy in {mode}/{name} has a paper twin (quanty promote adds one)"
            )
        return queries.compare_twins(run, twins)

    @r.post("/runs/{mode}/{name}/control")
    async def control(request: Request, mode: str, name: str, cmd: Command) -> dict[str, Any]:
        return await api.command(request, mode, name, cmd)

    @r.get("/accounts")
    async def accounts() -> list[dict[str, Any]]:
        return await api.accounts()

    @r.get("/feeds")
    def feeds() -> list[dict[str, Any]]:
        return [
            {
                "feed": f.feed,
                "status": f.status,
                "latest": _row(f.latest),
                "lag_p99": f.lag_p99,
                "messages": f.messages,
            }
            for f in queries.feed_states(api.data)
        ]

    @r.websocket("/runs/{mode}/{name}/stream")
    async def stream(ws: WebSocket, mode: str, name: str) -> None:
        await api.stream(ws, mode, name)

    return r
