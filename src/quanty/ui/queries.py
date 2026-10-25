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
_STOP = (OrderRow.action == Action.SELL) & (OrderRow.tag == STOP_TAG)
_NEEDS_LOOK = (OrderRow.status != OrderStatus.FILLED) | (OrderRow.filled < OrderRow.qty - QTY_EPS) | _STOP
_REJECTED = OrderRow.status == OrderStatus.REJECTED
_RESTING = OrderRow.status.in_(RESTING)
_DONE = ~_REJECTED & ~_RESTING
_FULL = OrderRow.filled >= OrderRow.qty - QTY_EPS
_SOME = OrderRow.filled > QTY_EPS
# what each order view selects: one result (order_result's words), or look / stops
ORDER_VIEWS: dict[str, ColumnElement[bool]] = {
    "filled": _DONE & _FULL,
    "partial": _DONE & _SOME & ~_FULL,
    "missed": _DONE & ~_SOME & (OrderRow.tif == Tif.IOC),
    "unfilled": _DONE & ~_SOME & (OrderRow.tif != Tif.IOC),
    "rejected": _REJECTED,
    "resting": _RESTING,
    "look": _NEEDS_LOOK,
    "stops": _STOP & _SOME,
}


Instances = str | list[str] | None  # one instance, several (e.g. the real-money ones), or all


def _order_filter(instance: Instances, since: int | None, until: int | None) -> list[ColumnElement[bool]]:
    out: list[ColumnElement[bool]] = []
    if isinstance(instance, list):
        out.append(OrderRow.instance.in_(instance))
    elif instance:
        out.append(OrderRow.instance == instance)
    if since is not None:
        out.append(OrderRow.created_ts >= since)
    if until is not None:
        out.append(OrderRow.created_ts <= until)
    return out


def order_stats(
    run: Run, instance: Instances = None, since: int | None = None, until: int | None = None
) -> dict[str, dict[str, int]]:
    """Per instance, over every order (or those filtered): how many there were and how each kind of
    result ended (filled ... resting add up to orders), plus look (what the Orders view shows by
    default; also as needs_look) and stops (stop-loss sells that filled)."""

    def count(cond: ColumnElement[bool]) -> ColumnElement[int]:
        return func.sum(case((cond, 1), else_=0))

    engine = _open(run.path)
    try:
        with Session(engine) as conn:
            rows = conn.execute(
                select(
                    OrderRow.instance,
                    func.count().label("orders"),
                    *(count(cond).label(name) for name, cond in ORDER_VIEWS.items()),
                )
                .where(*_order_filter(instance, since, until))
                .group_by(OrderRow.instance)
            ).all()
    finally:
        engine.dispose()
    out = {}
    for r in rows:
        stats = {k: int(v or 0) for k, v in r._mapping.items() if k != "instance"}
        stats["needs_look"] = stats.pop("look")
        out[r.instance] = stats
    return out


def activity(
    run: Run,
    limit: int = 100,
    needs_look: bool = False,
    *,
    offset: int = 0,
    view: str | None = None,
    instance: Instances = None,
    since: int | None = None,
    until: int | None = None,
) -> list[Activity]:
    """Orders, newest first, each with what filled, at what price and cost. needs_look (or
    view="look"): only those that didn't simply fill in full, and stop losses; view can also be one
    result ("missed", ...) or "stops". offset/limit page through the history."""
    view = "look" if needs_look else view
    if view is not None and view not in ORDER_VIEWS:
        raise ValueError(f"unknown order view {view!r}")
    engine = _open(run.path)
    try:
        with Session(engine) as conn:
            q = select(OrderRow).where(*_order_filter(instance, since, until))
            if view is not None:
                q = q.where(ORDER_VIEWS[view])
            q = q.order_by(OrderRow.created_ts.desc(), OrderRow.id.desc()).offset(offset).limit(limit)
            orders = list(conn.execute(q).scalars())
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


TRADE_OUTCOMES = ("won", "lost", "stopped", "sold", "open")


