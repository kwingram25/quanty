"""Read-only views of ledgers and feed metrics, for the web UI.

Everything opens databases read-only, so the UI is safe to run beside paper and live sessions.
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import ColumnElement, Engine, case, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from quanty import db
from quanty.core.ledger import Summary, summarize
from quanty.db.models import FeedMetricRow, FillRow, LogRow, MarketRow, OrderRow, SettlementRow
from quanty.enums import Action, OrderStatus, Tif

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

    @property
    def result(self) -> str:
        return order_result(self.order.status, self.order.filled, self.order.qty, self.order.tif)


QTY_EPS = 1e-9
RESTING = (OrderStatus.PENDING, OrderStatus.OPEN)
STOP_TAG = "stop"  # an exit a strategy tags this way is shown as a stop loss


def order_result(status: OrderStatus, filled: float, qty: float, tif: Tif) -> str:
    """How an order turned out, in one word: filled, partial (some filled, the rest didn't), missed
    (an IOC that found nothing), unfilled (a resting order canceled or expired with nothing),
    rejected, or resting (still on the book)."""
    if status == OrderStatus.REJECTED:
        return "rejected"
    if status in RESTING:
        return "resting"
    if filled >= qty - QTY_EPS:
        return "filled"
    if filled > QTY_EPS:
        return "partial"
    return "missed" if tif == Tif.IOC else "unfilled"


# orders worth a look: anything but filled in full, and every stop loss
_NEEDS_LOOK = (
    (OrderRow.status != OrderStatus.FILLED)
    | (OrderRow.filled < OrderRow.qty - QTY_EPS)
    | ((OrderRow.action == Action.SELL) & (OrderRow.tag == STOP_TAG))
)


def order_stats(run: Run) -> dict[str, dict[str, int]]:
    """Per instance, over every order: how many there were and how each kind of result ended (these
    add up to orders), plus needs_look (what the Orders view shows by default) and stops (stop-loss
    sells that filled)."""
    rejected = OrderRow.status == OrderStatus.REJECTED
    resting = OrderRow.status.in_(RESTING)
    done = ~rejected & ~resting
    full = OrderRow.filled >= OrderRow.qty - QTY_EPS
    some = OrderRow.filled > QTY_EPS

    def count(cond: ColumnElement[bool]) -> ColumnElement[int]:
        return func.sum(case((cond, 1), else_=0))

    engine = _open(run.path)
    try:
        with Session(engine) as conn:
            rows = conn.execute(
                select(
                    OrderRow.instance,
                    func.count().label("orders"),
                    count(done & full).label("filled"),
                    count(done & some & ~full).label("partial"),
                    count(done & ~some & (OrderRow.tif == Tif.IOC)).label("missed"),
                    count(done & ~some & (OrderRow.tif != Tif.IOC)).label("unfilled"),
                    count(rejected).label("rejected"),
                    count(resting).label("resting"),
                    count(_NEEDS_LOOK).label("needs_look"),
                    count((OrderRow.action == Action.SELL) & (OrderRow.tag == STOP_TAG) & some).label(
                        "stops"
                    ),
                ).group_by(OrderRow.instance)
            ).all()
    finally:
        engine.dispose()
    return {r.instance: {k: int(v or 0) for k, v in r._mapping.items() if k != "instance"} for r in rows}


def activity(run: Run, limit: int = 100, needs_look: bool = False) -> list[Activity]:
    """The latest orders, newest first, each with what filled, at what price and cost.
    needs_look: only those that didn't simply fill in full (missed, partial, rejected, resting...)."""
    engine = _open(run.path)
    try:
        with Session(engine) as conn:
            q = select(OrderRow).order_by(OrderRow.created_ts.desc()).limit(limit)
            orders = list(conn.execute(q.where(_NEEDS_LOOK) if needs_look else q).scalars())
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


def trades(run: Run, limit: int = 100) -> list[dict[str, Any]]:
    """One row per position a strategy took (instance, market, side): what it bought and sold, what
    it cost, how it ended (won, lost, sold, stopped: sold by a stop loss, still open) and its P&L,
    newest first."""
    engine = _open(run.path)
    try:
        with Session(engine) as conn:
            key = (FillRow.instance, FillRow.market_id, FillRow.side)
            rows = conn.execute(
                select(
                    *key,
                    func.min(FillRow.ts).label("opened"),
                    func.max(FillRow.ts).label("last"),
                    func.sum(case((FillRow.action == Action.BUY, FillRow.qty), else_=0)).label("bought"),
                    func.sum(
                        case((FillRow.action == Action.BUY, FillRow.qty * FillRow.price), else_=0)
                    ).label("cost"),
                    func.sum(case((FillRow.action == Action.SELL, FillRow.qty), else_=0)).label("sold"),
                    func.sum(
                        case((FillRow.action == Action.SELL, FillRow.qty * FillRow.price), else_=0)
                    ).label("proceeds"),
                    func.sum(FillRow.fee).label("fees"),
                    func.sum(FillRow.realized).label("realized"),
                    func.max(
                        case(((FillRow.action == Action.SELL) & (OrderRow.tag == STOP_TAG), 1), else_=0)
                    ).label("stopped"),
                )
                .join(OrderRow, OrderRow.id == FillRow.order_id, isouter=True)
                .group_by(*key)
                .order_by(func.max(FillRow.ts).desc())
                .limit(limit)
            ).all()
            settled = {
                (r.instance, r.market_id, str(r.side)): r
                for r in conn.execute(select(SettlementRow)).scalars()
            }
    finally:
        engine.dispose()
    out = []
    for r in rows:
        st = settled.get((r.instance, r.market_id, str(r.side)))
        pnl: float | None = (r.realized or 0.0) + (st.pnl if st else 0.0)
        if st is not None:
            outcome = "won" if st.payout > 0 else "lost"
        elif r.sold >= r.bought - 1e-9 and r.sold > 0:
            outcome = "stopped" if r.stopped else "sold"
        else:
            outcome, pnl = ("open", None) if not r.realized else ("open", r.realized)
        out.append(
            {
                "instance": r.instance,
                "market_id": r.market_id,
                "side": str(r.side),
                "opened": r.opened,
                "closed": st.ts if st else (r.last if outcome == "sold" else None),
                "bought": r.bought,
                "avg_price": r.cost / r.bought if r.bought else None,
                "cost": r.cost + (r.fees or 0.0),
                "sold": r.sold,
                "avg_sell": r.proceeds / r.sold if r.sold else None,
                "fees": r.fees or 0.0,
                "outcome": outcome,
                "stopped": bool(r.stopped),  # some of it was sold by a stop loss
                "pnl": pnl,
            }
        )
    return out


def strategy_info(run: Run) -> dict[str, dict[str, Any]]:
    """Per instance: what it is (logged when the run started) and how many markets it has traded."""
    engine = _open(run.path)
    try:
        with Session(engine) as conn:
            rows = (
                conn.execute(select(LogRow).where(LogRow.msg == "strategy").order_by(LogRow.id))
                .scalars()
                .all()
            )
            traded = dict(
                conn.execute(
                    select(FillRow.instance, func.count(func.distinct(FillRow.market_id))).group_by(
                        FillRow.instance
                    )
                ).all()
            )
    finally:
        engine.dispose()
    out: dict[str, dict[str, Any]] = {r.instance: dict(r.fields or {}) for r in rows}  # the latest start wins
    for inst, n in traded.items():
        out.setdefault(inst, {})["markets_traded"] = n
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
            tags = dict(
                conn.execute(
                    select(OrderRow.id, OrderRow.tag).where(OrderRow.id.in_({f.order_id for f in fills}))
                ).all()
            )
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
                "tag": tags.get(f.order_id, ""),
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
