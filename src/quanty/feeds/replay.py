"""Turn recordings back into the event stream the engine consumes."""

from collections.abc import Iterator, Mapping
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
    errors: dict[str, int] = field(default_factory=dict)
    health: dict[str, FeedHealth] = field(init=False)

    def __post_init__(self) -> None:
        self.health = {n: FeedHealth(n, f.config, self.start) for n, f in self.feeds.items()}

    def __iter__(self) -> Iterator[Event]:
        for item in read_all(self.root, self.feeds, self.start, self.end):
            health = self.health[item.feed]
            if isinstance(item, Link):
                health.on_link(item)
                yield Status(ts=item.t, key=f"{item.feed}:link", text=item.state)
                continue
            try:
                events = self.feeds[item.feed].parse(item)
            except Exception as exc:
                if self.strict:
                    raise
                self.errors[item.feed] = self.errors.get(item.feed, 0) + 1
                if self.errors[item.feed] <= 3:
                    log.warning("parse error", feed=item.feed, t=item.t, error=repr(exc))
                health.on_raw(item, None)
                continue
            health.on_raw(item, events)
            yield from events

    def windows(self) -> list[Window]:
        return [h.window(self.end) for h in self.health.values()]
