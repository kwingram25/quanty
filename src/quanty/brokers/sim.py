"""Simulated venue for backtest and paper trading."""

from collections.abc import Mapping
from dataclasses import dataclass

from quanty.core.broker import Broker
from quanty.core.events import (
    Book,
    BrokerEvent,
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


@dataclass
class _Level:
    price: float  # YES
    size: int


@dataclass
class _Working:
    order: Order
    remaining: int
    buys_yes: bool
    yes_price: float
    queue_ahead: int = 0

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
    """

    def __init__(
        self, name: str = "sim", *, latency_ms: int = 0, fees: Mapping[str, FeeModel] | None = None
    ) -> None:
        """fees maps venue name -> FeeModel; venues not listed trade free."""
        super().__init__(name)
        self.latency_ms = latency_ms
        self.fees = dict(fees or {})
        self._venue: dict[str, str] = {}
        self._books: dict[str, tuple[list[_Level], list[_Level]]] = {}
        self._pending: dict[str, Order] = {}
        self._working: dict[str, _Working] = {}
        self._closed: set[str] = set()

    # --- engine interface ---

    def submit(self, order: Order) -> None:
        self._pending[order.id] = order
        self.emit(_Arrive(ts=self.engine.now + self.latency_ms, broker=self.name, order_id=order.id))

    def cancel(self, order: Order) -> None:
        self.emit(_CancelArrive(ts=self.engine.now + self.latency_ms, broker=self.name, order_id=order.id))

    def handle(self, event: BrokerEvent) -> None:
        match event:
            case _Arrive():
                self._arrive(self._pending.pop(event.order_id))
            case _CancelArrive():
                w = self._working.pop(event.order_id, None)
                if w is not None:  # else already filled, expired or never accepted
                    self._update(w, OrderStatus.CANCELED)
            case _:
                raise TypeError(f"unexpected {event!r}")

    def on_event(self, event: Event) -> None:
        match event:
            case MarketOpen():
                self._venue[event.market.id] = event.market.venue
            case Book():
                self._books[event.market_id] = (
                    [_Level(p, s) for p, s in event.bids],
                    [_Level(p, s) for p, s in event.asks],
                )
                self._book_moved(event.market_id)
            case MarketTrade():
                self._trade(event)
            case MarketClose() | Settled():
                self._expire(event.market_id)

    # --- matching ---

    def _arrive(self, order: Order) -> None:
        if order.market_id in self._closed:
            self._emit_update(order, OrderStatus.REJECTED, 0, "market closed")
            return
        buys_yes, yes_px = order.in_yes()
        w = _Working(order, order.qty, buys_yes, yes_px)
        bids, asks = self._books.get(order.market_id, ([], []))
        opposite, same = (asks, bids) if buys_yes else (bids, asks)
        if order.post_only and opposite and w.marketable(opposite[0].price):
            self._emit_update(order, OrderStatus.REJECTED, 0, "post-only order would take")
            return
        self._take(w, opposite, Liquidity.TAKER, at_level_price=True)
        if w.remaining == 0:
            self._update(w, OrderStatus.FILLED)
        elif order.tif is Tif.IOC:
            self._update(w, OrderStatus.CANCELED)
        else:
            w.queue_ahead = sum(lv.size for lv in same if abs(lv.price - yes_px) < EPS)
            self._working[order.id] = w
            self._update(w, OrderStatus.OPEN)

    def _take(self, w: _Working, levels: list[_Level], liq: Liquidity, at_level_price: bool) -> None:
        for lv in levels:
            if w.remaining == 0 or not w.marketable(lv.price):
                break
            qty = min(w.remaining, lv.size)
            lv.size -= qty
            self._fill(w, lv.price if at_level_price else w.yes_price, qty, liq)
        levels[:] = [lv for lv in levels if lv.size > 0]

    def _book_moved(self, market_id: str) -> None:
        bids, asks = self._books[market_id]
        for w in self._resting(market_id):
            opposite, same = (asks, bids) if w.buys_yes else (bids, asks)
            here = sum(lv.size for lv in same if abs(lv.price - w.yes_price) < EPS)
            w.queue_ahead = min(w.queue_ahead, here)  # cancels ahead of us move us up
            self._take(w, opposite, Liquidity.MAKER, at_level_price=False)
            self._finish_if_filled(w)

    def _trade(self, t: MarketTrade) -> None:
        left = t.qty
        for w in self._resting(t.market_id):
            if left <= 0:
                break
            through = t.price < w.yes_price - EPS if w.buys_yes else t.price > w.yes_price + EPS
            hits_us = t.taker is None or (t.taker is Action.SELL) == w.buys_yes
            if through:
                qty = min(w.remaining, left)
            elif abs(t.price - w.yes_price) < EPS and hits_us:
                ahead = min(w.queue_ahead, left)
                w.queue_ahead -= ahead
                qty = min(w.remaining, left - ahead)
            else:
                continue
            if qty > 0:
                left -= qty
                self._fill(w, w.yes_price, qty, Liquidity.MAKER)
                self._finish_if_filled(w)

    def _expire(self, market_id: str) -> None:
        self._closed.add(market_id)
        for w in self._resting(market_id):
            del self._working[w.order.id]
            self._update(w, OrderStatus.EXPIRED)

    # --- helpers ---

    def _resting(self, market_id: str) -> list[_Working]:
        """Resting orders in a market, most aggressive price first, then oldest."""
        ws = [w for w in self._working.values() if w.order.market_id == market_id]
        return sorted(ws, key=lambda w: (-w.yes_price if w.buys_yes else w.yes_price, w.order.created_ts))

    def _fill(self, w: _Working, yes_px: float, qty: int, liq: Liquidity) -> None:
        o = w.order
        price = from_yes(o.side, yes_px)
        fee = self.fees.get(self._venue.get(o.market_id, ""), NoFee()).fee(price, qty, liq)
        w.remaining -= qty
        self.emit(
            Fill(ts=self.engine.now, order_id=o.id, market_id=o.market_id, side=o.side, action=o.action,
                 price=price, qty=qty, fee=fee, liquidity=liq)
        )  # fmt: skip

    def _finish_if_filled(self, w: _Working) -> None:
        if w.remaining == 0 and self._working.pop(w.order.id, None) is not None:
            self._update(w, OrderStatus.FILLED)

    def _update(self, w: _Working, status: OrderStatus) -> None:
        self._emit_update(w.order, status, w.order.qty - w.remaining)

    def _emit_update(self, order: Order, status: OrderStatus, filled: int, reason: str = "") -> None:
        self.emit(
            OrderUpdate(ts=self.engine.now, order_id=order.id, status=status, filled=filled, reason=reason)
        )
