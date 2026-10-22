"""Per-feed health and latency, the same code live (while recording) and offline (over recordings).

Lag is receive time minus the source's own timestamp, for events that carry one. It includes
clock offset between us and the source, so watch it relative to its usual level; negative lags
(source clock ahead of ours) are counted, not recorded in the histogram.
"""

from dataclasses import dataclass
from typing import Literal

from hdrh.histogram import HdrHistogram

from quanty.core.events import Event
from quanty.feeds.base import FeedConfig, Link, Raw

MAX_LAG_MS = 600_000

Status = Literal["ok", "stale", "down", "waiting"]


@dataclass(frozen=True)
class Window:
    """Stats for one feed over [start, end)."""

    feed: str
    start: int
    end: int
    messages: int
    events: int
    parse_errors: int
    lag_count: int
    lag_p50: float | None
    lag_p90: float | None
    lag_p99: float | None
    lag_max: float | None
    negative_lags: int
    gaps: int  # silences longer than expect_every_s
    max_gap_ms: int
    reconnects: int


class FeedHealth:
    def __init__(self, name: str, config: FeedConfig, start: int) -> None:
        self.name = name
        self.config = config
        self.up: bool | None = None
        self.last_t: int | None = None
        self._window_start = start
        self._reset()

    def _reset(self) -> None:
        self._hist = HdrHistogram(1, MAX_LAG_MS, 3)
        self._messages = self._events = self._errors = self._negative = 0
        self._gaps = self._max_gap = self._reconnects = 0

    def on_raw(self, raw: Raw, events: list[Event] | None) -> None:
        """Record one message; events=None means it failed to parse."""
        expect = self.config.expect_every_s
        if self.last_t is not None:
            silence = raw.t - self.last_t
            self._max_gap = max(self._max_gap, silence)
            if expect is not None and silence > expect * 1000:
                self._gaps += 1
        self.last_t = raw.t
        self._messages += 1
        if events is None:
            self._errors += 1
            return
        self._events += len(events)
        for ev in events:
            if ev.src_ts is not None:
                lag = raw.t - ev.src_ts
                if lag < 0:
                    self._negative += 1
                else:
                    self._hist.record_value(min(max(lag, 1), MAX_LAG_MS))

    def on_link(self, link: Link) -> None:
        if link.state == "down" and self.up:
            self._reconnects += 1
        self.up = link.state == "up"

    def status(self, now: int) -> Status:
        if self.up is False:
            return "down"
        if self.last_t is None:
            return "waiting"
        expect = self.config.expect_every_s
        if expect is not None and now - self.last_t > expect * 1000:
            return "stale"
        return "ok"

    def window(self, end: int) -> Window:
        """Close the current window at `end` and start the next one."""
        h, n = self._hist, self._hist.get_total_count()

        def pct(p: float) -> float | None:
            return float(h.get_value_at_percentile(p)) if n else None

        w = Window(
            self.name, self._window_start, end, self._messages, self._events, self._errors, n,
            pct(50), pct(90), pct(99), float(h.get_max_value()) if n else None, self._negative,
            self._gaps, self._max_gap, self._reconnects,
        )  # fmt: skip
        self._window_start = end
        self._reset()
        return w
