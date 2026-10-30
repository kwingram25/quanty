"""Per-feed health and latency, the same code live (while recording) and offline (over recordings).

Lag is receive time minus the source's own timestamp, for events that carry one. It includes
clock offset between us and the source, so watch it relative to its usual level; negative lags
(source clock ahead of ours) are counted, not recorded in the histogram.
"""

from collections import Counter
from dataclasses import dataclass
from typing import Literal

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
        self.last_stream_t: int | None = None  # the last message from the stream, not a poll
        self._window_start = start
        self._reset()

    def _reset(self) -> None:
        self._lags: Counter[int] = Counter()  # lag in ms -> how many: exact, and few distinct values
        self._messages = self._events = self._errors = self._negative = 0
        self._gaps = self._max_gap = self._reconnects = 0

    def on_raw(self, raw: Raw, events: list[Event] | None, streamed: bool = True) -> None:
        """Record one message; events=None means it failed to parse. streamed=False: a poll recorded
        beside the stream (Feed.streamed), which counts as a message but not as the stream alive."""
        expect = self.config.expect_every_s
        if streamed:
            if self.last_stream_t is not None:
                silence = raw.t - self.last_stream_t
                self._max_gap = max(self._max_gap, silence)
                if expect is not None and silence > expect * 1000:
                    self._gaps += 1
            self.last_stream_t = raw.t
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
                    self._lags[min(max(lag, 1), MAX_LAG_MS)] += 1

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
        last = self.last_stream_t if self.last_stream_t is not None else self.last_t
        if expect is not None and now - last > expect * 1000:
            return "stale"
        return "ok"

    def window(self, end: int) -> Window:
        """Close the current window at `end` and start the next one."""
        lags = sorted(self._lags.items())
        n = sum(c for _, c in lags)

        def pct(p: float) -> float | None:
            """The smallest lag with at least p% of them at or below it."""
            need, seen = max(1, int(p / 100 * n + 0.5)), 0
            for lag, c in lags:
                seen += c
                if seen >= need:
                    return float(lag)
            return None

        w = Window(
            self.name, self._window_start, end, self._messages, self._events, self._errors, n,
            pct(50), pct(90), pct(99), float(lags[-1][0]) if n else None, self._negative,
            self._gaps, self._max_gap, self._reconnects,
        )  # fmt: skip
        self._window_start = end
        self._reset()
        return w
