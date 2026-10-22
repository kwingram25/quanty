"""Coinbase Advanced Trade public market data: trades and best bid/offer. No API key needed."""

import json
from typing import ClassVar, Literal

import orjson
from pydantic import PositiveFloat

from quanty.core.events import Event, Quote, Trade
from quanty.feeds.base import FeedConfig, Raw, WebSocketConfig, WebSocketFeed
from quanty.feeds.parse import iso_ms


class CoinbaseConfig(WebSocketConfig):
    kind: Literal["coinbase"] = "coinbase"
    products: tuple[str, ...] = ("BTC-USD",)
    channels: tuple[Literal["market_trades", "ticker"], ...] = ("market_trades", "ticker")
    expect_every_s: PositiveFloat | None = 5  # heartbeats arrive every second


class Coinbase(WebSocketFeed):
    """Events keyed "coinbase:<product>", e.g. "coinbase:BTC-USD".

    The trade snapshot Coinbase sends on subscribe (recent past trades) is skipped, so replays never
    see old trades as new. Trade `side` is not mapped to a taker side: Coinbase's convention for
    this channel isn't documented clearly enough to rely on.
    """

    kind = "coinbase"
    config_type: ClassVar[type[FeedConfig]] = CoinbaseConfig
    default_url = "wss://advanced-trade-ws.coinbase.com"
    config: CoinbaseConfig

    def subscriptions(self) -> list[str]:
        products = list(self.config.products)
        return [
            json.dumps({"type": "subscribe", "product_ids": products, "channel": ch})
            for ch in (*self.config.channels, "heartbeats")
        ]

    def parse(self, raw: Raw) -> list[Event]:
        msg = orjson.loads(raw.text)
        channel = msg.get("channel")
        out: list[Event] = []
        if channel == "market_trades":
            for ev in msg["events"]:
                if ev["type"] == "snapshot":
                    continue
                for tr in ev["trades"]:
                    out.append(
                        Trade(ts=raw.t, src_ts=iso_ms(tr["time"]), key=f"coinbase:{tr['product_id']}",
                              price=float(tr["price"]), size=float(tr["size"]))
                    )  # fmt: skip
        elif channel == "ticker":
            src = iso_ms(msg["timestamp"])
            for ev in msg["events"]:
                for tk in ev["tickers"]:
                    out.append(
                        Quote(ts=raw.t, src_ts=src, key=f"coinbase:{tk['product_id']}",
                              bid=float(tk["best_bid"]), ask=float(tk["best_ask"]),
                              bid_size=float(tk["best_bid_quantity"]),
                              ask_size=float(tk["best_ask_quantity"]))
                    )  # fmt: skip
        elif channel == "error" or msg.get("type") == "error":
            raise ValueError(f"coinbase error message: {raw.text[:200]}")
        return out  # heartbeats, subscriptions: health only
