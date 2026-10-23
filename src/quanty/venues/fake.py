"""A fake exchange for testing brokers without real orders.

FakeExchange speaks the order routes of Polymarket US and Kalshi (the subset quanty uses, with the
same response fields) and matches orders against books you set. Plug it into a client with
`httpx.AsyncClient(transport=fake.transport())`. Faults can be injected per request:

    fake.fail_next("reject")    # the next order request gets HTTP 400
    fake.fail_next("error")     # HTTP 500
    fake.fail_next("timeout")   # the order IS placed, then the reply times out (outcome unknown)
    fake.fail_next("lost_read") # the next order read replies without the order (seen on Polymarket)

Resting orders fill when you call `trade(market, yes_price, qty)` (a taker trading against them).
"""

import itertools
import json
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

Fault = Literal["reject", "error", "timeout", "lost_read"]


@dataclass
class FakeOrder:
    id: str
    venue: str
    market: str
    buys_yes: bool
    yes_price: float
    qty: float
    ioc: bool
    filled: float = 0.0
    fees: float = 0.0
    canceled: bool = False
    executions: list[tuple[float, float]] = field(default_factory=list)  # (qty, yes price)

    @property
    def left(self) -> float:
        return 0.0 if self.canceled else round(self.qty - self.filled, 9)


