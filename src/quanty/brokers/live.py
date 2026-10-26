"""Live broker: real orders on the venues. Refuses everything unless armed.

Orders go out through each venue's OrderAPI (the same requests shadow mode previews). Fills come
from the venue's replies: an IOC's fills are in its reply; a resting order is read every poll_s
until the venue reports it done, and re-read after a cancel until it does (fills can race a
cancel). Safety:

- armed=False (the default) rejects every order; `quanty live --arm` arms the brokers.
- max_qty / max_notional reject any single order above them (fat-finger guard); without both,
  nothing is placed.
- error_limit consecutive venue rejections disable the broker until restart.
- A request whose reply never arrives (timeout, dropped connection) leaves the order's outcome
  unknown: it stays pending and the broker disables itself at once, since the position it would
  report can no longer be trusted. Check the venue before restarting.
- With a journal (quanty live keeps one per broker), every order is written down before it is
  sent and when it is done. On start, orders a previous run left resting are canceled: by venue
  order id, or on Kalshi by client order id when the reply never came. Only journaled orders are
  ever touched, so other programs trading the same account are left alone.
"""

import asyncio
import json
import time
from collections.abc import Coroutine, Mapping
from pathlib import Path
from typing import Any

import httpx
import structlog

from quanty.core.broker import Broker
from quanty.core.events import Event, Fill, MarketOpen, OrderUpdate
from quanty.core.orders import Order
from quanty.core.types import Liquidity, Market, OrderStatus, Tif, from_yes
from quanty.venues.orders import OrderAPI, VenueError, VenueOrder, default_apis

log = structlog.get_logger("quanty.live")

