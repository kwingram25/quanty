"""Read-only views of ledgers and feed metrics, for the web UI.

Everything opens databases read-only, so the UI is safe to run beside paper and live sessions.
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, case, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from quanty import db
from quanty.core.ledger import Summary, summarize
from quanty.db.models import FeedMetricRow, FillRow, LogRow, MarketRow, OrderRow, SettlementRow
from quanty.enums import Action

MODES = ("live", "paper", "backtest")
STALE_S = 180  # a feed window older than this means the recorder isn't running


@dataclass(frozen=True)
class Run:
    name: str  # "<mode>/<file stem>"
    mode: str
    path: Path
    modified: float  # unix seconds


@dataclass(frozen=True)
class RunView:
    run: Run
    summaries: list[Summary]
    last_ts: int | None  # latest order, fill or log, ms
    errors: int  # error-level log rows

    @property
    def pnl(self) -> float:
        return sum(s.pnl for s in self.summaries)

    @property
    def fills(self) -> int:
        return sum(s.fills for s in self.summaries)


@dataclass(frozen=True)
class Position:
    instance: str
    market_id: str
    side: str
    qty: float
    avg_price: float


@dataclass(frozen=True)
class FeedState:
    feed: str
    latest: FeedMetricRow
    status: str  # ok / degraded / down / stale
    lag_p99: list[tuple[int, float]]  # (window end ms, p99 ms), oldest first
    messages: list[tuple[int, int]]


def find_runs(data: Path) -> list[Run]:
    out = []
    for mode in MODES:
        for path in sorted((data / mode).glob("*.sqlite")):
            out.append(Run(f"{mode}/{path.stem}", mode, path, path.stat().st_mtime))
    return sorted(out, key=lambda r: (MODES.index(r.mode), -r.modified))


def run_by_name(data: Path, name: str) -> Run | None:
    return next((r for r in find_runs(data) if r.name == name), None)


def _open(path: Path) -> Engine:
    return db.connect_readonly(path)


def view(run: Run) -> RunView:
    engine = _open(run.path)
    try:
        with Session(engine) as conn:
            summaries = summarize(conn.execute)
            last = [
                conn.execute(select(func.max(OrderRow.created_ts))).scalar(),
                conn.execute(select(func.max(FillRow.ts))).scalar(),
                conn.execute(select(func.max(LogRow.ts))).scalar(),
            ]
            errors = conn.execute(select(func.count()).where(LogRow.level == "error")).scalar() or 0
    finally:
        engine.dispose()
    known = [t for t in last if t is not None]
    return RunView(run, summaries, max(known) if known else None, errors)


def pnl_curves(run: Run) -> dict[str, list[tuple[int, float]]]:
    """Cumulative realized P&L per instance: sells (fills.realized) and settlements, by time."""
    engine = _open(run.path)
    try:
        with Session(engine) as conn:
            fills = conn.execute(
                select(FillRow.instance, FillRow.ts, FillRow.realized).where(FillRow.realized != 0)
            ).all()
            settles = conn.execute(select(SettlementRow.instance, SettlementRow.ts, SettlementRow.pnl)).all()
    finally:
        engine.dispose()
    out: dict[str, list[tuple[int, float]]] = {}
    for inst, ts, pnl in sorted([*fills, *settles], key=lambda r: (r[1], r[0])):
        series = out.setdefault(inst, [])
        series.append((ts, (series[-1][1] if series else 0.0) + pnl))
    return out


def positions(run: Run) -> list[Position]:
    """Open positions: contracts bought minus sold in markets not yet settled for that instance."""
    engine = _open(run.path)
    try:
        with Session(engine) as conn:
            settled = {
                (r.instance, r.market_id)
                for r in conn.execute(select(SettlementRow.instance, SettlementRow.market_id))
            }
            rows = conn.execute(
                select(
                    FillRow.instance,
                    FillRow.market_id,
                    FillRow.side,
                    func.sum(case((FillRow.action == Action.BUY, FillRow.qty), else_=-FillRow.qty)).label(
                        "net"
                    ),
                    func.sum(
                        case((FillRow.action == Action.BUY, FillRow.qty * FillRow.price), else_=0)
                    ).label("cost"),
                    func.sum(case((FillRow.action == Action.BUY, FillRow.qty), else_=0)).label("bought"),
                ).group_by(FillRow.instance, FillRow.market_id, FillRow.side)
            ).all()
    finally:
        engine.dispose()
    return [
        Position(r.instance, r.market_id, str(r.side), r.net, r.cost / r.bought if r.bought else 0.0)
        for r in rows
        if r.net > 1e-9 and (r.instance, r.market_id) not in settled
    ]


def recent(run: Run, limit: int = 50) -> dict[str, list[Any]]:
    """The latest orders, fills and notable log rows (warnings, errors, shadow and live broker events)."""
    engine = _open(run.path)
    try:
        with Session(engine) as conn:
            orders = list(
                conn.execute(select(OrderRow).order_by(OrderRow.created_ts.desc()).limit(limit)).scalars()
            )
            fills = list(conn.execute(select(FillRow).order_by(FillRow.ts.desc()).limit(limit)).scalars())
            logs = (
                conn.execute(
                    select(LogRow)
                    .where(
                        (LogRow.level != "info")
                        | LogRow.msg.like("shadow%")
                        | LogRow.msg.like("live%")
                        | LogRow.msg.like("leftover%")
                    )
                    .order_by(LogRow.ts.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
    finally:
        engine.dispose()
    return {"orders": orders, "fills": fills, "logs": list(logs)}


@dataclass(frozen=True)
class Activity:
    """An order with its fills folded in."""

    order: OrderRow
    fills: int
    avg_price: float | None
    fees: float
    realized: float
    liquidity: str  # maker / taker / mixed / ""


def activity(run: Run, limit: int = 100) -> list[Activity]:
    """The latest orders, newest first, each with what filled, at what price and cost."""
    engine = _open(run.path)
    try:
        with Session(engine) as conn:
            orders = list(
                conn.execute(select(OrderRow).order_by(OrderRow.created_ts.desc()).limit(limit)).scalars()
            )
            ids = [o.id for o in orders]
            agg = {
                r.order_id: r
                for r in conn.execute(
                    select(
                        FillRow.order_id,
                        func.count().label("n"),
                        func.sum(FillRow.qty).label("qty"),
                        func.sum(FillRow.qty * FillRow.price).label("cost"),
                        func.sum(FillRow.fee).label("fees"),
                        func.sum(FillRow.realized).label("realized"),
                        func.min(FillRow.liquidity).label("liq_lo"),
                        func.max(FillRow.liquidity).label("liq_hi"),
                    )
                    .where(FillRow.order_id.in_(ids))
                    .group_by(FillRow.order_id)
                )
            }
    finally:
        engine.dispose()
    out = []
    for o in orders:
        a = agg.get(o.id)
        if a is None:
            out.append(Activity(o, 0, None, 0.0, 0.0, ""))
            continue
        liq = str(a.liq_lo) if a.liq_lo == a.liq_hi else "mixed"
        out.append(Activity(o, a.n, a.cost / a.qty if a.qty else None, a.fees or 0.0, a.realized or 0.0, liq))
    return out


def names(run: Run, ids: set[str]) -> dict[str, dict[str, str]]:
    """What each market is (title, subtitle, event, series), for the ids the dashboard shows."""
    if not ids:
        return {}
    engine = _open(run.path)
    try:
        with Session(engine) as conn:
            rows = conn.execute(select(MarketRow).where(MarketRow.id.in_(ids))).scalars().all()
    except OperationalError:  # a ledger from before markets were recorded
        return {}
    finally:
        engine.dispose()
    return {
        r.id: {"title": r.title, "subtitle": r.subtitle, "event": r.event, "series": r.series} for r in rows
    }


def tape(run: Run, limit: int = 60) -> list[dict[str, Any]]:
    """Fills, settlements and notable log rows merged, newest first: what the event feed shows."""
    engine = _open(run.path)
    try:
        with Session(engine) as conn:
            fills = conn.execute(select(FillRow).order_by(FillRow.id.desc()).limit(limit)).scalars().all()
            settles = (
                conn.execute(select(SettlementRow).order_by(SettlementRow.id.desc()).limit(limit))
                .scalars()
                .all()
            )
            logs = (
                conn.execute(
                    select(LogRow)
                    .where(
                        (LogRow.level != "info")
                        | LogRow.msg.like("shadow%")
                        | LogRow.msg.like("live%")
                        | LogRow.msg.like("leftover%")
                    )
                    .order_by(LogRow.id.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
    finally:
        engine.dispose()
    items: list[dict[str, Any]] = []
    for f in fills:
        items.append(
            {
                "key": f"f{f.id}",
                "kind": "fill",
                "ts": f.ts,
                "instance": f.instance,
                "market_id": f.market_id,
                "side": str(f.side),
                "action": str(f.action),
                "price": f.price,
                "qty": f.qty,
                "fee": f.fee,
                "realized": f.realized,
                "liquidity": str(f.liquidity),
            }
        )
    for x in settles:
        items.append(
            {
                "key": f"s{x.id}",
                "kind": "settlement",
                "ts": x.ts,
                "instance": x.instance,
                "market_id": x.market_id,
                "side": str(x.side),
                "qty": x.qty,
                "payout": x.payout,
                "pnl": x.pnl,
            }
        )
    for g in logs:
        items.append(
            {
                "key": f"l{g.id}",
                "kind": "log",
                "ts": g.ts,
                "instance": g.instance,
                "level": g.level,
                "msg": g.msg,
                "fields": {k: v for k, v in (g.fields or {}).items() if k != "request"},
            }
        )
    items.sort(key=lambda i: (i["ts"], i["key"]), reverse=True)
    return items[:limit]


def feed_states(data: Path, hours: float = 2.0) -> list[FeedState]:
    path = data / "quanty.sqlite"
    if not path.exists():
        return []
    since = int((time.time() - hours * 3600) * 1000)
    engine = _open(path)
    try:
        with Session(engine) as conn:
            rows = (
                conn.execute(
                    select(FeedMetricRow).where(FeedMetricRow.end >= since).order_by(FeedMetricRow.end)
                )
                .scalars()
                .all()
            )
            if not rows:  # nothing recent: show each feed's last window, marked stale
                last = select(FeedMetricRow.feed, func.max(FeedMetricRow.end).label("end")).group_by(
                    FeedMetricRow.feed
                )
                ends = {r.feed: r.end for r in conn.execute(last)}
                rows = [
                    r
                    for r in conn.execute(select(FeedMetricRow)).scalars().all()
                    if ends.get(r.feed) == r.end
                ]
    finally:
        engine.dispose()
    by_feed: dict[str, list[FeedMetricRow]] = {}
    for r in rows:
        by_feed.setdefault(r.feed, []).append(r)
    now = time.time() * 1000
    out = []
    for feed, rs in sorted(by_feed.items()):
        latest = rs[-1]
        if now - latest.end > STALE_S * 1000:
            status = "stale"
        elif latest.messages == 0:
            status = "down"
        elif latest.parse_errors or latest.gaps or latest.reconnects:
            status = "degraded"
        else:
            status = "ok"
        out.append(
            FeedState(
                feed,
                latest,
                status,
                [(r.end, r.lag_p99) for r in rs if r.lag_p99 is not None],
                [(r.end, r.messages) for r in rs],
            )
        )
    return out
