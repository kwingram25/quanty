"""Record feeds live: raw messages to disk, health and latency to the database every window.

With a trader attached (quanty paper), every parsed event also drives the engine in real time.
"""

import asyncio
import dataclasses
from collections.abc import Callable, Mapping
from pathlib import Path

import structlog
from sqlalchemy import Engine, insert

from quanty import db
from quanty.control import Controller
from quanty.core.broker import Broker
from quanty.core.engine import Engine as Trader
from quanty.core.events import Event
from quanty.core.events import Status as StatusEvent
from quanty.db.models import FeedMetricRow
from quanty.feeds.base import Feed, Link, Raw, now_ms
from quanty.feeds.health import FeedHealth, Status, Window
from quanty.feeds.recording import Recorder

log = structlog.get_logger("quanty.record")

TICK_S = 0.01
OnWindow = Callable[[list[Window], dict[str, Status]], None]


class TaskCrashed(RuntimeError):
    """A feed or the engine clock stopped with an error; the session stops too."""

    def __init__(self, task: str, cause: BaseException) -> None:
        super().__init__(f"{task.removeprefix('feed:')}: {cause}")
        self.cause = cause

    @property
    def expected(self) -> bool:
        """A setup problem with a message for the user (missing keys and the like), not a bug."""
        return type(self.cause) is RuntimeError


class RecordSession:
    """Runs feeds, records everything they receive, and tracks their health.

    Each message is also parsed as it arrives, so a broken parser shows up in the metrics
    straight away; parsing never stops recording, since the raw message is already on disk.
    """

    def __init__(
        self,
        feeds: Mapping[str, Feed],
        root: Path,
        db_path: Path | None = None,
        window_s: float = 60,
        trader: "Trader | None" = None,
        controller: "Controller | None" = None,
        record: bool = True,
    ) -> None:
        """trader: an engine to drive with the live events (paper/shadow/live); None = record only.
        record: write the raw messages to disk (off when a separate recorder does, on its own core)."""
        self.trader = trader
        self.controller = controller  # applies UI commands to the trader (quanty.control)
        if controller is not None:
            controller.feeds = self.status  # the dashboard shows each platform's connection
        self.feeds = dict(feeds)
        self.recorder = Recorder(root)
        self.record = record
        self.db: Engine | None = db.connect(db_path) if db_path else None
        self.window_ms = int(window_s * 1000)
        start = now_ms()
        self.health = {name: FeedHealth(name, f.config, start) for name, f in self.feeds.items()}
        self._logged_errors: set[str] = set()

    def sink(self, item: Raw | Link) -> None:
        if self.record:
            self.recorder.write(item)
        health = self.health[item.feed]
        if isinstance(item, Link):
            health.on_link(item)
            self.feeds[item.feed].on_link(item)
            self._drive([StatusEvent(ts=item.t, key=f"{item.feed}:link", text=item.state)])
            return
        try:
            events = self.feeds[item.feed].parse(item)
        except Exception as exc:
            if item.feed not in self._logged_errors:  # once per feed; the count is in the metrics
                self._logged_errors.add(item.feed)
                log.error("parse error", feed=item.feed, error=repr(exc), message=item.text[:200])
            events = None
        health.on_raw(item, events)
        if events:
            self._drive(events)

    def _drive(self, events: list[Event]) -> None:
        if self.trader is None:
            return
        for ev in events:
            self.trader.advance(ev.ts)  # due order arrivals, cancels and clock ticks first
            self.trader.process(ev)

    def _brokers(self) -> list["Broker"]:
        assert self.trader is not None
        return list(self.trader.brokers.values())  # spares too: an instance may be switched onto one

    async def _tick(self) -> None:
        """Advance the engine with the wall clock, so order latency and clock ticks fire on time."""
        assert self.trader is not None
        while True:
            self.trader.advance(now_ms())
            await asyncio.sleep(TICK_S)

    def status(self) -> dict[str, Status]:
        now = now_ms()
        return {name: h.status(now) for name, h in self.health.items()}

    async def run(self, duration_s: float | None = None, on_window: OnWindow | None = None) -> None:
        if self.trader is not None:
            for broker in self._brokers():
                await broker.start()
        tasks = [asyncio.create_task(f.run(self.sink), name=f"feed:{n}") for n, f in self.feeds.items()]
        if self.trader is not None:
            tasks.append(asyncio.create_task(self._tick(), name="engine clock"))
        if self.controller is not None:
            tasks.append(asyncio.create_task(self.controller.run_forever(), name="controls"))
        end = None if duration_s is None else now_ms() + int(duration_s * 1000)
        next_window = (now_ms() // self.window_ms + 1) * self.window_ms
        try:
            while end is None or now_ms() < end:
                await asyncio.sleep(min(1.0, max(0.0, ((end or next_window) - now_ms()) / 1000)))
                self.recorder.flush()
                if now_ms() >= next_window:
                    self._close_window(next_window, on_window)
                    next_window += self.window_ms
                for t in tasks:
                    if t.done() and not t.cancelled() and (exc := t.exception()):
                        raise TaskCrashed(t.get_name(), exc) from exc
        finally:
            self._close_window(now_ms(), on_window)  # before shutdown, which can take seconds
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.trader is not None:
                for broker in self._brokers():
                    await broker.aclose()
                self.trader.advance(now_ms())
                self.trader.ledger.commit()
            self.recorder.close()
            if self.db is not None:
                self.db.dispose()

    def _close_window(self, end: int, on_window: OnWindow | None) -> None:
        windows = [h.window(end) for h in self.health.values()]
        if self.db is not None:
            with self.db.begin() as conn:
                conn.execute(insert(FeedMetricRow), [dataclasses.asdict(w) for w in windows])
        if on_window is not None:
            on_window(windows, self.status())