class FakeExchange:
    def __init__(self, *, taker_fee: float = 0.0) -> None:
        """taker_fee: $ per contract charged on fills that take liquidity."""
        self.taker_fee = taker_fee
        self.books: dict[str, tuple[list[list[float]], list[list[float]]]] = {}  # market -> (bids, asks), YES
        self.orders: dict[str, FakeOrder] = {}
        self.requests: list[httpx.Request] = []
        self._faults: list[Fault] = []
        self._ids = itertools.count(1)

    # --- test controls ---

    def set_book(self, market: str, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> None:
        """market is the venue's own id (Kalshi ticker or Polymarket slug); levels are (YES price, qty)."""
        self.books[market] = (
            sorted(([p, q] for p, q in bids), reverse=True),
            sorted([p, q] for p, q in asks),
        )

    def fail_next(self, fault: Fault) -> None:
        self._faults.append(fault)

    def trade(self, market: str, yes_price: float, qty: float, *, taker_buys: bool) -> None:
        """A taker trades `qty` at `yes_price` against resting orders on the other side, oldest first."""
        for o in [o for o in self.orders.values() if o.market == market and o.left > 0]:
            if qty <= 1e-9:
                break
            if o.buys_yes == taker_buys or (
                o.yes_price < yes_price if o.buys_yes else o.yes_price > yes_price
            ):
                continue
            q = min(qty, o.left)
            o.filled += q
            o.executions.append((q, o.yes_price))
            qty -= q

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # --- matching ---

    def _match(self, o: FakeOrder) -> None:
        bids, asks = self.books.setdefault(o.market, ([], []))
        levels = asks if o.buys_yes else bids
        for lv in levels:
            if o.left <= 1e-9 or (lv[0] > o.yes_price + 1e-9 if o.buys_yes else lv[0] < o.yes_price - 1e-9):
                break
            q = min(o.left, lv[1])
            lv[1] -= q
            o.filled += q
            o.fees += q * self.taker_fee
            o.executions.append((q, lv[0]))
        levels[:] = [lv for lv in levels if lv[1] > 1e-9]
        if o.ioc:
            o.canceled = True

    def _crosses(self, market: str, buys_yes: bool, yes_price: float) -> bool:
        bids, asks = self.books.get(market, ([], []))
        if buys_yes:
            return bool(asks) and asks[0][0] <= yes_price + 1e-9
        return bool(bids) and bids[0][0] >= yes_price - 1e-9

    def _place(
        self,
        venue: str,
        market: str,
        buys_yes: bool,
        yes_price: float,
        qty: float,
        ioc: bool,
        post_only: bool,
    ) -> FakeOrder | None:
        if post_only and self._crosses(market, buys_yes, yes_price):
            return None
        o = FakeOrder(f"{venue[:2]}-{next(self._ids)}", venue, market, buys_yes, yes_price, qty, ioc)
        self.orders[o.id] = o
        self._match(o)
        return o

    # --- HTTP ---

    async def _handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        fault = self._faults.pop(0) if self._faults else None
        if fault == "reject":
            return httpx.Response(400, json={"error": "rejected by fake exchange"})
        if fault == "error":
            return httpx.Response(500, json={"error": "internal error"})
        path = req.url.path
        body: dict[str, Any] = json.loads(req.content) if req.content else {}
        if path.startswith("/trade-api/v2/"):
            resp = self._kalshi(req.method, path.removeprefix("/trade-api/v2"), body, fault)
        else:
            resp = self._polymarket(req.method, path, body, fault)
        if fault == "timeout":
            raise httpx.ReadTimeout("fake exchange: reply timed out", request=req)
        return resp

    def _polymarket(
        self, method: str, path: str, body: dict[str, Any], fault: Fault | None
    ) -> httpx.Response:
        if method == "POST" and path == "/v1/orders":
            intent = body["intent"]
            buys_yes = intent in ("ORDER_INTENT_BUY_LONG", "ORDER_INTENT_SELL_SHORT")
            ioc = body["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
            o = self._place(
                "polymarket_us",
                body["marketSlug"],
                buys_yes,
                float(body["price"]["value"]),
                float(body["quantity"]),
                ioc,
                bool(body.get("participateDontInitiate")),
            )
            if o is None:  # the API reports rejections as an execution
                rejected = {
                    "type": "EXECUTION_TYPE_REJECTED",
                    "orderRejectReason": "ORD_REJECT_REASON_EXCHANGE_OPTION",
                    "text": "participateDontInitiate order would match",
                    "order": {"state": "ORDER_STATE_REJECTED"},
                }
                return httpx.Response(200, json={"id": f"po-rej-{next(self._ids)}", "executions": [rejected]})
            snap = {"id": o.id, "state": self._pm_state(o), "cumQuantity": f"{o.filled:g}"}
            ex: list[dict[str, Any]] = [
                {"type": "EXECUTION_TYPE_NEW", "order": {**snap, "state": "ORDER_STATE_NEW"}}
            ]
            ex += [
                {
                    "type": "EXECUTION_TYPE_FILL",
                    "lastShares": f"{q:g}",
                    "lastPx": {"value": f"{p:.2f}"},
                    "aggressor": True,
                    "commissionNotionalCollected": {"value": f"{q * self.taker_fee:.4f}"},
                    "order": snap,
                }
                for q, p in o.executions
            ]
            if o.canceled:  # an IOC's unfilled rest
                ex.append({"type": "EXECUTION_TYPE_CANCELED", "order": snap})
            return httpx.Response(200, json={"id": o.id, "executions": ex})
        if path.startswith("/v1/order/"):
            parts = path.split("/")
            o = self.orders.get(parts[3])
            if o is None:
                return httpx.Response(404, json={"error": "order not found"})
            if method == "POST" and parts[-1] == "cancel":
                o.canceled = True
                return httpx.Response(200, json={})
            if fault == "lost_read":
                return httpx.Response(200, json={})
            state = self._pm_state(o)
            return httpx.Response(
                200,
                json={
                    "order": {
                        "id": o.id,
                        "state": state,
                        "cumQuantity": f"{o.filled:g}",
                        "leavesQuantity": f"{o.left:g}",
                    }
                },
            )
        return httpx.Response(404, json={"error": f"no route {method} {path}"})

    def _kalshi(self, method: str, path: str, body: dict[str, Any], fault: Fault | None) -> httpx.Response:
        if method == "POST" and path == "/portfolio/events/orders":
            ioc = body["time_in_force"] == "immediate_or_cancel"
            o = self._place(
                "kalshi",
                body["ticker"],
                body["side"] == "bid",
                float(body["price"]),
                float(body["count"]),
                ioc,
                bool(body.get("post_only")),
            )
            if o is None:
                return httpx.Response(400, json={"error": {"code": "post_only_cross"}})
            avg = sum(q * p for q, p in o.executions) / o.filled if o.filled else 0.0
            return httpx.Response(
                201,
                json={
                    "order_id": o.id,
                    "fill_count": f"{o.filled:.2f}",
                    "average_fill_price": f"{avg:.4f}",
                    "average_fee_paid": f"{self.taker_fee:.4f}",
                    "remaining_count": f"{o.left:.2f}",
                },
            )
        if method == "DELETE" and path.startswith("/portfolio/events/orders/"):
            o = self.orders.get(path.rsplit("/", 1)[1])
            if o is None:
                return httpx.Response(404, json={"error": "not_found"})
            o.canceled = True
            return httpx.Response(200, json={"order": {"order_id": o.id, "status": "canceled"}})
        if method == "GET" and path.startswith("/portfolio/orders/"):
            o = self.orders.get(path.rsplit("/", 1)[1])
            if o is None:
                return httpx.Response(404, json={"error": "not_found"})
            if fault == "lost_read":
                return httpx.Response(200, json={})
            return httpx.Response(
                200,
                json={
                    "order": {
                        "order_id": o.id,
                        "status": self._kalshi_status(o),
                        "fill_count_fp": f"{o.filled:.2f}",
                        "taker_fees_dollars": f"{o.fees:.4f}",
                        "maker_fees_dollars": "0",
                    }
                },
            )
        return httpx.Response(404, json={"error": f"no route {method} {path}"})

    @staticmethod
    def _pm_state(o: FakeOrder) -> str:
        if o.filled >= o.qty - 1e-9:
            return "ORDER_STATE_FILLED"
        if o.canceled:
            return "ORDER_STATE_CANCELED"
        return "ORDER_STATE_PARTIALLY_FILLED" if o.filled else "ORDER_STATE_NEW"

    @staticmethod
    def _kalshi_status(o: FakeOrder) -> str:
        return "executed" if o.filled >= o.qty - 1e-9 else "canceled" if o.canceled else "resting"