def _positions(instance: Instances, since: int | None, until: int | None) -> Any:
    """Every position (instance, market, side) with what it bought and sold, its settlement and how
    it ended, as a subquery to filter, page and total."""
    key = (FillRow.instance, FillRow.market_id, FillRow.side)
    buy, sell = FillRow.action == Action.BUY, FillRow.action == Action.SELL
    q = (
        select(
            *key,
            func.min(FillRow.ts).label("opened"),
            func.max(FillRow.ts).label("last"),
            func.sum(case((buy, FillRow.qty), else_=0)).label("bought"),
            func.sum(case((buy, FillRow.qty * FillRow.price), else_=0)).label("cost"),
            func.sum(case((sell, FillRow.qty), else_=0)).label("sold"),
            func.sum(case((sell, FillRow.qty * FillRow.price), else_=0)).label("proceeds"),
            func.sum(FillRow.fee).label("fees"),
            func.sum(FillRow.realized).label("realized"),
            func.max(case((sell & (OrderRow.tag == STOP_TAG), 1), else_=0)).label("stopped"),
        )
        .join(OrderRow, OrderRow.id == FillRow.order_id, isouter=True)
        .group_by(*key)
    )
    if isinstance(instance, list):
        q = q.where(FillRow.instance.in_(instance))
    elif instance:
        q = q.where(FillRow.instance == instance)
    pos = q.subquery()
    st = SettlementRow
    closed_out = (pos.c.sold > QTY_EPS) & (pos.c.sold >= pos.c.bought - QTY_EPS)
    outcome = case(
        (st.id.is_not(None), case((st.payout > 0, "won"), else_="lost")),
        (closed_out, case((pos.c.stopped == 1, "stopped"), else_="sold")),
        else_="open",
    )
    q2 = select(
        pos,
        st.ts.label("settled_ts"),
        st.pnl.label("settle_pnl"),
        outcome.label("outcome"),
    ).join(
        st,
        (st.instance == pos.c.instance) & (st.market_id == pos.c.market_id) & (st.side == pos.c.side),
        isouter=True,
    )
    if since is not None:
        q2 = q2.where(pos.c.opened >= since)
    if until is not None:
        q2 = q2.where(pos.c.opened <= until)
    return q2.subquery()


def trades(
    run: Run,
    limit: int = 100,
    *,
    offset: int = 0,
    instance: Instances = None,
    outcome: str | None = None,
    since: int | None = None,
    until: int | None = None,
) -> list[dict[str, Any]]:
    """One row per position a strategy took (instance, market, side): what it bought and sold, what
    it cost, how it ended (won, lost, stopped: sold by a stop loss, sold, still open) and its P&L,
    most recently active first. Filters: instance, outcome, and when it was opened (ms)."""
    if outcome is not None and outcome not in TRADE_OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    t = _positions(instance, since, until)
    q = select(t)
    if outcome is not None:
        q = q.where(t.c.outcome == outcome)
    q = q.order_by(t.c.last.desc(), t.c.instance, t.c.market_id).offset(offset).limit(limit)
    engine = _open(run.path)
    try:
        with Session(engine) as conn:
            rows = conn.execute(q).all()
    finally:
        engine.dispose()
    out = []
    for r in rows:
        settled = r.settled_ts is not None
        if r.outcome == "open":
            pnl: float | None = r.realized or None  # open: only what's realized on part-sales so far
        else:
            pnl = (r.realized or 0.0) + (r.settle_pnl or 0.0)
        out.append(
            {
                "instance": r.instance,
                "market_id": r.market_id,
                "side": str(r.side),
                "opened": r.opened,
                "closed": r.settled_ts if settled else (r.last if r.outcome in ("sold", "stopped") else None),
                "bought": r.bought,
                "avg_price": r.cost / r.bought if r.bought else None,
                "cost": r.cost + (r.fees or 0.0),
                "sold": r.sold,
                "avg_sell": r.proceeds / r.sold if r.sold else None,
                "fees": r.fees or 0.0,
                "outcome": r.outcome,
                "stopped": bool(r.stopped),  # some of it was sold by a stop loss
                "pnl": pnl,
            }
        )
    return out


