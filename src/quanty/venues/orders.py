"""Venue order requests: the exact request a quanty order becomes, and a way to check it without trading.

Shadow mode previews every order (Polymarket US validates it server-side without placing it; Kalshi
has no preview endpoint, so the request is built and checked locally). The live broker sends the
same requests, and reads and cancels orders; every venue's answer is normalized to a VenueOrder.
"""

import json
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol

import httpx

from quanty.core.orders import Order
from quanty.core.types import Action, Market, Side, Tif
from quanty.venues.auth import KalshiSigner, PolymarketSigner

EXPIRE_BEFORE_CLOSE_S = 2  # resting orders end this long before their market closes
CLIENT_PREFIX = "quanty-"  # on client order ids, where the venue takes one (Kalshi)


class Signer(Protocol):
    def headers(self, method: str, path: str) -> dict[str, str]: ...


@dataclass(frozen=True)
class VenueRequest:
    method: str
    path: str
    body: dict[str, Any]


@dataclass(frozen=True)
class Preview:
    ok: bool
    request: VenueRequest
    status: int | None = None  # HTTP status, None when checked locally
    rtt_ms: float | None = None
    detail: str = ""
    response: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VenueOrder:
    """A venue's view of one order, in quanty terms."""

    id: str
    filled: float  # cumulative contracts
    fees: float  # cumulative $, negative = rebate
    done: bool
    state: str = ""  # the venue's own state name
    fill_yes_price: float | None = None  # average YES price of `filled`, when the venue reports it


class VenueError(Exception):
    """The venue refused a request (an HTTP error status); the order's outcome is known."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status, self.detail = status, detail


def native_id(market: Market | str) -> str:
    return (market if isinstance(market, str) else market.id).split(":", 1)[1]


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except json.JSONDecodeError:
        return {"text": resp.text[:300]}
    return data if isinstance(data, dict) else {"data": data}


def _num(v: Any) -> float:
    if isinstance(v, dict):
        v = v.get("value")
    return float(v or 0)


class OrderAPI(ABC):
    venue: str
    base_url: str

    @property
    @abstractmethod
    def signer(self) -> Signer: ...

    @abstractmethod
    def place_request(self, order: Order, market: Market) -> VenueRequest: ...

    @abstractmethod
    async def preview(self, client: httpx.AsyncClient, order: Order, market: Market) -> Preview: ...

    @abstractmethod
    async def place(self, client: httpx.AsyncClient, req: VenueRequest) -> VenueOrder:
        """Send the order. VenueError when refused; an httpx error means the outcome is unknown."""

    @abstractmethod
    async def get(self, client: httpx.AsyncClient, venue_id: str, market_id: str) -> VenueOrder: ...

    @abstractmethod
    async def cancel(self, client: httpx.AsyncClient, venue_id: str, market_id: str) -> None: ...

    async def resting_by_client_id(self, client: httpx.AsyncClient) -> dict[str, tuple[str, str]]:
        """client order id -> (venue order id, market id) of this account's resting orders, where the
        venue keeps client ids (Kalshi); {} where it doesn't (Polymarket US)."""
        return {}

    async def _send(self, client: httpx.AsyncClient, method: str, path: str, **kw: Any) -> dict[str, Any]:
        headers = self.signer.headers(method, path)
        if "json" in kw:
            headers["Content-Type"] = "application/json"
        resp = await client.request(method, f"{self.base_url}{path}", headers=headers, **kw)
        data = _json(resp)
        if resp.status_code >= 300:
            raise VenueError(resp.status_code, json.dumps(data)[:300])
        return data


