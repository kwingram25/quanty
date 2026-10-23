"""Venue feeds: market discovery over REST plus books and trades over a WebSocket, as one recording.

Discovery responses are recorded as raw messages too, in the same stream as the WebSocket
data, so a replay sees markets appear, books update and results arrive in exactly the order
they did live.
"""

import asyncio
import functools
import json
from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx
import structlog
from pydantic import Field, NonNegativeFloat, PositiveFloat, PositiveInt

from quanty.core.events import Book, Level
from quanty.feeds.base import FeedConfig, Raw, Sink, WebSocketConfig, WebSocketFeed, now_ms

log = structlog.get_logger("quanty.venues")


class VenueConfig(WebSocketConfig):
    lookahead_s: PositiveFloat = Field(1800, description="Subscribe to markets this long before they open")
    keep_after_s: NonNegativeFloat = Field(60, description="Stay subscribed this long after a market closes")
    results_for_s: PositiveFloat = Field(
        10800, description="Keep checking closed markets for results this long"
    )
    book_depth: PositiveInt = Field(10, description="Book levels per side in Book events")
    rest_url: str | None = Field(None, description="Override the venue's REST base URL")
    expect_every_s: PositiveFloat | None = 30


@dataclass
class Window:
    """What the transport knows about a market, to decide subscriptions and result polling."""

    id: str  # venue-native id (ticker or slug)
    open_ts: int
    close_ts: int
    settled: bool = False


class VenueFeed(WebSocketFeed):
    config_type: ClassVar[type[FeedConfig]] = VenueConfig
    config: VenueConfig
    default_rest: ClassVar[str] = ""
    overlap_s: ClassVar[float] = 3.0  # when replacing a connection, keep the old one this long

    def __init__(self, name: str, config: FeedConfig) -> None:
        super().__init__(name, config)
        self.windows: dict[str, Window] = {}
        self.transport: httpx.AsyncBaseTransport | None = None  # tests inject a mock

    # --- venue-specific ---

    @abstractmethod
    async def discover(self, client: httpx.AsyncClient, sink: Sink) -> None:
        """Called every second: fetch whatever is due, record it with rest(), update self.windows."""

    @abstractmethod
    def subscribe_messages(self, ids: frozenset[str]) -> list[str]: ...

    async def update_in_place(self, added: frozenset[str], removed: frozenset[str]) -> bool:
        """Change subscriptions on the live connection; False = reconnect instead."""
        return False

    # --- shared ---

    def rest_url(self) -> str:
        return self.config.rest_url or self.default_rest

    def rest(self, name: str, body: Any, **params: Any) -> Raw:
        """A REST response as a raw message, with what was asked."""
        return self.raw(json.dumps({"rest": name, "params": params, "body": body}, separators=(",", ":")))

    def wanted(self, now: int) -> frozenset[str]:
        ahead, after = self.config.lookahead_s * 1000, self.config.keep_after_s * 1000
        return frozenset(w.id for w in self.windows.values() if w.open_ts - ahead <= now < w.close_ts + after)

    async def run(self, sink: Sink) -> None:
        self.headers()  # missing or bad keys fail here, before anything is recorded
        async with httpx.AsyncClient(timeout=10, transport=self.transport) as client:
            disc = asyncio.create_task(self._discover_forever(client, sink))
            conn: asyncio.Task[None] | None = None
            current: frozenset[str] = frozenset()
            try:
                while True:
                    want = self.wanted(now_ms())
                    if want != current:
                        if (
                            conn is not None
                            and want
                            and await self.update_in_place(want - current, current - want)
                        ):
                            current = want
                        else:
                            old, conn = conn, None
                            if want:
                                subs = self.subscribe_messages(want)
                                conn = asyncio.create_task(
                                    self.connect_forever(sink, functools.partial(list, subs))
                                )
                            current = want
                            if old is not None:
                                await asyncio.sleep(self.overlap_s if conn else 0)
                                old.cancel()
                    for task in (disc, conn):
                        if task is not None and task.done() and not task.cancelled():
                            task.result()  # surface a crash (the connection's too: keys, signing)
                    await asyncio.sleep(1)
            finally:
                for task in (disc, conn):
                    if task is not None:
                        task.cancel()

    async def _discover_forever(self, client: httpx.AsyncClient, sink: Sink) -> None:
        while True:
            try:
                await self.discover(client, sink)
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("discovery failed", feed=self.name, error=repr(exc))
            await asyncio.sleep(1)


def top(levels: Any, n: int, reverse: bool) -> tuple[Level, ...]:
    """Best n (price, size) levels from a SortedDict of price -> size."""
    keys = levels.keys()
    picked = keys[-n:][::-1] if reverse else keys[:n]
    return tuple((p, levels[p]) for p in picked)


def same_book(a: Book | None, b: Book) -> bool:
    return a is not None and a.bids == b.bids and a.asks == b.asks