EPS = 1e-9
FRESH_404_S = 5  # Polymarket US can answer 404 to a read just after accepting the order
NEVER_DONE_AFTER_CLOSE_S = 30  # stop reading an order the venue still hasn't finished this long after close


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class LiveBroker(Broker):
    kind = "live"

    def __init__(
        self,
        name: str = "live",
        *,
        apis: Mapping[str, OrderAPI] | None = None,
        armed: bool = False,
        max_qty: float | None = None,
        max_notional: float | None = None,
        error_limit: int = 3,
        poll_s: float = 1.0,
        final_reads: int = 6,
        final_read_every_s: float = 0.25,
        transport: httpx.AsyncBaseTransport | None = None,
        journal: Path | None = None,
    ) -> None:
        super().__init__(name)
        self.apis = dict(apis) if apis is not None else default_apis()
        self.armed = armed
        self.max_qty, self.max_notional = max_qty, max_notional
        self.error_limit = error_limit
        self.poll_s, self.final_reads, self.final_read_every_s = poll_s, final_reads, final_read_every_s
        self.transport = transport
        self.journal = journal
        self._placed_at: dict[str, float] = {}
        self.disabled = ""  # why, once disabled
        self.errors = 0  # consecutive rejections
        self._client: httpx.AsyncClient | None = None
        self._markets: dict[str, Market] = {}
        self._venue_id: dict[str, str] = {}
        self._known: dict[
            str, tuple[float, float, float]
        ] = {}  # order id -> (filled, fees, YES cost of filled)
        self._open: dict[str, Order] = {}  # resting at the venue
        self._cancel_wanted: set[str] = set()  # cancel asked while the placement was in flight
        self._unknown: set[str] = set()  # outcome unknown
        self._tasks: set[asyncio.Task[None]] = set()
        self._poller: asyncio.Task[None] | None = None

    # --- engine interface ---

    def on_event(self, event: Event) -> None:
        if isinstance(event, MarketOpen):
            self._markets[event.market.id] = event.market

    def submit(self, order: Order) -> None:
        reason = self._refusal(order)
        if reason:
            self._emit_update(order, OrderStatus.REJECTED, 0.0, reason)
            return
        self._spawn(self._place(order))

    def cancel(self, order: Order) -> None:
        if order.id in self._unknown or (order.id in self._known and order.id not in self._open):
            return  # outcome unknown, or already done
        if order.id not in self._venue_id:
            self._cancel_wanted.add(order.id)  # placement still in flight: cancel when it lands
            return
        self._spawn(self._cancel(order))

    async def start(self) -> None:
        """Cancel what a previous run left resting (see the module docstring)."""
        if self.journal is None or not self.journal.exists():
            return
        left: dict[str, dict[str, Any]] = {}
        for line in self.journal.read_text().splitlines():
            rec = json.loads(line)
            left.setdefault(rec["order"], {}).update(rec)
        left = {k: r for k, r in left.items() if not r.get("done")}
        by_client: dict[str, dict[str, tuple[str, str]]] = {}
        for oid, rec in left.items():
            api = self.apis.get(rec["venue"])
            if api is None:
                continue
            venue_id = rec.get("venue_id")
            if venue_id is None and rec.get("client_id"):  # sent, reply never came
                if rec["venue"] not in by_client:
                    by_client[rec["venue"]] = await api.resting_by_client_id(self.client)
                venue_id = by_client[rec["venue"]].get(rec["client_id"], (None, ""))[0]
            if venue_id is None:
                self._startup_log(oid, rec, "leftover order of unknown outcome: check the venue", "error")
                continue
            vo = await self._settle_leftover(api, venue_id, rec["market"])
            level = "warning" if vo is not None and vo.filled > EPS else "info"
            filled = None if vo is None else vo.filled
            self._startup_log(oid, rec, "leftover order canceled", level, venue_id=venue_id, filled=filled,
                              done=vo is not None and vo.done)  # fmt: skip
        # keep only what is still unsettled
        still = [json.dumps(r) for oid, r in left.items() if not r.get("settled")]
        self.journal.write_text("".join(f"{line}\n" for line in still))

    async def _settle_leftover(self, api: OrderAPI, venue_id: str, market_id: str) -> VenueOrder | None:
        try:
            vo = await api.get(self.client, venue_id, market_id)
            if not vo.done:
                await api.cancel(self.client, venue_id, market_id)
                for _ in range(self.final_reads):
                    vo = await api.get(self.client, venue_id, market_id)
                    if vo.done:
                        break
                    await asyncio.sleep(self.final_read_every_s)
            return vo
        except (VenueError, httpx.HTTPError) as exc:
            log.warning("leftover order check failed", venue_id=venue_id, error=str(exc))
            return None

    def _startup_log(self, oid: str, rec: dict[str, Any], msg: str, level: str, **fields: Any) -> None:
        if fields.get("done"):
            rec["settled"] = True
        info = {"order_id": oid, "venue": rec["venue"], "market": rec["market"], **fields}
        self.engine.ledger.log(self.engine.now, f"broker:{self.name}", msg, info, level=level)
        getattr(log, level)(msg, broker=self.name, **info)

    def _write_journal(self, order: Order, **fields: Any) -> None:
        if self.journal is not None:
            with self.journal.open("a") as f:
                f.write(json.dumps({"order": order.id, **fields}) + "\n")

    async def aclose(self) -> None:
        if self._poller is not None:
            self._poller.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()

    @property
    def capped(self) -> bool:
        """Both per-order caps are set; without them nothing is placed."""
        return self.max_qty is not None and self.max_notional is not None

    # --- checks ---

    def _refusal(self, order: Order) -> str:
        if not self.armed:
            return "live broker not armed"
        if self.disabled:
            return f"live broker disabled: {self.disabled}"
        if not self.capped:
            return f"no caps: set max_qty and max_notional under [brokers.{self.name}]"
        market = self._markets.get(order.market_id)
        if market is None or market.venue not in self.apis:
            return f"no order API for {order.market_id}"
        if self.max_qty is not None and order.qty > self.max_qty + EPS:
            return f"qty {order.qty:g} over max_qty {self.max_qty:g}"
        if self.max_notional is not None and order.qty * order.price > self.max_notional + EPS:
            return f"notional ${order.qty * order.price:.2f} over max_notional ${self.max_notional:.2f}"
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return "the live broker needs a running session (quanty live)"
        return ""

    def _disable(self, reason: str) -> None:
        if not self.disabled:
            self.disabled = reason
            self.engine.ledger.log(
                self.engine.now,
                f"broker:{self.name}",
                "live broker disabled",
                {"reason": reason},
                level="error",
            )
            log.error("live broker disabled", broker=self.name, reason=reason)

    # --- venue calls ---

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10, transport=self.transport)
        return self._client

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _api(self, order: Order) -> tuple[OrderAPI, Market]:
        market = self._markets[order.market_id]
        return self.apis[market.venue], market

    async def _place(self, order: Order) -> None:
        api, market = self._api(order)
        req = api.place_request(order, market)
        self._write_journal(
            order, venue=api.venue, market=order.market_id, client_id=req.body.get("client_order_id")
        )
        t0 = time.perf_counter()
        try:
            vo = await api.place(self.client, req)
        except VenueError as exc:
            self.errors += 1
            self._log(order, "live order rejected", level="warning", status=exc.status, detail=exc.detail,
                      request=req.body)  # fmt: skip
            self._emit_update(order, OrderStatus.REJECTED, 0.0, exc.detail)
            self._write_journal(order, done=True)
            if self.errors >= self.error_limit:
                self._disable(f"{self.errors} consecutive rejections")
            return
        except httpx.HTTPError as exc:
            self._unknown.add(order.id)
            self._log(order, "live order outcome unknown", level="error", error=repr(exc), request=req.body)
            self._disable(f"order {order.id} outcome unknown ({type(exc).__name__}); check the venue")
            return
        self.errors = 0
        self._venue_id[order.id] = vo.id
        self._placed_at[order.id] = time.monotonic()
        self._write_journal(order, venue_id=vo.id)
        self._log(order, "live order", venue_id=vo.id, rtt_ms=round((time.perf_counter() - t0) * 1000, 1),
                  filled=vo.filled, request=req.body)  # fmt: skip
        self._apply(order, vo, taker=True)
        if order.id in self._cancel_wanted:
            self._cancel_wanted.discard(order.id)
            if not vo.done:
                await self._cancel(order)

    async def _cancel(self, order: Order) -> None:
        api, market = self._api(order)
        try:
            await api.cancel(self.client, self._venue_id[order.id], market.id)
        except (VenueError, httpx.HTTPError) as exc:  # often: it filled first; the reads below tell
            self._log(order, "live cancel failed", level="warning", error=str(exc))
        for _ in range(self.final_reads):
            if await self._refresh(order):
                return
            await asyncio.sleep(self.final_read_every_s)

    async def _refresh(self, order: Order) -> bool:
        """Read the order and apply any news. True once it is done."""
        api, market = self._api(order)
        try:
            vo = await api.get(self.client, self._venue_id[order.id], market.id)
        except (VenueError, httpx.HTTPError) as exc:  # no fill news: try again
            fresh = time.monotonic() - self._placed_at.get(order.id, 0.0) < FRESH_404_S
            if not (fresh and isinstance(exc, VenueError) and exc.status == 404):  # Polymarket lags a moment
                log.warning("live order read failed", order=order.id, error=str(exc))
            return False
        self._apply(order, vo, taker=False)
        return vo.done

    async def _poll(self) -> None:
        while self._open:
            await asyncio.sleep(self.poll_s)
            for order in list(self._open.values()):
                if await self._refresh(order):
                    continue
                if now_ms() > self._markets[order.market_id].close_ts + NEVER_DONE_AFTER_CLOSE_S * 1000:
                    self._log(order, "live order never reported done", level="error")
                    self._finish(order, OrderStatus.EXPIRED, "venue never reported it done")
        self._poller = None

    # --- results ---

    def _apply(self, order: Order, vo: VenueOrder, *, taker: bool) -> None:
        filled, fees, cost = self._known.get(order.id, (0.0, 0.0, 0.0))
        new = round(vo.filled - filled, 9)
        if new > EPS:
            if vo.fill_yes_price is not None:  # the venue's average over everything filled so far
                new_cost = vo.filled * vo.fill_yes_price - cost
            else:  # a resting order fills at its own price
                new_cost = new * order.in_yes()[1]
            yes_px = new_cost / new
            liq = Liquidity.TAKER if taker or order.tif is Tif.IOC else Liquidity.MAKER
            self.emit(Fill(ts=self._ts(), order_id=order.id, market_id=order.market_id, side=order.side,
                           action=order.action, price=round(from_yes(order.side, yes_px), 6), qty=new,
                           fee=vo.fees - fees, liquidity=liq))  # fmt: skip
            filled, fees, cost = vo.filled, vo.fees, cost + new_cost
        self._known[order.id] = (filled, fees, cost)
        if vo.done:
            if filled >= order.qty - EPS:
                self._finish(order, OrderStatus.FILLED)
            elif "EXPIRED" in vo.state.upper():
                self._finish(order, OrderStatus.EXPIRED)
            elif "REJECTED" in vo.state.upper():
                self._finish(order, OrderStatus.REJECTED, vo.state)
            else:
                self._finish(order, OrderStatus.CANCELED)
        elif order.id not in self._open:
            self._open[order.id] = order
            self._emit_update(order, OrderStatus.OPEN, filled)
            if self._poller is None:
                self._poller = asyncio.get_running_loop().create_task(self._poll())

    def _finish(self, order: Order, status: OrderStatus, reason: str = "") -> None:
        self._open.pop(order.id, None)
        self._write_journal(order, done=True)
        self._emit_update(order, status, self._known.get(order.id, (0.0, 0.0, 0.0))[0], reason)

    def _ts(self) -> int:
        return max(self.engine.now, now_ms())

    def _emit_update(self, order: Order, status: OrderStatus, filled: float, reason: str = "") -> None:
        self.emit(OrderUpdate(ts=self._ts(), order_id=order.id, status=status, filled=filled, reason=reason))

    def _log(self, order: Order, msg: str, level: str = "info", **fields: Any) -> None:
        self.engine.ledger.log(
            self.engine.now, order.instance, msg, {"order_id": order.id, **fields}, level=level
        )