class PolymarketOrders(OrderAPI):
    """Polymarket US: prices in YES terms (buying NO at 99¢ is BUY_SHORT at YES 0.01)."""

    venue = "polymarket_us"
    base_url = "https://api.polymarket.us"
    INTENTS: ClassVar[dict[tuple[Side, Action], str]] = {
        (Side.YES, Action.BUY): "ORDER_INTENT_BUY_LONG",
        (Side.NO, Action.BUY): "ORDER_INTENT_BUY_SHORT",
        (Side.YES, Action.SELL): "ORDER_INTENT_SELL_LONG",
        (Side.NO, Action.SELL): "ORDER_INTENT_SELL_SHORT",
    }

    def __init__(self, signer: Signer | None = None) -> None:
        self._signer = signer

    @property
    def signer(self) -> Signer:
        if self._signer is None:
            self._signer = PolymarketSigner.from_env()
        return self._signer

    def place_request(self, order: Order, market: Market) -> VenueRequest:
        yes_price = order.price if order.side is Side.YES else 1 - order.price
        qty: float | int = int(order.qty) if float(order.qty).is_integer() else order.qty
        body: dict[str, Any] = {
            "marketSlug": native_id(market),
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{yes_price:.2f}", "currency": "USD"},
            "quantity": qty,
            "intent": self.INTENTS[(order.side, order.action)],
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        }
        if order.tif is Tif.IOC:
            body["tif"] = "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
            body["synchronousExecution"] = True  # the response carries the fills
        else:
            expire = datetime.fromtimestamp(market.close_ts / 1000 - EXPIRE_BEFORE_CLOSE_S, UTC)
            body["tif"] = "TIME_IN_FORCE_GOOD_TILL_DATE"
            body["goodTillTime"] = expire.strftime("%Y-%m-%dT%H:%M:%SZ")
        if order.post_only:
            body["participateDontInitiate"] = True
        return VenueRequest("POST", "/v1/orders", body)

    async def preview(self, client: httpx.AsyncClient, order: Order, market: Market) -> Preview:
        req = self.place_request(order, market)
        path = "/v1/order/preview"
        headers = {**self.signer.headers("POST", path), "Content-Type": "application/json"}
        t0 = time.perf_counter()
        resp = await client.post(f"{self.base_url}{path}", json={"request": req.body}, headers=headers)
        rtt = (time.perf_counter() - t0) * 1000
        try:
            data = resp.json()
        except json.JSONDecodeError:
            data = {"text": resp.text[:300]}
        detail = "" if resp.status_code == 200 else json.dumps(data)[:300]
        return Preview(
            resp.status_code == 200,
            req,
            resp.status_code,
            rtt,
            detail,
            data if isinstance(data, dict) else {},
        )

    DONE_STATES: ClassVar[set[str]] = {"ORDER_STATE_FILLED", "ORDER_STATE_CANCELED", "ORDER_STATE_REJECTED",
                                       "ORDER_STATE_EXPIRED", "ORDER_STATE_REPLACED"}  # fmt: skip

    FILL_TYPES: ClassVar[set[str]] = {"EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL"}

    async def place(self, client: httpx.AsyncClient, req: VenueRequest) -> VenueOrder:
        res = await self._send(client, req.method, req.path, json=req.body)
        if not res.get("id"):
            raise VenueError(200, f"no order id in reply: {json.dumps(res)[:300]}")
        # executions report every event (new, fills, cancel, reject), each with the order as it then stood
        executions = res.get("executions") or []
        for e in executions:
            if e.get("type") == "EXECUTION_TYPE_REJECTED":
                reason = f"{e.get('orderRejectReason', 'rejected')}: {e.get('text', '')}"
                raise VenueError(200, reason.rstrip(": "))
        ex = [e for e in executions if e.get("type") in self.FILL_TYPES and _num(e.get("lastShares")) > 0]
        filled = sum(_num(e["lastShares"]) for e in ex)
        price = sum(_num(e["lastShares"]) * _num(e.get("lastPx")) for e in ex) / filled if filled else None
        fees = sum(_num(e.get("commissionNotionalCollected")) for e in ex)
        state = str((executions[-1].get("order") or {}).get("state", "")) if executions else ""
        ioc = req.body.get("tif") == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"  # synchronous: over when it returns
        done = ioc or state in self.DONE_STATES or filled >= float(req.body["quantity"]) - 1e-9
        return VenueOrder(str(res["id"]), filled, fees, done, state, price)

    async def get(self, client: httpx.AsyncClient, venue_id: str, market_id: str) -> VenueOrder:
        od = (await self._send(client, "GET", f"/v1/order/{venue_id}")).get("order") or {}
        if not od:
            raise VenueError(200, "no order in reply")  # seen live: no fill information, try again
        state = str(od.get("state", ""))
        left = od.get("leavesQuantity")
        nothing_left = left is not None and _num(left) <= 1e-9
        price = _num(od["avgPx"]) if od.get("avgPx") else None
        done = state in self.DONE_STATES or nothing_left
        return VenueOrder(venue_id, _num(od.get("cumQuantity")), 0.0, done, state, price)

    async def cancel(self, client: httpx.AsyncClient, venue_id: str, market_id: str) -> None:
        await self._send(
            client, "POST", f"/v1/order/{venue_id}/cancel", json={"marketSlug": native_id(market_id)}
        )


