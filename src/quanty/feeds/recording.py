"""Raw recordings: every message as received, hourly gzip files per feed.

Layout: <root>/<feed>/<YYYY-MM-DD>/<HH>.jsonl.gz (UTC). One line per item:

    {"t":1792712242986,"m":<message>}               a message; JSON messages inline, others as a string
    {"t":1792712242986,"c":1792712200000,"m":...}     ...that came over a connection (Raw.conn)
    {"t":1792712242990,"link":"down","detail":"..."}  a connection change

Inline messages are sliced back out byte for byte, so parsers see exactly what was received.
Files are append-only and safe to reopen after a restart (gzip members concatenate).

Beside an hour's file, <HH>.data.jsonl.gz holds the same lines for just its messages that can hold
data (a feed's data_marker: Kalshi's index values among its books), so a warm-up that only needs
data reads megabytes rather than the hour's every message.
"""

import fcntl
import gzip
import heapq
import time
import zlib
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Literal, cast

import orjson
import structlog

from quanty.feeds.base import Link, Raw

HOUR_MS = 3_600_000
log = structlog.get_logger("quanty.record")


def hour_path(root: Path, feed: str, t: int) -> Path:
    return root / feed / time.strftime("%Y-%m-%d/%H.jsonl.gz", time.gmtime(t / 1000))


def data_path(path: Path) -> Path:
    """The hour's data file: its messages that can hold data (Feed.data_marker), all of them from the
    hour's start, for warm-ups that need only data. Not every hour has one (older recordings)."""
    return path.with_name(path.name.replace(".jsonl.gz", ".data.jsonl.gz"))


def encode(item: Raw | Link) -> str:
    if isinstance(item, Link):
        return orjson.dumps({"t": item.t, "link": item.state, "detail": item.detail}).decode() + "\n"
    text = item.text
    head = f'{{"t":{item.t},' if item.conn is None else f'{{"t":{item.t},"c":{item.conn},'
    if text[:1] in ("{", "[") and "\n" not in text:
        try:
            orjson.loads(text)
            return f'{head}"m":{text}}}\n'
        except orjson.JSONDecodeError:
            pass
    return f'{head}"m":{orjson.dumps(text).decode()}}}\n'


def decode(feed: str, line: str) -> Raw | Link:
    comma = line.index(",", 5)
    t = int(line[5:comma])
    rest = line[comma + 1 :].rstrip("\n")
    conn = None
    if rest.startswith('"c":'):
        end = rest.index(",", 4)
        conn, rest = int(rest[4:end]), rest[end + 1 :]
    if rest.startswith('"m":'):
        body = rest[4:-1]
        return Raw(feed, t, cast(str, orjson.loads(body)) if body.startswith('"') else body, conn)
    obj = orjson.loads(line)
    return Link(feed, t, cast(Literal["up", "down"], obj["link"]), obj.get("detail", ""))


