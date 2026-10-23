"""Hand-written venue messages in the shapes Kalshi and Polymarket US send (no recorded data)."""

import json
from typing import Any

T0 = 1_792_713_600_000  # 2026-10-23 00:00:00 UTC
MIN = 60_000


def iso(ms: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Kalshi ---

KALSHI_RANGES = [
    {"start": "0.0000", "end": "0.1000", "step": "0.0010"},
    {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
    {"start": "0.9000", "end": "1.0000", "step": "0.0010"},
]


def kalshi_market(ticker: str, open_ts: int, close_ts: int, **kw: Any) -> dict[str, Any]:
    return {
        "ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "open_time": iso(open_ts),
        "close_time": iso(close_ts), "status": "active", "result": "", "price_ranges": KALSHI_RANGES,
        "title": "BTC price up in next 15 mins?", **kw,
    }  # fmt: skip


def kalshi_rest(markets: list[dict[str, Any]], series: str = "KXBTC15M", cursor: str = "") -> str:
    body = {"markets": markets, "cursor": cursor}
    return json.dumps({"rest": "markets", "params": {"series": series}, "body": body})


def k_snapshot(
    ticker: str, yes: list[tuple[str, str]], no: list[tuple[str, str]], sid: int = 1, seq: int = 1
) -> str:
    msg = {
        "market_ticker": ticker,
        "yes_dollars_fp": [list(x) for x in yes],
        "no_dollars_fp": [list(x) for x in no],
    }
    return json.dumps({"type": "orderbook_snapshot", "sid": sid, "seq": seq, "msg": msg})


def k_delta(ticker: str, side: str, price: str, delta: str, seq: int, sid: int = 1, ts: int = T0) -> str:
    msg = {"market_ticker": ticker, "price_dollars": price, "delta_fp": delta, "side": side, "ts_ms": ts}
    return json.dumps({"type": "orderbook_delta", "sid": sid, "seq": seq, "msg": msg})


def k_trade(ticker: str, tid: str, yes_price: str, count: str, taker: str, ts: int = T0) -> str:
    msg = {"trade_id": tid, "market_ticker": ticker, "yes_price_dollars": yes_price, "count_fp": count,
           "taker_side": taker, "ts_ms": ts}  # fmt: skip
    return json.dumps({"type": "trade", "sid": 2, "seq": 1, "msg": msg})


def k_index(value: str, t: int = T0, index: str = "BRTI") -> str:
    data = json.dumps({"type": "value", "time": t, "id": index, "value": value})
    return json.dumps(
        {"type": "cfbenchmarks_value", "sid": 4, "seq": 1, "msg": {"index_id": index, "data": data}}
    )


# --- Polymarket US ---


def pm_market(
    slug: str,
    start: int,
    end: int,
    ptb: str | None = None,
    settle: str | None = None,
    status: str = "MARKET_STATUS_OPEN",
    outcome_prices: str = '["0.5","0.5"]',
) -> dict[str, Any]:
    terms = {
        "marketType": "ASSET_PRICE_MARKET_TYPE_UP_DOWN", "indexSymbol": "BRTI", "horizon": "15m",
        "windowStart": iso(start), "windowEnd": iso(end),
        "priceToBeat": {"value": ptb, "currency": "USD"} if ptb else None,
        "settlementPrice": {"value": settle, "currency": "USD"} if settle else None,
    }  # fmt: skip
    return {
        "slug": slug, "status": status, "gameStartTime": iso(start), "orderPriceMinTickSize": 0.01,
        "minimumTradeQty": 0.01, "feeCoefficient": 0.0695, "outcomePrices": outcome_prices,
        "assetPriceTerms": terms,
    }  # fmt: skip


def pm_rest(market: dict[str, Any] | None, slug: str) -> str:
    return json.dumps(
        {"rest": "market", "params": {"slug": slug}, "body": {"market": market} if market else {}}
    )


def pm_book(slug: str, bids: list[tuple[str, str]], offers: list[tuple[str, str]], ts: int = T0) -> str:
    def lv(p: str, q: str) -> dict[str, Any]:
        return {"px": {"value": p, "currency": "USD"}, "qty": q}

    md = {"marketSlug": slug, "bids": [lv(*b) for b in bids], "offers": [lv(*o) for o in offers],
          "state": "MARKET_STATE_OPEN", "transactTime": iso(ts).replace("Z", ".123456789Z")}  # fmt: skip
    return json.dumps(
        {"requestId": "md", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA", "marketData": md}
    )


def pm_trade(slug: str, tid: str, price: str, qty: str, taker_side: str, ts: int = T0) -> str:
    tr = {"marketSlug": slug, "id": tid, "price": {"value": price, "currency": "USD"},
          "quantity": {"value": qty, "currency": "USD"}, "tradeTime": iso(ts).replace("Z", ".5Z"),
          "taker": {"side": taker_side}, "maker": {"side": "ORDER_SIDE_SELL"}}  # fmt: skip
    return json.dumps({"requestId": "tr", "subscriptionType": "SUBSCRIPTION_TYPE_TRADE", "trade": tr})


def k_index_5hz(value: str, t: int = T0, index: str = "BRTI") -> str:
    msg = {"index_id": index, "value_usd": value, "source_ts_ms": t}
    return json.dumps({"type": "cfbenchmarks_value_5hz", "sid": 5, "seq": 1, "msg": msg})