class KalshiOrders(OrderAPI):
    """Kalshi: orders in YES terms (bid/ask); counts in hundredths of a contract."""

    venue = "kalshi"
    base_url = "https://api.elections.kalshi.com"
    PATH = "/trade-api/v2/portfolio/events/orders"
    READ_PATH = "/trade-api/v2/portfolio/orders"
    DONE_STATES: ClassVar[set[str]] = {"canceled", "executed"}

    def __init__(self, signer: Signer | None = None) -> None:
        self._signer = signer

    @property
    def signer(self) -> Signer:
        if self._signer is None:
            self._signer = KalshiSigner.from_env()
        return self._signer

    def place_request(self, order: Order, market: Market) -> VenueRequest:
        buys_yes, yes_price = order.in_yes()
        body: dict[str, Any] = {
            "ticker": native_id(market),
            "side": "bid" if buys_yes else "ask",
            "price": f"{yes_price:.4f}",
            "count": f"{order.qty:.2f}",
            "client_order_id": f"{CLIENT_PREFIX}{uuid.uuid4()}",
            "cancel_order_on_pause": True,
            "exchange_index": -1,
        }
        if order.tif is Tif.IOC:
            body["time_in_force"] = "immediate_or_cancel"
            body["self_trade_prevention_type"] = "taker_at_cross"
        else:
            body["time_in_force"] = "good_till_canceled"
            body["expiration_time"] = int(market.close_ts / 1000 - EXPIRE_BEFORE_CLOSE_S)
            body["self_trade_prevention_type"] = "maker"
        if order.post_only:
            body["post_only"] = True
        return VenueRequest("POST", self.PATH, body)

    async def preview(self, client: httpx.AsyncClient, order: Order, market: Market) -> Preview:
        """Kalshi has no order preview: build and sign the request, check it locally, send nothing."""
        req = self.place_request(order, market)
        self.signer.headers(
            req.method, req.path
        )  # fails loudly here, not at the first live order, if keys are wrong
        problems = []
        if not market.on_tick(order.price):
            problems.append(f"price {order.price} off tick")
        if not market.valid_qty(order.qty):
            problems.append(f"qty {order.qty} not a multiple of {market.qty_step:g}")
        return Preview(
            not problems, req, detail="; ".join(problems) or "checked locally (Kalshi has no preview)"
        )

    async def place(self, client: httpx.AsyncClient, req: VenueRequest) -> VenueOrder:
        res = await self._send(client, req.method, req.path, json=req.body)
        if not res.get("order_id"):
            raise VenueError(200, f"no order id in reply: {json.dumps(res)[:300]}")
        filled = _num(res.get("fill_count"))
        price = _num(res.get("average_fill_price")) if filled else None
        fees = filled * _num(res.get("average_fee_paid"))
        ioc = req.body.get("time_in_force") == "immediate_or_cancel"
        left = res.get("remaining_count")  # the reply has no status field
        done = ioc or (left is not None and _num(left) <= 1e-9) or filled >= float(req.body["count"]) - 1e-9
        return VenueOrder(str(res["order_id"]), filled, fees, done, "", price)

    async def get(self, client: httpx.AsyncClient, venue_id: str, market_id: str) -> VenueOrder:
        od = (await self._send(client, "GET", f"{self.READ_PATH}/{venue_id}")).get("order") or {}
        if not od:
            raise VenueError(200, "no order in reply")
        fees = _num(od.get("maker_fees_dollars")) + _num(od.get("taker_fees_dollars"))
        status = str(od.get("status", ""))
        return VenueOrder(venue_id, _num(od.get("fill_count_fp")), fees, status in self.DONE_STATES, status)

    async def cancel(self, client: httpx.AsyncClient, venue_id: str, market_id: str) -> None:
        await self._send(
            client, "DELETE", f"{self.PATH}/{venue_id}", params={"market_ticker": native_id(market_id)}
        )

    async def resting_by_client_id(self, client: httpx.AsyncClient) -> dict[str, tuple[str, str]]:
        out: dict[str, tuple[str, str]] = {}
        cursor = ""
        while True:
            params = {"status": "resting", "limit": 200, **({"cursor": cursor} if cursor else {})}
            res = await self._send(client, "GET", self.READ_PATH, params=params)
            for o in res.get("orders") or []:
                if o.get("client_order_id"):
                    out[o["client_order_id"]] = (str(o["order_id"]), f"{self.venue}:{o['ticker']}")
            cursor = res.get("cursor") or ""
            if not cursor:
                return out


def default_apis() -> dict[str, OrderAPI]:
    return {api.venue: api for api in (PolymarketOrders(), KalshiOrders())}
