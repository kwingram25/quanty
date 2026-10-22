"""Binance spot public market data: best bid/offer and trades. No API key needed.

Uses Binance's public market-data mirror (data-stream.binance.vision): the main stream and API
hosts refuse US IPs, while the mirror carries the same streams.
"""

from typing import ClassVar, Literal

import orjson
from pydantic import PositiveFloat

from quanty.core.events import Event, Quote, Trade
from quanty.core.types import Action
from quanty.feeds.base import FeedConfig, Raw, WebSocketConfig, WebSocketFeed


class BinanceConfig(WebSocketConfig):
    kind: Literal["binance"] = "binance"
    symbols: tuple[str, ...] = ("btcusdt",)
    streams: tuple[Literal["bookTicker", "trade"], ...] = ("bookTicker", "trade")
    expect_every_s: PositiveFloat | None = 10


class Binance(WebSocketFeed):
    """Events keyed "binance:<SYMBOL>", e.g. "binance:BTCUSDT".

    bookTicker messages carry no timestamp, so quotes have no src_ts and don't count toward lag.
    """

    kind = "binance"
    config_type: ClassVar[type[FeedConfig]] = BinanceConfig
    default_url = "wss://data-stream.binance.vision/stream"
    config: BinanceConfig

    def url(self) -> str:
        streams = "/".join(f"{s.lower()}@{st}" for s in self.config.symbols for st in self.config.streams)
        return f"{self.config.url or self.default_url}?streams={streams}"

    def parse(self, raw: Raw) -> list[Event]:
        msg = orjson.loads(raw.text)
        stream, data = msg["stream"], msg["data"]
        key = f"binance:{data['s']}"
        if stream.endswith("@bookTicker"):
            return [
                Quote(ts=raw.t, key=key, bid=float(data["b"]), ask=float(data["a"]),
                      bid_size=float(data["B"]), ask_size=float(data["A"]))
            ]  # fmt: skip
        if stream.endswith("@trade"):
            taker = Action.SELL if data["m"] else Action.BUY  # m: the buyer was the maker
            return [
                Trade(
                    ts=raw.t,
                    src_ts=data["T"],
                    key=key,
                    price=float(data["p"]),
                    size=float(data["q"]),
                    taker=taker,
                )
            ]
        return []
