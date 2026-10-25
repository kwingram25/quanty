"""Raw recordings: every message as received, hourly gzip files per feed.

Layout: <root>/<feed>/<YYYY-MM-DD>/<HH>.jsonl.gz (UTC). One line per item:

    {"t":1792712242986,"m":<message>}               a message; JSON messages inline, others as a string
    {"t":1792712242990,"link":"down","detail":"..."}  a connection change

Inline messages are sliced back out byte for byte, so parsers see exactly what was received.
Files are append-only and safe to reopen after a restart (gzip members concatenate).
"""

import gzip
import heapq
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Literal, cast

import orjson

from quanty.feeds.base import Link, Raw

HOUR_MS = 3_600_000


def hour_path(root: Path, feed: str, t: int) -> Path:
    return root / feed / time.strftime("%Y-%m-%d/%H.jsonl.gz", time.gmtime(t / 1000))


def encode(item: Raw | Link) -> str:
    if isinstance(item, Link):
        return orjson.dumps({"t": item.t, "link": item.state, "detail": item.detail}).decode() + "\n"
    text = item.text
    if text[:1] in ("{", "[") and "\n" not in text:
        try:
            orjson.loads(text)
            return f'{{"t":{item.t},"m":{text}}}\n'
        except orjson.JSONDecodeError:
            pass
    return f'{{"t":{item.t},"m":{orjson.dumps(text).decode()}}}\n'


def decode(feed: str, line: str) -> Raw | Link:
    comma = line.index(",", 5)
    t = int(line[5:comma])
    rest = line[comma + 1 :].rstrip("\n")
    if rest.startswith('"m":'):
        body = rest[4:-1]
        return Raw(feed, t, cast(str, orjson.loads(body)) if body.startswith('"') else body)
    obj = orjson.loads(line)
    return Link(feed, t, cast(Literal["up", "down"], obj["link"]), obj.get("detail", ""))


class Recorder:
    """Appends raw messages and link changes to hourly files. Call flush() regularly, close() at exit."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._files: dict[str, tuple[Path, IO[str]]] = {}

    def write(self, item: Raw | Link) -> None:
        path = hour_path(self.root, item.feed, item.t)
        cur = self._files.get(item.feed)
        if cur is None or cur[0] != path:
            if cur:
                cur[1].close()
            path.parent.mkdir(parents=True, exist_ok=True)
            cur = (path, cast(IO[str], gzip.open(path, "at", compresslevel=6)))  # noqa: SIM115 - kept open for the hour
            self._files[item.feed] = cur
        cur[1].write(encode(item))

    def flush(self) -> None:
        for _, f in self._files.values():
            f.flush()

    def close(self) -> None:
        for _, f in self._files.values():
            f.close()
        self._files.clear()


def hours(start: int, end: int) -> Iterator[int]:
    """Start of every UTC hour overlapping [start, end)."""
    t = start - start % HOUR_MS
    while t < end:
        yield t
        t += HOUR_MS


def read(root: Path, feed: str, start: int, end: int) -> Iterator[Raw | Link]:
    """One feed's recorded items with start <= t < end, in recorded order."""
    for hour in hours(start, end):
        path = hour_path(root, feed, hour)
        if not path.exists():
            continue
        with gzip.open(path, "rt") as f:
            try:
                for line in f:
                    item = decode(feed, line)
                    if start <= item.t < end:
                        yield item
            except EOFError:  # the file is still being written, or the writer died mid-flush
                pass


def read_all(root: Path, feeds: Iterable[str], start: int, end: int) -> Iterator[Raw | Link]:
    """Several feeds merged by receive time."""
    return heapq.merge(*(read(root, f, start, end) for f in feeds), key=lambda item: item.t)


def recorded_feeds(root: Path) -> list[str]:
    """Feeds with recordings under root (its other folders, session/ and backtest/, hold ledgers)."""
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and next(p.glob("*/*.jsonl.gz"), None))


def span(root: Path, feed: str) -> tuple[int, int] | None:
    """[first hour start, last hour end) with recordings for a feed, from the file layout."""
    paths = sorted((root / feed).glob("*/*.jsonl.gz"))
    if not paths:
        return None

    def start_of(p: Path) -> int:
        dt = datetime.strptime(f"{p.parent.name} {p.name[:2]}", "%Y-%m-%d %H").replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)

    return start_of(paths[0]), start_of(paths[-1]) + int(timedelta(hours=1).total_seconds() * 1000)
