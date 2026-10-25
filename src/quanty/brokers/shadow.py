"""Shadow broker: simulated fills, plus a real venue check of every order. Nothing is ever placed."""

import asyncio
from collections.abc import Mapping
from typing import Any

import httpx
import structlog

from quanty.brokers.sim import SimBroker
from quanty.core.events import Event, MarketOpen
from quanty.core.orders import Order
from quanty.core.types import Market
from quanty.venues.orders import OrderAPI, Preview, default_apis

log = structlog.get_logger("quanty.shadow")

MAX_IN_FLIGHT = 5  # well under Polymarket US's 20 requests/s per key


class ShadowBroker(SimBroker):
    kind = "shadow"
    """Fills exactly like SimBroker; each submitted order is also turned into the venue's real
    request and previewed (OrderAPI.preview), with the result and round trip in the ledger's logs
    ("shadow preview"). Previews need a running event loop (paper/live sessions); in a backtest
    there is none and orders are only simulated.
    """

    def __init__(
        self, name: str = "shadow", *, apis: Mapping[str, OrderAPI] | None = None, **sim: Any
    ) -> None:
        super().__init__(name, **sim)
        self.apis = dict(apis) if apis is not None else default_apis()
        self._markets: dict[str, Market] = {}
        self._client: httpx.AsyncClient | None = None
        self._limit = asyncio.Semaphore(MAX_IN_FLIGHT)
        self._tasks: set[asyncio.Task[None]] = set()
        self.previews: list[Preview] = []

    def on_event(self, event: Event) -> None:
        if isinstance(event, MarketOpen):
            self._markets[event.market.id] = event.market
        super().on_event(event)

    def submit(self, order: Order) -> None:
        super().submit(order)
        market = self._markets.get(order.market_id)
        api = self.apis.get(market.venue) if market else None
        if market is None or api is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # backtest: simulate only
        task = loop.create_task(self._preview(api, order, market))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _preview(self, api: OrderAPI, order: Order, market: Market) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10)
        async with self._limit:
            try:
                result = await api.preview(self._client, order, market)
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                fields = {"order_id": order.id, "ok": False, "error": repr(exc)}
                self.engine.ledger.log(
                    self.engine.now, order.instance, "shadow preview", fields, level="error"
                )
                return
        self.previews.append(result)
        rtt = None if result.rtt_ms is None else round(result.rtt_ms, 1)
        fields = {"order_id": order.id, "venue": api.venue, "ok": result.ok, "status": result.status,
                  "rtt_ms": rtt, "detail": result.detail, "request": result.request.body}  # fmt: skip
        self.engine.ledger.log(self.engine.now, order.instance, "shadow preview", fields,
                               level="info" if result.ok else "warning")  # fmt: skip
        if not result.ok:
            log.warning("shadow preview rejected", order=order.id, venue=api.venue, detail=result.detail)

    async def aclose(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
