"""Simulated venue for backtest and paper trading."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from quanty.core.broker import Broker
from quanty.core.events import (
    Book,
    BrokerEvent,
    DataEvent,
    Event,
    Fill,
    MarketClose,
    MarketOpen,
    MarketTrade,
    OrderUpdate,
    Settled,
)
from quanty.core.fees import FeeModel, NoFee
from quanty.core.orders import Order
from quanty.core.types import EPS, Action, Liquidity, OrderStatus, Tif, from_yes


@dataclass(frozen=True, kw_only=True)
class _Arrive(BrokerEvent):
    order_id: str


@dataclass(frozen=True, kw_only=True)
class _CancelArrive(BrokerEvent):
    order_id: str


@dataclass(frozen=True)
class QueueModel:
    """How resting orders queue and fill. The defaults are the realistic model; the switches let a
    backtest reproduce a more conservative (or a different venue's) simulator exactly."""

    measure_at: Literal["arrival", "decision"] = "arrival"  # when the queue ahead is read from the book
    credit_cancels: bool = True  # the queue ahead shrinks when the level shrinks (cancels ahead of us)
    fill_on_cross: bool = True  # the other side of the book moving onto the order fills it
    through_fill: Literal["trade_size", "full"] = "trade_size"  # a trade through our price fills this much
    cancel_stops_fills: bool = False  # no fills once a cancel is sent (optimistic), vs until it arrives
    # with credit_cancels: trades at our price within this long after the level shrank are what the
    # shrink showed (a venue often sends the book before its trades), not more of the queue gone.
    # 0: every shrink is cancels and every trade more, which counts those contracts twice
    match_trades_ms: int = 500


@dataclass
class _Level:
    price: float  # YES
    size: float


@dataclass
class _Working:
    order: Order
    remaining: float
    buys_yes: bool
    yes_price: float
    queue_ahead: float = 0.0
    credited: float = 0.0  # how far the queue ahead last moved up on a level shrink...
    credited_ts: int = 0  # ...and when: trades at our price soon after are that, not more

    def marketable(self, yes_px: float) -> bool:
        """Would a resting order on the other side at yes_px match us?"""
        return yes_px <= self.yes_price + EPS if self.buys_yes else yes_px >= self.yes_price - EPS


class SimBroker(Broker):
    """A simple, deterministic fill model.

    Orders and cancels reach the venue `latency_ms` after they are sent. On arrival an order
    takes what it can from the book at the book's prices (taker), then rests at its limit
    (maker) unless it is IOC; a post-only order that would take is rejected instead. A resting
    order joins the back of the queue at its price. It fills when trades at its price use up the
    queue ahead of it, when a trade prints through its price, or when the other side of the book
    moves onto it. Liquidity taken is removed from this broker's copy of the book until the next
    book update. Our simulated orders never appear in the book, so several of them can see the
    same trade; subclass and override to model this more carefully.

    arrival="next_event" is a more conservative timing: an order that has arrived is matched on
    the next event the broker sees after it (a book update, a trade or a data tick), against the
    book after that update, like a simulator that only checks pending orders when its next event arrives.
    """

    def __init__(
        self,
        name: str = "sim",
        *,
        latency_ms: int = 0,
        fees: Mapping[str, FeeModel] | None = None,
        arrival: Literal["at_latency", "next_event"] = "at_latency",
        queue: QueueModel | None = None,
    ) -> None:
        """fees maps venue name -> FeeModel, for markets that don't carry their own; others trade free."""
        super().__init__(name)
        self.latency_ms = latency_ms
        self.arrival = arrival
        self.queue = queue or QueueModel()
        self._queue_at_decision: dict[str, float] = {}
        self._canceling: set[str] = set()
        self._arrived: list[Order] = []  # next_event mode: arrived, waiting for the next event
        self.fees = dict(fees or {})
        self._fee: dict[str, FeeModel] = {}
        # each market's latest book as it came (cheap to keep), and the levels built from it on first
        # use, which fills then consume until the next book replaces them: a broker sees every book,
        # but only markets it trades need levels
        self._latest: dict[str, Book] = {}
        self._books: dict[str, tuple[list[_Level], list[_Level]]] = {}
        self._pending: dict[str, Order] = {}
        self._working: dict[str, _Working] = {}
        self._closed: set[str] = set()

    # --- engine interface ---

    def submit(self, order: Order) -> None:
        self._pending[order.id] = order
        if self.queue.measure_at == "decision":
            buys_yes, yes_px = order.in_yes()
            bids, asks = self._levels(order.market_id)
            same = bids if buys_yes else asks
            self._queue_at_decision[order.id] = sum(lv.size for lv in same if abs(lv.price - yes_px) < EPS)
        self.emit(_Arrive(ts=self.engine.now + self.latency_ms, broker=self.name, order_id=order.id))

    def cancel(self, order: Order) -> None:
        if self.queue.cancel_stops_fills:
            self._canceling.add(order.id)
        self.emit(_CancelArrive(ts=self.engine.now + self.latency_ms, broker=self.name, order_id=order.id))

    def handle(self, event: BrokerEvent) -> None:
        match event:
            case _Arrive():
                order = self._pending.pop(event.order_id)
                if self.arrival == "next_event":
                    self._arrived.append(order)
                else:
                    self._arrive(order)
            case _CancelArrive():
                w = self._working.pop(event.order_id, None)
                if w is not None:  # else already filled, expired or never accepted
                    self._update(w, OrderStatus.CANCELED)
            case _:
                raise TypeError(f"unexpected {event!r}")

    def on_event(self, event: Event) -> None:
        match event:
            case MarketOpen():
                m = event.market
                self._fee[m.id] = m.fee or self.fees.get(m.venue, NoFee())
            case Book():
                self._latest[event.market_id] = event
                self._books.pop(event.market_id, None)  # rebuilt from it when next needed
                self._match_arrived(event.market_id)
                if self._working:
                    self._book_moved(event.market_id)
            case MarketTrade():
                self._match_arrived(event.market_id)
                self._trade(event)
            case DataEvent():
                self._match_arrived(None)
            case MarketClose() | Settled():
                self._match_arrived(event.market_id)
                self._expire(event.market_id)

    def _match_arrived(self, market_id: str | None) -> None:
        if not self._arrived:
            return
        due = [o for o in self._arrived if market_id is None or o.market_id == market_id]
        self._arrived = [o for o in self._arrived if o not in due]
        for order in due:
            self._arrive(order)

    # --- matching ---

    def _arrive(self, order: Order) -> None:
        if order.market_id in self._closed:
            self._emit_update(order, OrderStatus.REJECTED, 0, "market closed")
            return
        buys_yes, yes_px = order.in_yes()
        w = _Working(order, order.qty, buys_yes, yes_px)
        bids, asks = self._levels(order.market_id)
        opposite, same = (asks, bids) if buys_yes else (bids, asks)
        if order.post_only and opposite and w.marketable(opposite[0].price):
            self._emit_update(order, OrderStatus.REJECTED, 0, "post-only order would take")
            return
        self._take(w, opposite, Liquidity.TAKER, at_level_price=True)
        if w.remaining <= EPS:
            self._update(w, OrderStatus.FILLED)
        elif order.tif is Tif.IOC:
            self._update(w, OrderStatus.CANCELED)
        else:
            here = sum(lv.size for lv in same if abs(lv.price - yes_px) < EPS)
            w.queue_ahead = self._queue_at_decision.pop(order.id, here)
            self._working[order.id] = w
            self._update(w, OrderStatus.OPEN)

    def _take(self, w: _Working, levels: list[_Level], liq: Liquidity, at_level_price: bool) -> None:
        for lv in levels:
            if w.remaining <= EPS or not w.marketable(lv.price):
                break
            qty = min(w.remaining, lv.size)
            lv.size = round(lv.size - qty, 9)
            self._fill(w, lv.price if at_level_price else w.yes_price, qty, liq)
        levels[:] = [lv for lv in levels if lv.size > EPS]

    def _book_moved(self, market_id: str) -> None:
        resting = self._resting(market_id)
        if not resting:
            return
        bids, asks = self._levels(market_id)
        for w in resting:
            opposite, same = (asks, bids) if w.buys_yes else (bids, asks)
            if self.queue.credit_cancels:
                here = sum(lv.size for lv in same if abs(lv.price - w.yes_price) < EPS)
                if here < w.queue_ahead - EPS:  # cancels ahead of us (or trades yet to print) move us up
                    if self.engine.now - w.credited_ts > self.queue.match_trades_ms:
                        w.credited = 0.0
                    w.credited += w.queue_ahead - here
                    w.credited_ts = self.engine.now
                    w.queue_ahead = here
            if self.queue.fill_on_cross and w.order.id not in self._canceling:
                self._take(w, opposite, Liquidity.MAKER, at_level_price=False)
                self._finish_if_filled(w)

    def _trade(self, t: MarketTrade) -> None:
        if not self._working:
            return
        left = t.qty
        for w in self._resting(t.market_id):
            if left <= EPS:
                break
            if w.order.id in self._canceling:
                continue
            # a trade only reaches our side when its taker trades against it (unknown taker: assume so)
            hits_us = t.taker is None or (t.taker is Action.SELL) == w.buys_yes
            through = t.price < w.yes_price - EPS if w.buys_yes else t.price > w.yes_price + EPS
            if through and hits_us:
                qty = w.remaining if self.queue.through_fill == "full" else min(w.remaining, left)
            elif abs(t.price - w.yes_price) < EPS and hits_us:
                seen = 0.0  # this trade's part the level already lost, and the queue with it
                if w.credited > EPS and self.engine.now - w.credited_ts <= self.queue.match_trades_ms:
                    seen = min(w.credited, left)
                    w.credited -= seen
                ahead = min(w.queue_ahead, left - seen)
                w.queue_ahead -= ahead
                qty = min(w.remaining, left - seen - ahead)
            else:
                continue
            if qty > EPS:
                left -= qty
                self._fill(w, w.yes_price, qty, Liquidity.MAKER)
                self._finish_if_filled(w)

    def _expire(self, market_id: str) -> None:
        self._closed.add(market_id)
        for w in self._resting(market_id):
            del self._working[w.order.id]
            self._update(w, OrderStatus.EXPIRED)

    # --- helpers ---

    def _levels(self, market_id: str) -> tuple[list[_Level], list[_Level]]:
        """The market's book as levels fills can consume, built from its latest book on first use."""
        got = self._books.get(market_id)
        if got is None:
            b = self._latest.get(market_id)
            got = ([_Level(p, s) for p, s in b.bids], [_Level(p, s) for p, s in b.asks]) if b else ([], [])
            self._books[market_id] = got
        return got

    def _resting(self, market_id: str) -> list[_Working]:
        """Resting orders in a market, most aggressive price first, then oldest."""
        ws = [w for w in self._working.values() if w.order.market_id == market_id]
        return sorted(ws, key=lambda w: (-w.yes_price if w.buys_yes else w.yes_price, w.order.created_ts))

    def _fill(self, w: _Working, yes_px: float, qty: float, liq: Liquidity) -> None:
        o = w.order
        price = from_yes(o.side, yes_px)
        fee = self._fee.get(o.market_id, NoFee()).fee(price, qty, liq)
        w.remaining = round(w.remaining - qty, 9)
        self.emit(
            Fill(ts=self.engine.now, order_id=o.id, market_id=o.market_id, side=o.side, action=o.action,
                 price=price, qty=qty, fee=fee, liquidity=liq)
        )  # fmt: skip

    def _finish_if_filled(self, w: _Working) -> None:
        if w.remaining <= EPS and self._working.pop(w.order.id, None) is not None:
            self._update(w, OrderStatus.FILLED)

    def _update(self, w: _Working, status: OrderStatus) -> None:
        if status.done:
            self._canceling.discard(w.order.id)
        self._emit_update(w.order, status, w.order.qty - w.remaining)

    def _emit_update(self, order: Order, status: OrderStatus, filled: float, reason: str = "") -> None:
        self.emit(
            OrderUpdate(ts=self.engine.now, order_id=order.id, status=status, filled=filled, reason=reason)
        )
