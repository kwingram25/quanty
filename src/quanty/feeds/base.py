"""Feed base classes: a feed turns an external source into raw messages, and raw messages into events.

Transport and parsing are separate on purpose. `run()` only receives and timestamps raw text,
which is recorded as-is; `parse()` turns raw text into events and can be re-run over old
recordings when it is fixed or extended (bump `parser_version`).
"""

import asyncio
import random
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Literal

import httpx
import structlog
from pydantic import Field, PositiveFloat
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from quanty.core.events import Event
from quanty.core.validate import ConfigModel

log = structlog.get_logger("quanty.feeds")


def now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(frozen=True, slots=True)
class Raw:
    """One message exactly as received."""

    feed: str
    t: int  # receive time, epoch ms
    text: str


@dataclass(frozen=True, slots=True)
class Link:
    """A connection state change, recorded alongside messages so gaps can be explained."""

    feed: str
    t: int
    state: Literal["up", "down"]
    detail: str = ""


Sink = Callable[[Raw | Link], None]


class FeedConfig(ConfigModel):
    kind: str
    expect_every_s: PositiveFloat | None = Field(
        60, description="Health: longest normal silence; longer is a gap. None = no regular cadence"
    )
    required: bool = Field(True, description="Health: a stale required feed makes the whole host unhealthy")
    max_lag_ms: PositiveFloat | None = Field(
        None, description="Health: warn when p99 receive lag exceeds this"
    )


class Feed(ABC):
    kind: ClassVar[str]
    config_type: ClassVar[type[FeedConfig]] = FeedConfig
    parser_version: ClassVar[int] = 1
    replayable: ClassVar[bool] = True  # False = the source can't be backfilled; record it or lose it
    # for data-only replays (a model warm-up): a fixed string every message that can hold data contains,
    # so recordings are filtered before they're decoded (None: any message may); and whether any can
    holds_data: ClassVar[bool] = True
    data_marker: ClassVar[str | None] = None

    def __init__(self, name: str, config: FeedConfig) -> None:
        self.name = name
        self.config = config

    @abstractmethod
    async def run(self, sink: Sink) -> None:
        """Receive forever, passing every raw message and link change to `sink`."""

    @abstractmethod
    def parse(self, raw: Raw) -> list[Event]:
        """Events in one raw message; ts must be raw.t. Raise on malformed input.

        Parsers may keep state (e.g. an order book built from deltas), so recordings are parsed in
        order by one feed instance; on_link is called for connection changes in the same order.
        """

    def carries_data(self, text: str) -> bool:
        """Whether a raw message can hold data (an index value, a price) rather than only market
        structure (books, trades, discovery): a cheap text test, so a warm-up that only needs data
        skips the rest unparsed. Data feeds hold nothing else."""
        return True

    def on_link(self, link: Link) -> None:  # noqa: B027 - optional hook
        """A connection change, in stream order: reset state that a new connection re-sends."""

    def raw(self, text: str) -> Raw:
        return Raw(self.name, now_ms(), text)

    def link(self, state: Literal["up", "down"], detail: str = "") -> Link:
        return Link(self.name, now_ms(), state, detail)


class WebSocketConfig(FeedConfig):
    url: str | None = Field(None, description="Override the feed's default URL")
    idle_timeout_s: PositiveFloat = Field(30, description="Reconnect after this long with no message")
    backoff_s: PositiveFloat = Field(1, description="First reconnect delay; doubles up to backoff_max_s")
    backoff_max_s: PositiveFloat = 30


