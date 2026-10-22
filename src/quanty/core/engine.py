"""The event loop shared by backtest, paper, shadow and live.

Events are processed one at a time in `ts` order. Engine state (markets, books, latest data) is
updated first, then brokers see the event (a simulated broker may fill resting orders), then
strategies. Orders a strategy places are validated and risk-checked, then sent to its instance's
broker; fills and order updates come back as events, so nothing re-enters a strategy mid-call.
Events scheduled for the same ts as the next input are processed before it.
"""

import heapq
import itertools
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

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
from quanty.core.strategy import Strategy
from quanty.core.types import Action, Market, OrderStatus, Side, Tif

Call = Callable[[Strategy[Any], "Context"], None]


@dataclass
class Instance:
    """One running copy of a strategy: its params, broker and limits."""

    id: str
    strategy: Strategy[Any]
    broker: Broker
    limits: Limits = field(default_factory=Limits)


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

    def seconds_left(self, market_id: str) -> float:
        return (self._e.markets[market_id].close_ts - self._e.now) / 1000

    def book(self, market_id: str) -> Book | None:
        return self._e.books.get(market_id)

    def latest(self, key: str) -> DataEvent | None:
        """The most recent data event with this key, e.g. "coinbase:BTC-USD"."""
        return self._e.latest.get(key)

    def position(self, market_id: str, side: Side) -> int:
        return self._e.portfolio.position(self._i.id, market_id, side).qty

    def orders(self, market_id: str | None = None) -> list[Order]:
        """This instance's live orders."""
        return [
            o for o in self._e.live.values()
            if o.instance == self._i.id and market_id in (None, o.market_id)
        ]  # fmt: skip

    def buy(
        self, market_id: str, side: Side, price: float, qty: int, *,
        tif: Tif = Tif.GTC, post_only: bool = False, tag: str = "",
    ) -> Order:  # fmt: skip
        """Place a buy; the returned order is REJECTED (with a reason) if it fails checks."""
        return self._e.place(self._i, market_id, side, Action.BUY, price, qty, tif, post_only, tag)

    def sell(
        self, market_id: str, side: Side, price: float, qty: int, *,
        tif: Tif = Tif.GTC, post_only: bool = False, tag: str = "",
    ) -> Order:  # fmt: skip
        """Sell contracts you hold (no shorting: to bet against YES, buy NO)."""
        return self._e.place(self._i, market_id, side, Action.SELL, price, qty, tif, post_only, tag)

    def cancel(self, order: Order | str) -> None:
        self._e.cancel(order if isinstance(order, str) else order.id)

    def cancel_all(self, market_id: str | None = None) -> None:
        for o in self.orders(market_id):
            self._e.cancel(o.id)

    def log(self, msg: str, **fields: object) -> None:
        self._e.ledger.log(self._e.now, self._i.id, msg, fields)


class Engine:
    def __init__(
        self,
        instances: Sequence[Instance],
        *,
        ledger: Ledger | None = None,
        limits: Limits | None = None,
        clock_ms: int | None = 1000,
        strict: bool = True,
    ) -> None:
        """strict re-raises strategy errors (backtests); otherwise the instance is logged and halted."""
        self.instances = {i.id: i for i in instances}
        if len(self.instances) != len(instances):
            raise ValueError("instance ids must be unique")
        self.ledger = ledger or Ledger()
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
        self._ctx = {i.id: Context(self, i) for i in instances}
        self._brokers: dict[str, Broker] = {}
        for inst in instances:
            if self._brokers.setdefault(inst.broker.name, inst.broker) is not inst.broker:
                raise ValueError(f"two different brokers are named {inst.broker.name!r}")
        for broker in self._brokers.values():
            broker.attach(self)
        self._queue: list[tuple[int, int, Event]] = []
        self._seq = itertools.count()
        self._order_seq = itertools.count(1)
        self._next_clock: int | None = None
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
        match ev:
            case BrokerEvent():
                self._brokers[ev.broker].handle(ev)
            case Fill():
                self._on_fill(ev)
            case OrderUpdate():
                self._on_update(ev)
            case MarketOpen():
                m = ev.market
                self.markets[m.id] = self.open_markets[m.id] = m
                self._to_brokers(ev)
                self._each(m.id, lambda s, c: s.on_market_open(c, m))
            case Book():
                self.books[ev.market_id] = ev
                self._to_brokers(ev)
                self._each(ev.market_id, lambda s, c: s.on_book(c, ev))
            case MarketTrade():
                self._to_brokers(ev)
                self._each(ev.market_id, lambda s, c: s.on_trade(c, ev))
            case DataEvent():
                self.latest[ev.key] = ev
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
        self, inst: Instance, market_id: str, side: Side, action: Action, price: float, qty: int,
        tif: Tif, post_only: bool, tag: str,
    ) -> Order:  # fmt: skip
        order = Order(
            id=f"{inst.id}-{next(self._order_seq)}", instance=inst.id, market_id=market_id,
            side=side, action=action, price=round(price, 9), qty=qty, tif=tif, post_only=post_only,
            tag=tag, created_ts=self.now,
        )  # fmt: skip
        self.orders[order.id] = order
        reason = self._invalid(order) or self._risk(inst, order)
        if reason:
            order.status, order.reason = OrderStatus.REJECTED, reason
            self.ledger.order(order)
            return order
        self.live[order.id] = order
        self.ledger.order(order)
        inst.broker.submit(order)
        return order

    def cancel(self, order_id: str) -> None:
        order = self.live.get(order_id)
        if order is not None:
            self.instances[order.instance].broker.cancel(order)

    def halt(self, instance_id: str | None = None, reason: str = "") -> None:
        """Kill switch: no new buys and resting buys cancelled, for one instance or all.

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
            self.ledger.log(self.now, iid, "resumed")

    def _invalid(self, o: Order) -> str | None:
        m = self.open_markets.get(o.market_id)
        if m is None:
            return f"market {o.market_id} is not open"
        if not isinstance(o.qty, int) or o.qty <= 0:
            return "qty must be a positive whole number"
        if not 0 < o.price < 1:
            return "price must be between 0 and 1"
        steps = o.price / m.tick
        if abs(steps - round(steps)) > 1e-6:
            return f"price {o.price} is not on the {m.tick} tick"
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

    def _close(self, ev: MarketClose) -> None:
        m = self.open_markets.pop(ev.market_id, None)
        self._to_brokers(ev)
        if m is not None:
            self._each(m.id, lambda s, c: s.on_market_close(c, m))

    def _settle(self, ev: Settled) -> None:
        if ev.market_id in self.open_markets:
            self._close(MarketClose(ts=ev.ts, market_id=ev.market_id))
        for s in self.portfolio.settle(ev.market_id, ev.outcome, ev.ts):
            self.ledger.settlement(s)
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