def trade_summary(
    run: Run,
    *,
    instance: Instances = None,
    outcome: str | None = None,
    since: int | None = None,
    until: int | None = None,
) -> dict[str, Any]:
    """Totals over every position the filters select: how many, by outcome, and net P&L."""
    t = _positions(instance, since, until)
    q = select(
        func.count().label("trades"),
        *(func.sum(case((t.c.outcome == o, 1), else_=0)).label(o) for o in TRADE_OUTCOMES),
        func.sum(func.coalesce(t.c.realized, 0) + func.coalesce(t.c.settle_pnl, 0)).label("pnl"),
        func.sum(t.c.cost + func.coalesce(t.c.fees, 0)).label("cost"),
    )
    if outcome is not None:
        q = q.where(t.c.outcome == outcome)
    engine = _open(run.path)
    try:
        with Session(engine) as conn:
            r = conn.execute(q).one()
    finally:
        engine.dispose()
    return {k: (v or 0) for k, v in r._mapping.items()}


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


def counterpart(data: Path, run: Run) -> Run | None:
    """The same experiment's run in the other mode (paper/<name> for live/<name>, and back)."""
    other = {"paper": "live", "live": "paper"}.get(run.mode)
    return run_by_name(data, f"{other}/{run.path.stem}") if other else None


def _first_order_ts(run: Run) -> int | None:
    engine = _open(run.path)
    try:
        with Session(engine) as conn:
            return conn.execute(select(func.min(OrderRow.created_ts))).scalar()
    finally:
        engine.dispose()


def _execution(run: Run, instance: str, since: int | None) -> dict[str, float | None]:
    """How an instance's buys filled: average price paid, and how far fills landed from the limit
    asked (negative: better than asked), contract-weighted; fees per contract."""
    buy = (FillRow.action == Action.BUY) & (FillRow.instance == instance)
    if since is not None:
        buy &= FillRow.ts >= since
    engine = _open(run.path)
    try:
        with Session(engine) as conn:
            r = conn.execute(
                select(
                    func.sum(FillRow.qty).label("qty"),
                    func.sum(FillRow.qty * FillRow.price).label("paid"),
                    func.sum(FillRow.qty * (FillRow.price - OrderRow.price)).label("vs_limit"),
                    func.sum(FillRow.fee).label("fees"),
                )
                .join(OrderRow, OrderRow.id == FillRow.order_id)
                .where(buy)
            ).one()
    finally:
        engine.dispose()
    q = r.qty or 0.0
    return {
        "avg_paid": r.paid / q if q else None,
        "vs_limit": r.vs_limit / q if q else None,
        "fees_per_contract": (r.fees or 0.0) / q if q else None,
        "contracts": q,
    }


def compare(paper: Run, live: Run) -> dict[str, Any]:
    """The same experiment's paper and live runs side by side, per instance in both, over the time
    both ran (from the later of their first orders): is live doing what paper did?"""
    starts = [t for t in (_first_order_ts(paper), _first_order_ts(live)) if t is not None]
    since = max(starts) if len(starts) == 2 else None
    out: dict[str, Any] = {"paper": paper.name, "live": live.name, "since": since, "instances": {}}
    stats = {"paper": order_stats(paper, since=since), "live": order_stats(live, since=since)}
    for iid in sorted(set(stats["paper"]) & set(stats["live"])):
        row = {}
        for side, run in (("paper", paper), ("live", live)):
            o = stats[side][iid]
            t = trade_summary(run, instance=iid, since=since)
            settled = t["won"] + t["lost"]
            closed = settled + t["stopped"] + t["sold"]
            row[side] = {
                "orders": o["orders"],
                "filled_pct": o["filled"] / o["orders"] if o["orders"] else None,
                "missed_pct": o["missed"] / o["orders"] if o["orders"] else None,
                "rejected": o["rejected"],
                "trades": t["trades"],
                "win_rate": t["won"] / settled if settled else None,
                "stops": t["stopped"],
                "pnl": t["pnl"],
                "pnl_per_trade": t["pnl"] / closed if closed else None,
                **_execution(run, iid, since),
            }
        out["instances"][iid] = row
    return out
