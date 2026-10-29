"""The event loop shared by backtest, paper, shadow and live.

Events are processed one at a time in `ts` order. Engine state (markets, books, latest data) is
updated first, then brokers see the event (a simulated broker may fill resting orders), then
strategies. Orders a strategy places are validated and risk-checked, then sent to its instance's
broker; fills and order updates come back as events, so nothing re-enters a strategy mid-call.
Events scheduled for the same ts as the next input are processed before it.
"""

import contextlib
import functools
import heapq
import itertools
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from quanty.core.broker import Broker
from quanty.core.events import (
    Book,
    BrokerEvent,
    Clock,
    DataEvent,
    Event,
    Fill,
    MarketClose,
    MarketOpen,
    MarketTrade,
    OrderUpdate,
    Settled,
)
from quanty.core.ledger import Ledger
from quanty.core.orders import Order
from quanty.core.portfolio import Portfolio
from quanty.core.risk import Limits, Risk
from quanty.core.strategy import Params, Strategy
from quanty.core.types import Action, Market, OrderStatus, Side, Tif

if TYPE_CHECKING:
    from quanty.signals import Signal

Call = Callable[[Strategy[Any], "Context"], None]


@dataclass
class Instance:
    """One running copy of a strategy: its params, broker and limits."""

    id: str
    strategy: Strategy[Any]
    broker: Broker
    limits: Limits = field(default_factory=Limits)
    twin_of: str | None = None  # a paper copy of this live instance, for the dashboard's Compare


def describe(inst: Instance, code: Mapping[str, Any] | None = None) -> dict[str, object]:
    """What an instance is: its strategy, description, the series it trades, every param (and which
    ones its config set), broker and limits, and the code it ran (`code`: the project's version, see
    quanty.version; plus a hash of the strategy's file). Logged when a run starts, for the dashboard."""
    from quanty.version import file_hash

    strategy, params = inst.strategy, inst.strategy.params
    doc = (type(strategy).__doc__ or "").strip().splitlines()
    module_doc = (sys.modules[type(strategy).__module__].__doc__ or "").strip()
    return {
        "strategy": type(strategy).__module__.rsplit(".", 1)[-1],
        "description": strategy.description or (doc[0] if doc else ""),
        "about": module_doc,
        "series": list(getattr(params, "series", None) or strategy.series),
        "params": params.model_dump(mode="json"),
        "params_set": sorted(params.model_fields_set),
        "param_help": {k: f.description or "" for k, f in type(params).model_fields.items()},
        "broker": inst.broker.name,
        "broker_kind": inst.broker.kind,  # live = real money
        "limits": inst.limits.model_dump(exclude_none=True),
        "twin_of": inst.twin_of,
        "code": {
            **(code or {}),
            "file": file_hash(getattr(sys.modules[type(strategy).__module__], "__file__", None)),
        },
    }


