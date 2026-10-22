"""Data feeds: transports, adapters, recording, replay and health."""

from collections.abc import Mapping
from typing import Any

from quanty.feeds.base import Feed, FeedConfig, Link, PollingFeed, Raw, WebSocketFeed
from quanty.feeds.binance import Binance
from quanty.feeds.coinbase import Coinbase

KINDS: dict[str, type[Feed]] = {cls.kind: cls for cls in (Coinbase, Binance)}


def make(name: str, raw: Mapping[str, Any]) -> Feed:
    """A feed from its config table; `kind` defaults to the feed's name."""
    kind = raw.get("kind", name)
    cls = KINDS.get(kind)
    if cls is None:
        raise ValueError(f"feed {name}: unknown kind {kind!r} (known: {', '.join(sorted(KINDS))})")
    return cls(name, cls.config_type.from_config({**raw, "kind": kind}, where=f"feeds.{name}"))


__all__ = ["KINDS", "Feed", "FeedConfig", "Link", "PollingFeed", "Raw", "WebSocketFeed", "make"]
