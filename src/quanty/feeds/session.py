"""Record feeds live: raw messages to disk, health and latency to the database every window."""

import asyncio
import dataclasses
from collections.abc import Callable, Mapping
from pathlib import Path

import structlog
from sqlalchemy import Engine, insert

from quanty import db
from quanty.db.models import FeedMetricRow
from quanty.feeds.base import Feed, Link, Raw, now_ms
from quanty.feeds.health import FeedHealth, Status, Window
from quanty.feeds.recording import Recorder

log = structlog.get_logger("quanty.record")

OnWindow = Callable[[list[Window], dict[str, Status]], None]


class RecordSession:
    """Runs feeds, records everything they receive, and tracks their health.

    Each message is also parsed as it arrives, so a broken parser shows up in the metrics
    straight away; parsing never stops recording, since the raw message is already on disk.
    """

    def __init__(
        self, feeds: Mapping[str, Feed], root: Path, db_path: Path | None = None, window_s: float = 60
    ) -> None:
        self.feeds = dict(feeds)
        self.recorder = Recorder(root)
        self.db: Engine | None = db.connect(db_path) if db_path else None
        self.window_ms = int(window_s * 1000)
        start = now_ms()
        self.health = {name: FeedHealth(name, f.config, start) for name, f in self.feeds.items()}
        self._logged_errors: set[str] = set()

    def sink(self, item: Raw | Link) -> None:
        self.recorder.write(item)
        health = self.health[item.feed]
        if isinstance(item, Link):
            health.on_link(item)
            self.feeds[item.feed].on_link(item)
            return
        try:
            events = self.feeds[item.feed].parse(item)
        except Exception as exc:
            if item.feed not in self._logged_errors:  # once per feed; the count is in the metrics
                self._logged_errors.add(item.feed)
                log.error("parse error", feed=item.feed, error=repr(exc), message=item.text[:200])
            events = None
        health.on_raw(item, events)

    def status(self) -> dict[str, Status]:
        now = now_ms()
        return {name: h.status(now) for name, h in self.health.items()}

    async def run(self, duration_s: float | None = None, on_window: OnWindow | None = None) -> None:
        tasks = [asyncio.create_task(f.run(self.sink), name=f"feed:{n}") for n, f in self.feeds.items()]
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
                    if t.done() and not t.cancelled() and t.exception():
                        raise RuntimeError(f"{t.get_name()} crashed") from t.exception()
        finally:
            self._close_window(now_ms(), on_window)  # before shutdown, which can take seconds
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
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