class WebSocketFeed(Feed):
    """Connect, subscribe, receive; reconnect with jittered exponential backoff on any failure.

    The backoff resets once a session has stayed up for a minute. A connection that stays open but
    goes silent for idle_timeout_s is treated as failed, since some feeds stall that way.
    """

    config_type: ClassVar[type[FeedConfig]] = WebSocketConfig
    default_url: ClassVar[str] = ""
    config: WebSocketConfig

    def url(self) -> str:
        return self.config.url or self.default_url

    def subscriptions(self) -> list[str]:
        """Messages to send after connecting."""
        return []

    def keep(self, text: str) -> bool:
        """False drops a message before recording (e.g. protocol pongs)."""
        return True

    def headers(self) -> dict[str, str]:
        """Extra HTTP headers for the WebSocket handshake (e.g. a signed auth header)."""
        return {}

    def on_connected(self, ws: ClientConnection) -> None:
        """Called once a connection is subscribed; ws stays valid until the session ends."""

    async def run(self, sink: Sink) -> None:
        await self.connect_forever(sink, self.subscriptions)

    async def connect_forever(self, sink: Sink, subscriptions: Callable[[], list[str]]) -> None:
        """Keep one connection up with these subscriptions. Cancelling ends it without a "down" link,
        so a connection replaced on purpose doesn't count as an outage."""
        delay = self.config.backoff_s
        while True:
            started = time.monotonic()
            try:
                await self._session(sink, subscriptions())
                reason = "closed by server"
            except (OSError, TimeoutError, WebSocketException) as exc:
                reason = f"{type(exc).__name__}: {exc}"
            sink(self.link("down", reason))
            if time.monotonic() - started > 60:
                delay = self.config.backoff_s
            wait = delay * random.uniform(0.5, 1.0)
            log.warning("feed disconnected", feed=self.name, reason=reason, retry_in_s=round(wait, 2))
            await asyncio.sleep(wait)
            delay = min(delay * 2, self.config.backoff_max_s)

    async def _session(self, sink: Sink, subscriptions: list[str]) -> None:
        async with connect(
            self.url(), additional_headers=self.headers(), max_size=None, open_timeout=10, close_timeout=2
        ) as ws:
            for sub in subscriptions:
                await ws.send(sub)
            sink(self.link("up"))
            self.on_connected(ws)
            log.info("feed connected", feed=self.name)
            while True:
                async with asyncio.timeout(self.config.idle_timeout_s):
                    msg = await ws.recv()
                text = msg if isinstance(msg, str) else msg.decode()
                if "\n" in text:  # JSON formatting only; one message per recorded line
                    text = text.replace("\r", "").replace("\n", "")
                if self.keep(text):
                    sink(self.raw(text))


class PollingConfig(FeedConfig):
    url: str | None = None
    interval_s: PositiveFloat = Field(60, description="Time between polls")
    timeout_s: PositiveFloat = 10
    retries: int = Field(3, ge=1, description="Attempts per poll before recording a failure")


class PollingFeed(Feed):
    """Fetch on a schedule (every interval_s by default; override next_poll for calendars)."""

    config_type: ClassVar[type[FeedConfig]] = PollingConfig
    default_url: ClassVar[str] = ""
    config: PollingConfig

    def request(self) -> tuple[str, dict[str, str]]:
        """(url, query params) to fetch."""
        return self.config.url or self.default_url, {}

    def next_poll(self, now: int) -> int:
        """Epoch ms of the next poll after `now`."""
        step = int(self.config.interval_s * 1000)
        return (now // step + 1) * step

    async def run(self, sink: Sink) -> None:
        up: bool | None = None
        async with httpx.AsyncClient(timeout=self.config.timeout_s) as client:
            while True:
                await asyncio.sleep(max(0, self.next_poll(now_ms()) - now_ms()) / 1000)
                try:
                    text = await self._fetch(client)
                except httpx.HTTPError as exc:
                    if up is not False:
                        sink(self.link("down", f"{type(exc).__name__}: {exc}"))
                    up = False
                    continue
                if up is not True:
                    sink(self.link("up"))
                up = True
                sink(self.raw(text))

    async def _fetch(self, client: httpx.AsyncClient) -> str:
        url, params = self.request()
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.config.retries),
            wait=wait_exponential_jitter(initial=0.5, max=10),
            retry=retry_if_exception_type(httpx.HTTPError),
            reraise=True,
        ):
            with attempt:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                return resp.text
        raise AssertionError("unreachable")
