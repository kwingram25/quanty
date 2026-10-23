"""Data feeds: transports, adapters, recording, replay and health."""

from collections.abc import Mapping
from functools import cache
from typing import Any

from quanty.feeds.base import Feed, FeedConfig, Link, PollingFeed, Raw, WebSocketFeed
from quanty.feeds.binance import Binance
from quanty.feeds.coinbase import Coinbase


@cache
def kinds() -> dict[str, type[Feed]]:
    """Feed kind -> class. Built on first use: venues build on feeds, so they can't be imported here."""
    from quanty.venues import Kalshi, PolymarketUS

    return {cls.kind: cls for cls in (Coinbase, Binance, Kalshi, PolymarketUS)}


def __getattr__(name: str) -> Any:
    if name == "KINDS":
        return kinds()
    raise AttributeError(name)


def make(name: str, raw: Mapping[str, Any]) -> Feed:
    """A feed from its config table; `kind` defaults to the feed's name."""
    kind = raw.get("kind", name)
    cls = kinds().get(kind)
    if cls is None:
        raise ValueError(f"feed {name}: unknown kind {kind!r} (known: {', '.join(sorted(kinds()))})")
    return cls(name, cls.config_type.from_config({**raw, "kind": kind}, where=f"feeds.{name}"))


__all__ = ["KINDS", "Feed", "FeedConfig", "Link", "PollingFeed", "Raw", "WebSocketFeed", "kinds", "make"]
