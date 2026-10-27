"""Record feeds live: raw messages to disk, health and latency to the database every window.

With a trader attached (quanty paper), every parsed event also drives the engine in real time.
"""

import asyncio
import contextlib
import dataclasses
import signal
from collections.abc import Awaitable, Callable, Mapping
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
from quanty.feeds.recording import HOUR_MS, Recorder, prune

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
        keep_days: float | None = None,
        warmup: Callable[[], None] | None = None,
        on_ready: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """trader: an engine to drive with the live events (paper/shadow/live); None = record only.
        record: write the raw messages to disk (off when a separate recorder does, on its own core).
        keep_days: delete this session's recordings older than this, checked hourly (None: keep all).
        warmup: rebuilds the trader's state from recordings (Engine.warming), run in a thread while the
        feeds connect; live events meanwhile wait, then go through warming too (no orders on old
        events), and on_ready (restoring positions, say) runs before the first live event trades."""
        self.trader = trader
        self.controller = controller  # applies UI commands to the trader (quanty.control)
        if controller is not None:
            controller.feeds = self.status  # the dashboard shows each platform's connection
        self.feeds = dict(feeds)
        markers = {n: f.data_marker for n, f in self.feeds.items() if f.holds_data and f.data_marker}
        self.recorder = Recorder(
            root, share=trader is not None, data_markers=markers
        )  # a session defers to a recorder
        self.record = record
        self.keep_days = keep_days
        self._next_prune = 0
        self.db: Engine | None = db.connect(db_path) if db_path else None
        self.window_ms = int(window_s * 1000)
        start = now_ms()
        self.health = {name: FeedHealth(name, f.config, start) for name, f in self.feeds.items()}
        self._logged_errors: set[str] = set()
        self.warmup, self.on_ready = warmup, on_ready
        self.ready = warmup is None and on_ready is None  # trading: live events reach the strategies
        self._held: list[Event] = []  # live events while warming up

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
        if not self.ready:
            self._held.extend(events)
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
            if self.ready:
                self.trader.advance(now_ms())
            await asyncio.sleep(TICK_S)

    async def _prepare(self, tasks: list["asyncio.Task[None]"]) -> None:
        """Warm up off the event loop while the feeds connect, put the live events that came in
        meanwhile through warming as well, then trade (with the controls) from the next one."""
        assert self.trader is not None
        if self.warmup is not None:
            await asyncio.to_thread(self.warmup)
        with self.trader.warming():  # no event loop turn in here, so nothing new arrives meanwhile
            held, self._held = self._held, []
            for ev in held:
                if ev.ts < self.trader.now:  # the replay had it already (the recorder got it too)
                    continue
                self.trader.advance(ev.ts)
                self.trader.process(ev)
        if self.on_ready is not None:
            await self.on_ready()
        self.ready = True
        if self.controller is not None:
            tasks.append(asyncio.create_task(self.controller.run_forever(), name="controls"))

    def status(self) -> dict[str, Status]:
        now = now_ms()
        return {name: h.status(now) for name, h in self.health.items()}

    def stop(self) -> None:
        """End the run at its next check (within a second), cleanly: files closed, brokers stopped."""
        self._stopping = True

    async def run(self, duration_s: float | None = None, on_window: OnWindow | None = None) -> None:
        self._stopping = False
        # `docker stop` (and systemd) send SIGTERM: stop as cleanly as Ctrl-C, never mid-write (off the
        # main thread, or where signal handlers don't exist, it can't be caught)
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, self.stop)
        if self.trader is not None:
            for broker in self._brokers():
                await broker.start()
        tasks = [asyncio.create_task(f.run(self.sink), name=f"feed:{n}") for n, f in self.feeds.items()]
        if self.trader is not None:
            tasks.append(asyncio.create_task(self._tick(), name="engine clock"))
            if self.ready:
                if self.controller is not None:
                    tasks.append(asyncio.create_task(self.controller.run_forever(), name="controls"))
            else:
                tasks.append(asyncio.create_task(self._prepare(tasks), name="warm-up"))
        end = None if duration_s is None else now_ms() + int(duration_s * 1000)
        next_window = (now_ms() // self.window_ms + 1) * self.window_ms
        try:
            while (end is None or now_ms() < end) and not self._stopping:
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

    def _prune(self, now: int) -> None:
        if not self.record or self.keep_days is None or now < self._next_prune:
            return
        self._next_prune = now + HOUR_MS
        gone = prune(self.recorder.root, self.feeds, self.keep_days, now)
        if gone:
            log.info("old recordings deleted", files=gone, keep_days=self.keep_days)

    def _close_window(self, end: int, on_window: OnWindow | None) -> None:
        self._prune(end)
        windows = [h.window(end) for h in self.health.values()]
        if self.db is not None:
            with self.db.begin() as conn:
                conn.execute(insert(FeedMetricRow), [dataclasses.asdict(w) for w in windows])
        if on_window is not None:
            on_window(windows, self.status())
