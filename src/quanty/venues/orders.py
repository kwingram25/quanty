"""Venue order requests: the exact request a quanty order becomes, and a way to check it without trading.

Shadow mode previews every order (Polymarket US validates it server-side without placing it; Kalshi
has no preview endpoint, so the request is built and checked locally). The live broker sends the
same requests.
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


def native_id(market: Market) -> str:
    return market.id.split(":", 1)[1]


class OrderAPI(ABC):
    venue: str

    @abstractmethod
    def place_request(self, order: Order, market: Market) -> VenueRequest: ...

    @abstractmethod
    async def preview(self, client: httpx.AsyncClient, order: Order, market: Market) -> Preview: ...


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


class KalshiOrders(OrderAPI):
    """Kalshi: orders in YES terms (bid/ask); counts in hundredths of a contract."""

    venue = "kalshi"
    base_url = "https://api.elections.kalshi.com"
    PATH = "/trade-api/v2/portfolio/events/orders"

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
            "client_order_id": str(uuid.uuid4()),
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


def default_apis() -> dict[str, OrderAPI]:
    return {api.venue: api for api in (PolymarketOrders(), KalshiOrders())}