class Context:
    """What a strategy sees and does. One per instance."""

    def __init__(self, engine: "Engine", instance: Instance) -> None:
        self._e = engine
        self._i = instance

    @property
    def id(self) -> str:
        return self._i.id

    @property
    def now(self) -> int:
        return self._e.now

    def markets(self) -> list[Market]:
        """Open markets in this strategy's series."""
        return [m for m in self._e.open_markets.values() if self._i.strategy.wants(m)]

    def market(self, market_id: str) -> Market:
        return self._e.markets[market_id]

    def outcomes(self, market_id: str) -> list[Market]:
        """The open markets of this market's question when at most one of them can win (the term
        `exclusive`), e.g. every bucket of a temperature ladder; just this market otherwise."""
        ev = self._e.risk.exclusive_event(market_id)
        if ev is None:
            return [self.market(market_id)]
        return [m for m in self._e.open_markets.values() if self._e.risk.exclusive_event(m.id) == ev]

    def seconds_left(self, market_id: str) -> float:
        return (self._e.markets[market_id].close_ts - self._e.now) / 1000

    def book(self, market_id: str) -> Book | None:
        return self._e.books.get(market_id)

    def latest(self, key: str) -> DataEvent | None:
        """The most recent data event with this key, e.g. "coinbase:BTC-USD"."""
        return self._e.latest.get(key)

    def signal(self, name: str) -> float | None:
        """Current value of a configured signal; None until it has enough data."""
        return self._e.signals[name].value

    def position(self, market_id: str, side: Side) -> float:
        return self._e.portfolio.position(self._i.id, market_id, side).qty

    def entry_price(self, market_id: str, side: Side) -> float | None:
        """Average cost per contract held on that side, buy fees included (None when not holding):
        what a price-based stop compares the bid with."""
        pos = self._e.portfolio.position(self._i.id, market_id, side)
        return pos.avg_cost if pos.qty > 1e-9 else None

    def orders(self, market_id: str | None = None) -> list[Order]:
        """This instance's live orders."""
        return [
            o for o in self._e.live.values() if o.instance == self._i.id and market_id in (None, o.market_id)
        ]

    def buy(
        self,
        market_id: str,
        side: Side,
        price: float,
        qty: float,
        *,
        tif: Tif = Tif.GTC,
        post_only: bool = False,
        tag: str = "",
    ) -> Order:
        """Place a buy; the returned order is REJECTED (with a reason) if it fails checks."""
        return self._e.place(self._i, market_id, side, Action.BUY, price, qty, tif, post_only, tag)

    def sell(
        self,
        market_id: str,
        side: Side,
        price: float,
        qty: float,
        *,
        tif: Tif = Tif.GTC,
        post_only: bool = False,
        tag: str = "",
    ) -> Order:
        """Sell contracts you hold (no shorting: to bet against YES, buy NO)."""
        return self._e.place(self._i, market_id, side, Action.SELL, price, qty, tif, post_only, tag)

    def cancel(self, order: Order | str) -> None:
        self._e.cancel(order if isinstance(order, str) else order.id)

    def cancel_all(self, market_id: str | None = None) -> None:
        for o in self.orders(market_id):
            self._e.cancel(o.id)

    def log(self, msg: str, **fields: object) -> None:
        self._e.ledger.log(self._e.now, self._i.id, msg, fields)

    def estimate(self, market_id: str, yes: float) -> None:
        """Report this strategy's own probability that the market settles YES (its model's view), for
        the dashboard to show beside the market's price. Display only: nothing trades on it, and it's
        kept until the market closes (the latest one wins)."""
        if not 0.0 <= yes <= 1.0:
            raise ValueError(f"estimate for {market_id}: {yes} is not a probability between 0 and 1")
        self._e.estimates[market_id][self._i.id] = yes


def _open_hook(market: Market, strategy: Strategy[Any], ctx: Context) -> None:
    strategy.on_market_open(ctx, market)