class Recorder:
    """Appends raw messages and link changes to hourly files. Call flush() regularly, close() at exit.

    One recorder per feed folder: two processes appending to one compressed file interleave their
    writes and corrupt it, so each feed's folder is locked while recorded (released at exit, even a
    killed one). A second recorder of it is refused, or with share=True (a trading session) leaves
    that feed to the other process, checking each minute whether it has stopped, to take over."""

    def __init__(
        self, root: Path, share: bool = False, data_markers: Mapping[str, str] | None = None
    ) -> None:
        """data_markers: feed -> the text its messages that can hold data contain (Feed.data_marker);
        those are written to a small file beside each hour's too (data_path), for quick warm-ups."""
        self.root = root
        self.share = share
        self.data_markers = dict(data_markers or {})
        self._files: dict[str, tuple[Path, IO[str]]] = {}
        self._data: dict[str, IO[str] | None] = {}  # feed -> this hour's data file, if it has one
        self._locks: dict[str, IO[bytes]] = {}
        self._elsewhere: dict[str, int] = {}  # feed recorded by another process -> when to try again

    def _lock(self, feed: str) -> None:
        folder = self.root / feed
        folder.mkdir(parents=True, exist_ok=True)
        fd = (folder / ".recording.lock").open("ab")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fd.close()
            raise RuntimeError(
                f"another process is already recording {feed} into {self.root}/: run one recorder per "
                "data folder (beside `quanty record`, start sessions with --no-record)"
            ) from None
        self._locks[feed] = fd

    def write(self, item: Raw | Link) -> None:
        if item.feed not in self._locks:
            retry = self._elsewhere.get(item.feed)
            if retry is not None and item.t < retry:
                return  # another process records it
            try:
                self._lock(item.feed)
            except RuntimeError:
                if not self.share:
                    raise
                if retry is None:
                    log.info("another process records this feed here; this session leaves it to it",
                             feed=item.feed, data=str(self.root))  # fmt: skip
                self._elsewhere[item.feed] = item.t + 60_000
                return
            if self._elsewhere.pop(item.feed, None) is not None:
                log.info("the other recorder stopped; this session records the feed now", feed=item.feed)
        path = hour_path(self.root, item.feed, item.t)
        cur = self._files.get(item.feed)
        if cur is None or cur[0] != path:
            if cur:
                cur[1].close()
            if (old := self._data.pop(item.feed, None)) is not None:
                old.close()
            path.parent.mkdir(parents=True, exist_ok=True)
            # a data file only for an hour it has from its start: begun with the hour, or carried on
            data = data_path(path)
            if item.feed in self.data_markers and (not path.exists() or data.exists()):
                self._data[item.feed] = cast(IO[str], gzip.open(data, "at", compresslevel=6))  # noqa: SIM115
            cur = (path, cast(IO[str], gzip.open(path, "at", compresslevel=6)))  # noqa: SIM115 - kept open for the hour
            self._files[item.feed] = cur
        line = encode(item)
        cur[1].write(line)
        d = self._data.get(item.feed)
        if d is not None and isinstance(item, Raw) and self.data_markers[item.feed] in item.text:
            d.write(line)

    def flush(self) -> None:
        for f in [*(f for _, f in self._files.values()), *(d for d in self._data.values() if d)]:
            f.flush()

    def close(self) -> None:
        for f in [*(f for _, f in self._files.values()), *(d for d in self._data.values() if d)]:
            f.close()
        self._files.clear()
        self._data.clear()
        for fd in self._locks.values():
            fd.close()  # releases the lock
        self._locks.clear()


def hours(start: int, end: int) -> Iterator[int]:
    """Start of every UTC hour overlapping [start, end)."""
    t = start - start % HOUR_MS
    while t < end:
        yield t
        t += HOUR_MS


def read(root: Path, feed: str, start: int, end: int, contains: str | None = None) -> Iterator[Raw | Link]:
    """One feed's recorded items with start <= t < end, in recorded order. contains: only lines with
    this text (link changes included only if they have it), picked out before decoding."""
    for hour in hours(start, end):
        path = hour_path(root, feed, hour)
        if not path.exists():
            continue
        if contains is not None and data_path(path).exists():
            path = data_path(path)  # the same lines, without the rest of the hour to decompress
        for line in _lines(path, contains):
            try:
                item = decode(feed, line)
            except (ValueError, KeyError):  # a damaged line: a writer killed mid-write
                continue
            if start <= item.t < end:
                yield item


def _lines(path: Path, contains: str | None) -> Iterator[str]:
    """A recording's lines; with contains, only those that have it (a substring test here is several
    times faster than decoding every line, and than gzip | grep)."""
    # errors="replace": bytes a damaged block leaves become a line that fails to decode, and is skipped
    with gzip.open(path, "rt", errors="replace") as f:
        try:
            for line in f:
                if contains is None or contains in line:
                    yield line
        except (EOFError, zlib.error, gzip.BadGzipFile):
            pass  # still being written, or a writer died mid-flush: the rest isn't readable


def read_all(
    root: Path, feeds: Iterable[str], start: int, end: int, contains: Mapping[str, str | None] | None = None
) -> Iterator[Raw | Link]:
    """Several feeds merged by receive time; contains: per feed, as for read."""
    its = (read(root, f, start, end, (contains or {}).get(f)) for f in feeds)
    return heapq.merge(*its, key=lambda item: item.t)


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


def prune(root: Path, feeds: Iterable[str], keep_days: float, now: int) -> int:
    """Delete these feeds' hourly files that ended more than keep_days ago (and emptied day folders);
    returns how many files went."""
    cutoff = now - int(keep_days * 86_400_000)
    gone = 0
    for feed in feeds:
        for day in sorted((root / feed).glob("????-??-??")):
            for path in sorted(day.glob("*.jsonl.gz")):
                try:
                    start = int(
                        datetime.strptime(f"{day.name} {path.name[:2]}", "%Y-%m-%d %H")
                        .replace(tzinfo=UTC)
                        .timestamp()
                        * 1000
                    )
                except ValueError:
                    continue
                if start + HOUR_MS <= cutoff:
                    path.unlink(missing_ok=True)
                    gone += 1
            if day.is_dir() and not any(day.iterdir()):
                day.rmdir()
    return gone
