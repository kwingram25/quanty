"""Turn recordings back into the event stream the engine consumes."""

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from quanty.core.events import Event, Status
from quanty.feeds.base import Feed, Link
from quanty.feeds.health import FeedHealth, Window
from quanty.feeds.recording import read_all

log = structlog.get_logger("quanty.replay")


@dataclass
class Replay:
    """Iterate events from recordings of `feeds` over [start, end), merged by receive time.

    Link changes become Status events keyed "<feed>:link" ("up"/"down"). Messages that fail to
    parse are counted in `errors` and skipped, or raised with strict=True. Health is tracked per
    feed as it goes; `windows()` returns the totals afterwards.
    """

    feeds: Mapping[str, Feed]
    root: Path
    start: int
    end: int
    strict: bool = False
    data_only: bool = False  # only messages that can hold data (index values): a fast model warm-up
    errors: dict[str, int] = field(default_factory=dict)
    health: dict[str, FeedHealth] = field(init=False)

    def __post_init__(self) -> None:
        self.health = {n: FeedHealth(n, f.config, self.start) for n, f in self.feeds.items()}

    def __iter__(self) -> Iterator[Event]:
        feeds: Iterable[str] = self.feeds
        contains = None
        if self.data_only:  # only what can hold data, picked out of the recordings before decoding
            feeds = [n for n, f in self.feeds.items() if f.holds_data]
            contains = {n: self.feeds[n].data_marker for n in feeds}
        for item in read_all(self.root, feeds, self.start, self.end, contains):
            health = self.health[item.feed]
            if isinstance(item, Link):
                health.on_link(item)
                self.feeds[item.feed].on_link(item)
                yield Status(ts=item.t, key=f"{item.feed}:link", text=item.state)
                continue
            if self.data_only and not self.feeds[item.feed].carries_data(item.text):
                continue
            try:
                events = self.feeds[item.feed].parse(item)
            except Exception as exc:
                if self.strict:
                    raise
                self.errors[item.feed] = self.errors.get(item.feed, 0) + 1
                if self.errors[item.feed] <= 3:
                    log.warning("parse error", feed=item.feed, t=item.t, error=repr(exc))
                health.on_raw(item, None, self.feeds[item.feed].streamed(item.text))
                continue
            health.on_raw(item, events, self.feeds[item.feed].streamed(item.text))
            yield from events

    def windows(self) -> list[Window]:
        return [h.window(self.end) for h in self.health.values()]