class Engine:
    def __init__(
        self,
        instances: Sequence[Instance],
        *,
        ledger: Ledger | None = None,
        limits: Limits | None = None,
        clock_ms: int | None = 1000,
        strict: bool = True,
        signals: Sequence["Signal"] = (),
        spare_brokers: Sequence[Broker] = (),
        code: Mapping[str, Any] | None = None,
    ) -> None:
        """strict re-raises strategy errors (backtests); otherwise the instance is logged and halted.
        code: the project's code version (quanty.version.project), recorded with each strategy."""
        self.code = dict(code or {})
        self.instances = {i.id: i for i in instances}
        if len(self.instances) != len(instances):
            raise ValueError("instance ids must be unique")
        self.ledger = ledger or Ledger()
        self.ledger.kinds.update({i.id: i.broker.kind for i in instances})
        self.limits = limits or Limits()
        self.clock_ms = clock_ms
        self.strict = strict
        self.now = 0
        self.markets: dict[str, Market] = {}
        self.open_markets: dict[str, Market] = {}
        self.books: dict[str, Book] = {}
        self.latest: dict[str, DataEvent] = {}
        self.orders: dict[str, Order] = {}  # every order this run
        self.live: dict[str, Order] = {}  # orders not yet done
        self.portfolio = Portfolio()
        self.risk = Risk(self.portfolio, self.live, self.markets)
        self.halted: set[str] = set()
        self.blocked: dict[str, int] = defaultdict(int)  # buys refused while halted, per instance
        self.estimates: dict[str, dict[str, float]] = defaultdict(dict)  # market -> instance -> P(YES)
        self.signals = {s.name: s for s in signals}
        if len(self.signals) != len(signals):
            raise ValueError("signal names must be unique")
        self._signals_by_key: dict[str, list[Signal]] = {}
        for sig in signals:
            self._signals_by_key.setdefault(sig.key, []).append(sig)
        self._ctx = {i.id: Context(self, i) for i in instances}
        for inst in instances:  # what each instance is, so a ledger can say what was run
            self.ledger.log(self.now, inst.id, "strategy", describe(inst, self.code))
        self._brokers: dict[str, Broker] = {}
        for b in [*(i.broker for i in instances), *spare_brokers]:  # spares: to switch instances onto
            if self._brokers.setdefault(b.name, b) is not b:
                raise ValueError(f"two different brokers are named {b.name!r}")
        for broker in self._brokers.values():
            broker.attach(self)
        # (instance, market) -> the broker its position and orders there belong to, where that isn't
        # its broker now: an instance switched (or restored) keeps each market it's in on the broker
        # it entered with until that market settles. None: that broker isn't in this session.
        self._held_by: dict[tuple[str, str], Broker | None] = {}
        self._order_broker: dict[str, Broker] = {}
        self._queue: list[tuple[int, int, Event]] = []
        self._seq = itertools.count()
        # continue a run's numbering: a restarted session must not reuse (and overwrite) its orders
        self._order_seq = itertools.count(self.ledger.last_order_number() + 1)
        self._warming = False
        self._next_clock: int | None = None
        self._opens: list[tuple[int, str]] = []
        self._closes: list[tuple[int, str]] = []
        self._started = False

    # --- running ---

    def run(self, events: Iterable[Event]) -> None:
        """Process a finite ts-ordered stream (backtest, replay), then everything still queued."""
        for ev in events:
            self.advance(ev.ts)
            self.process(ev)
        self.advance(None)
        self.ledger.commit()

    def schedule(self, event: Event) -> None:
        heapq.heappush(self._queue, (event.ts, next(self._seq), event))

    def advance(self, until: int | None) -> None:
        """Process queued events and clock ticks due at or before `until`; None drains the queue."""
        while True:
            queued = self._queue[0][0] if self._queue else None
            clock = self._next_clock if until is not None else None
            if queued is not None and (until is None or queued <= until):
                if clock is not None and clock < queued:
                    self._tick(clock)
                else:
                    self.process(heapq.heappop(self._queue)[2])
            elif clock is not None and until is not None and clock <= until:
                self._tick(clock)
            else:
                return

    def process(self, ev: Event) -> None:
        if not self._started:
            self._start(ev.ts)
        self.now = max(self.now, ev.ts)
        self._open_and_close_due()
        match ev:
            case BrokerEvent():
                self._brokers[ev.broker].handle(ev)
            case Fill():
                self._on_fill(ev)
            case OrderUpdate():
                self._on_update(ev)
            case MarketOpen():
                self._announce(ev)
            case Book():
                self.books[ev.market_id] = ev
                self._to_brokers(ev)
                self._each(ev.market_id, lambda s, c: s.on_book(c, ev))
            case MarketTrade():
                self._to_brokers(ev)
                self._each(ev.market_id, lambda s, c: s.on_trade(c, ev))
            case DataEvent():
                self.latest[ev.key] = ev
                self._to_brokers(ev)
                for sig in self._signals_by_key.get(ev.key, ()):
                    sig.update(ev)
                self._each(None, lambda s, c: s.on_data(c, ev))
            case Clock():
                self._each(None, lambda s, c: s.on_clock(c, ev))
            case MarketClose():
                self._close(ev)
            case Settled():
                self._settle(ev)
            case _:
                raise TypeError(f"unhandled event {ev!r}")

    def _start(self, ts: int) -> None:
        self._started = True
        self.now = ts
        if self.clock_ms:
            self._next_clock = (ts // self.clock_ms + 1) * self.clock_ms
        self._each(None, lambda s, c: s.on_start(c))

    def _tick(self, ts: int) -> None:
        assert self.clock_ms is not None
        self._next_clock = ts + self.clock_ms
        self.process(Clock(ts=ts))

    # --- orders ---

    def place(
        self,
        inst: Instance,
        market_id: str,
        side: Side,
        action: Action,
        price: float,
        qty: float,
        tif: Tif,
        post_only: bool,
        tag: str,
    ) -> Order:
        if self._warming:  # replaying the recent past: decisions are rebuilt, nothing is placed
            return Order(
                id=f"{inst.id}-warmup",
                instance=inst.id,
                market_id=market_id,
                side=side,
                action=action,
                price=price,
                qty=qty,
                tif=tif,
                post_only=post_only,
                tag=tag,
                created_ts=self.now,
                status=OrderStatus.REJECTED,
                reason="warming up",
            )
        order = Order(
            id=f"{inst.id}-{next(self._order_seq)}",
            instance=inst.id,
            market_id=market_id,
            side=side,
            action=action,
            price=round(price, 9),
            qty=qty,
            tif=tif,
            post_only=post_only,
            tag=tag,
            created_ts=self.now,
        )
        broker = self._held_by.get((inst.id, market_id), inst.broker)
        if broker is None:
            order.status, order.reason = (
                OrderStatus.REJECTED,
                "it holds this market on a live broker this session doesn't have",
            )
            self.orders[order.id] = order
            self.ledger.order(order, kind="live")
            return order
        reason = self._invalid(order) or self._risk(inst, order)
        if reason == "halted":  # expected while halted: counted, not recorded order by order
            order.status, order.reason = OrderStatus.REJECTED, reason
            self.blocked[inst.id] += 1
            return order
        self.orders[order.id] = order
        if reason:
            order.status, order.reason = OrderStatus.REJECTED, reason
            self.ledger.order(order, kind=broker.kind)
            return order
        self.live[order.id] = order
        self._order_broker[order.id] = broker
        self.ledger.order(order, kind=broker.kind)
        broker.submit(order)
        return order

    def cancel(self, order_id: str) -> None:
        order = self.live.get(order_id)
        if order is not None:
            self._order_broker.get(order.id, self.instances[order.instance].broker).cancel(order)

    @contextlib.contextmanager
    def warming(self) -> Iterator[None]:
        """Within: replay recent events so strategies, models and signals rebuild their state before a
        session trades. Orders are refused (never reaching a broker) and nothing is written to the
        run's ledger. Do it before restore(), so replayed settlements find no positions to pay."""
        real, self.ledger = self.ledger, Ledger()
        self.ledger.kinds = dict(real.kinds)
        self._warming = True
        try:
            yield
        finally:
            self._warming = False
            self.ledger.close()
            self.ledger = real

    def restore(self) -> dict[str, int]:
        """Rebuild positions and realized (and daily) P&L from the run's recorded fills and
        settlements: a restarted session knows what its instances hold. Orders an earlier session
        left open are marked canceled (nothing tracks them any more). Returns what it found."""
        from sqlalchemy import select

        from quanty.db.models import FillRow, SettlementRow

        rows: list[tuple[int, int, Any]] = []
        for model in (FillRow, SettlementRow):
            q = select(model).where(model.instance.in_(list(self.instances)))
            rows += [(r.ts, 0 if model is FillRow else 1, r) for r in self.ledger.execute(q).scalars()]
        skipped = 0
        for _, kind, r in sorted(rows, key=lambda x: (x[0], x[1], x[2].id)):
            if kind == 1:
                self.portfolio.restore_settlement(r.instance, r.market_id, r.side, r.pnl, r.ts)
                continue
            fill = Fill(
                ts=r.ts,
                order_id=r.order_id,
                market_id=r.market_id,
                side=r.side,
                action=r.action,
                price=r.price,
                qty=r.qty,
                fee=r.fee,
                liquidity=r.liquidity,
            )
            try:
                self.portfolio.apply(r.instance, fill)
            except ValueError:  # a sell without its buy (a partial history): nothing to restore
                skipped += 1
        bought_as = {
            (r.instance, r.market_id): r.kind for _, k, r in rows if k == 0 and r.action == Action.BUY
        }
        by_kind = {b.kind: b for b in self._brokers.values()}  # the first of each kind
        unplaceable = 0
        for iid, mid, _, p in list(self.portfolio.open_positions()):
            kind = bought_as.get((iid, mid), self.instances[iid].broker.kind)
            if p.qty > 1e-9 and kind != self.instances[iid].broker.kind:
                broker = by_kind.get(kind) or (None if kind == "live" else by_kind.get("sim"))
                self._held_by[(iid, mid)] = broker
                unplaceable += broker is None
        open_ = sum(1 for *_, p in self.portfolio.open_positions() if p.qty > 1e-9)
        # orders an earlier session left resting: they lived in its brokers, which this session
        # doesn't have (a live broker's journal cancels its own at the venue on start)
        left = self.ledger.close_leftovers(self.instances, "left open when its session stopped")
        return {
            "fills": sum(1 for _, k, _ in rows if k == 0),
            "open": open_,
            "skipped": skipped,
            "live_without_broker": unplaceable,
            "orders_closed": left,
        }

    def live_holdings(self) -> dict[str, dict[str, float]]:
        """What the instances hold for real: market -> instance -> net YES contracts (− = NO), for
        positions bought through a live broker (kept on it, or on none when it isn't in this
        session)."""
        out: dict[str, dict[str, float]] = {}
        for iid, mid, side, p in self.portfolio.open_positions():
            if p.qty <= 1e-9:
                continue
            key = (iid, mid)
            holder = self._held_by[key] if key in self._held_by else self.instances[iid].broker
            if holder is not None and holder.kind != "live":
                continue
            net = p.qty if side is Side.YES else -p.qty
            out.setdefault(mid, {})[iid] = round(out.get(mid, {}).get(iid, 0.0) + net, 9)
        return out

    @property
    def brokers(self) -> dict[str, Broker]:
        """Every broker of this session, by name: the instances' and the spares."""
        return dict(self._brokers)

    def switch(self, instance_id: str, broker_name: str) -> list[str]:
        """Move an instance onto another of this session's brokers for every market it enters from
        now on. Markets where it holds a position or has orders stay on the broker they're on until
        they settle (a simulated position is never sold for real, nor a real one in simulation).
        Returns those markets."""
        inst = self.instances[instance_id]
        new = self._brokers.get(broker_name)
        if new is None:
            raise ValueError(f"no broker {broker_name!r} in this session (have: {', '.join(self._brokers)})")
        if new is inst.broker:
            raise ValueError(f"{instance_id} already runs on {broker_name}")
        busy = {m for i, m, _, p in self.portfolio.open_positions(instance_id) if p.qty > 1e-9}
        busy |= {o.market_id for o in self.live.values() if o.instance == instance_id}
        for m in busy:
            self._held_by.setdefault((instance_id, m), inst.broker)
        inst.broker = new
        self.ledger.kinds[instance_id] = new.kind
        self.ledger.log(
            self.now, instance_id, "strategy", describe(inst, self.code)
        )  # the dashboard: its kind now
        return sorted(busy)

    def add_broker(self, broker: Broker) -> None:
        """Add a broker to a running session, for instances to start on or switch onto. It's given
        each market's latest book, so it can fill from the next order rather than the next book."""
        if broker.name in self._brokers:
            raise ValueError(f"there is already a broker {broker.name!r}")
        self._brokers[broker.name] = broker
        broker.attach(self)
        for book in self.books.values():
            broker.on_event(book)

    def add_instance(self, inst: Instance) -> None:
        """Start another instance in a running session (a paper twin, a new variant). It sees the
        markets already open; its broker must be one of this session's."""
        if inst.id in self.instances:
            raise ValueError(f"there is already an instance {inst.id!r}")
        if self._brokers.get(inst.broker.name) is not inst.broker:
            raise ValueError(f"{inst.id}: broker {inst.broker.name!r} isn't one of this session's")
        self.instances[inst.id] = inst
        self._ctx[inst.id] = Context(self, inst)
        self.ledger.kinds[inst.id] = inst.broker.kind
        self.ledger.log(self.now, inst.id, "strategy", describe(inst, self.code))
        self._guard(inst, lambda s, c: s.on_start(c))
        for m in list(self.open_markets.values()):
            if inst.strategy.wants(m):
                self._guard(inst, functools.partial(_open_hook, m))

    def set_params(self, instance_id: str, params: Params) -> None:
        """Swap an instance's params while running; strategies see them from the next event."""
        inst = self.instances[instance_id]
        old, inst.strategy.params = inst.strategy.params, params
        inst.strategy.on_params(self._ctx[instance_id], old)

    def halt(self, instance_id: str | None = None, reason: str = "") -> None:
        """Kill switch: no new buys and resting buys canceled, for one instance or all.

        Sells (exits, stops) still go through so positions can be closed.
        """
        ids = [instance_id] if instance_id else list(self.instances)
        for iid in ids:
            self.halted.add(iid)
            self.ledger.log(self.now, iid, "halted", {"reason": reason}, level="warning")
        for o in list(self.live.values()):
            if o.instance in ids and o.action is Action.BUY:
                self.cancel(o.id)

    def resume(self, instance_id: str | None = None) -> None:
        for iid in [instance_id] if instance_id else list(self.instances):
            self.halted.discard(iid)
            self.ledger.log(self.now, iid, "resumed", {"buys_blocked": self.blocked.pop(iid, 0)})

    def _invalid(self, o: Order) -> str | None:
        m = self.open_markets.get(o.market_id)
        if m is None:
            return f"market {o.market_id} is not open"
        if not m.valid_qty(o.qty):
            return f"qty must be a positive multiple of {m.qty_step:g}"
        if not 0 < o.price < 1:
            return "price must be between 0 and 1"
        if not m.on_tick(o.price):
            return f"price {o.price} is not on the {m.tick_at(o.price):g} tick"
        return None

    def _risk(self, inst: Instance, o: Order) -> str | None:
        if o.action is Action.SELL:
            return self.risk.check_sell(o)
        if inst.id in self.halted:
            return "halted"
        return self.risk.check(o, inst.limits, inst.id) or self.risk.check(o, self.limits, None)

    def _on_fill(self, f: Fill) -> None:
        order = self.orders[f.order_id]
        order.filled += f.qty
        realized = self.portfolio.apply(order.instance, f)
        self.ledger.fill(order.instance, f, realized)
        self.ledger.order(order)
        self._guard(self.instances[order.instance], lambda s, c: s.on_fill(c, f))

    def _on_update(self, u: OrderUpdate) -> None:
        order = self.orders[u.order_id]
        order.status, order.reason = u.status, u.reason or order.reason
        if order.status.done:
            self.live.pop(order.id, None)
        self.ledger.order(order)
        self._guard(self.instances[order.instance], lambda s, c: s.on_order(c, order))

    # --- lifecycle ---

    def _announce(self, ev: MarketOpen) -> None:
        """A market is known from its announcement and open only between open_ts and close_ts.

        Venues list markets ahead of time; a repeat announcement updates the terms (e.g. a strike
        published at the open) without reopening it.
        """
        m = ev.market
        known = m.id in self.markets
        self.markets[m.id] = m
        if any(i.strategy.wants(m) for i in self.instances.values()):
            self.ledger.market(m)  # named in the ledger (and the dashboard)
        if m.id in self.open_markets:
            self.open_markets[m.id] = m
        if known:
            return
        self._to_brokers(ev)
        if m.close_ts <= self.now:
            return  # already over: known only for its settlement
        heapq.heappush(self._closes, (m.close_ts, m.id))
        if m.open_ts <= self.now:
            self._open(m.id)
        else:
            heapq.heappush(self._opens, (m.open_ts, m.id))

    def _open(self, market_id: str) -> None:
        m = self.markets[market_id]
        self.open_markets[market_id] = m
        self._each(market_id, lambda s, c: s.on_market_open(c, m))

    def _open_and_close_due(self) -> None:
        """Markets open and close on time even when no event marks the moment."""
        while self._opens and self._opens[0][0] <= self.now:
            _, mid = heapq.heappop(self._opens)
            if self.markets[mid].close_ts > self.now and mid not in self.open_markets:
                self._open(mid)
        while self._closes and self._closes[0][0] <= self.now:
            _, mid = heapq.heappop(self._closes)
            if mid in self.open_markets:
                self._close(MarketClose(ts=self.now, market_id=mid))

    def _close(self, ev: MarketClose) -> None:
        m = self.open_markets.pop(ev.market_id, None)
        self.estimates.pop(ev.market_id, None)
        self._to_brokers(ev)
        if m is not None:
            self._each(m.id, lambda s, c: s.on_market_close(c, m))

    def _settle(self, ev: Settled) -> None:
        if ev.market_id in self.open_markets:
            self._close(MarketClose(ts=ev.ts, market_id=ev.market_id))
        for s in self.portfolio.settle(ev.market_id, ev.outcome, ev.ts):
            held = self._held_by.pop((s.instance, s.market_id), self.instances[s.instance].broker)
            self.ledger.settlement(s, kind=held.kind if held is not None else "live")
        self._to_brokers(ev)
        m = self.markets.get(ev.market_id)
        if m is not None:
            self._each(m.id, lambda s, c: s.on_settled(c, m, ev.outcome))

    # --- dispatch ---

    def _to_brokers(self, ev: Event) -> None:
        for b in self._brokers.values():
            b.on_event(ev)

    def _each(self, market_id: str | None, call: Call) -> None:
        market = self.markets.get(market_id) if market_id is not None else None
        if market_id is not None and market is None:
            return  # data for a market we never saw open
        for inst in self.instances.values():
            if market is None or inst.strategy.wants(market):
                self._guard(inst, call)

    def _guard(self, inst: Instance, call: Call) -> None:
        try:
            call(inst.strategy, self._ctx[inst.id])
        except Exception as exc:
            if self.strict:
                raise
            self.ledger.log(self.now, inst.id, "strategy error", {"error": repr(exc)}, level="error")
            self.halt(inst.id, reason=f"strategy error: {exc!r}")
