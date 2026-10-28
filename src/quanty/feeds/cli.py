"""CLI commands for feeds: record, replay, and inspect health."""

import asyncio
import sys
import tomllib
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import structlog
import typer

from quanty import feeds as feedlib
from quanty.feeds.base import Feed
from quanty.feeds.health import Status, Window
from quanty.feeds.recording import recorded_feeds, span
from quanty.feeds.replay import Replay
from quanty.feeds.session import RecordSession, TaskCrashed

feeds_app = typer.Typer(help="Feed kinds and recorded feed health.", no_args_is_help=True)

DataDir = Annotated[Path, typer.Option("--data", help="Recordings directory")]
FeedNames = Annotated[list[str] | None, typer.Option("--feed", help="Feed name (repeatable); default: all")]
FeedsConfig = Annotated[
    Path | None, typer.Option("--config", help="Feed config TOML (see feeds.example.toml)")
]
Start = Annotated[
    str | None, typer.Option(help="UTC start, e.g. 2026-10-22T13:00 (default: first recording)")
]
End = Annotated[str | None, typer.Option(help="UTC end (default: last recording)")]


PROJECT_FEEDS = Path("feeds.toml")


def replay_config(config: Path | None) -> Path | None:
    """The feed config recordings are parsed with: the one given, else the project's feeds.toml,
    else built-in defaults. It matters: a feed's settings (a venue's series, say) decide which
    recorded messages become markets."""
    return config if config is not None else PROJECT_FEEDS if PROJECT_FEEDS.is_file() else None


def recorded(data: Path) -> list[str]:
    names = recorded_feeds(data)
    if not names:
        raise ValueError(f"no recordings in {data}")
    return names


def load_feeds(path: Path | None, names: list[str] | None) -> dict[str, Feed]:
    """Feeds from a config file, or built-in defaults for the named kinds."""
    tables: dict[str, dict[str, object]] = {}
    if path is not None:
        tables = tomllib.loads(path.read_text()).get("feeds", {})
    wanted = names or list(tables)
    if not wanted:
        raise ValueError("name at least one --feed, or pass --config")
    return {name: feedlib.make(name, tables.get(name, {})) for name in wanted}


def keep_days(path: Path | None) -> float | None:
    """[recording] keep_days in a feeds config: how long recordings are kept (None: forever)."""
    if path is None:
        return None
    raw = tomllib.loads(path.read_text()).get("recording", {}).get("keep_days")
    if raw is None:
        return None
    if not isinstance(raw, int | float) or isinstance(raw, bool) or raw <= 0:
        raise ValueError(f"{path.name}: [recording] keep_days must be a number of days above 0")
    return float(raw)


def parse_time(s: str) -> int:
    dt = datetime.fromisoformat(s)
    return int((dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp() * 1000)


def recorded_range(data: Path, names: list[str], start: str | None, end: str | None) -> tuple[int, int]:
    spans = [s for n in names if (s := span(data, n))]
    if not spans:
        raise ValueError(f"no recordings in {data} for {', '.join(names)}")
    lo = parse_time(start) if start else min(s[0] for s in spans)
    hi = parse_time(end) if end else max(s[1] for s in spans)
    return lo, hi


def _fail(exc: Exception) -> typer.Exit:
    typer.echo(f"quanty: {exc}", err=True)
    return typer.Exit(2)


def print_windows(windows: list[Window], status: dict[str, Status] | None = None) -> None:
    typer.echo(f"{'feed':14} {'status':8} {'msgs':>8} {'events':>8} {'err':>4} {'lag p50':>8} {'p99':>8} "
               f"{'max':>8} {'gaps':>5} {'max gap s':>9} {'reconn':>6}")  # fmt: skip

    def ms(v: float | None) -> str:
        return "-" if v is None else f"{v:.0f}"

    for w in windows:
        state = status.get(w.feed, "") if status else ""
        typer.echo(f"{w.feed:14} {state:8} {w.messages:8} {w.events:8} {w.parse_errors:4} {ms(w.lag_p50):>8} "
                   f"{ms(w.lag_p99):>8} {ms(w.lag_max):>8} {w.gaps:5} {w.max_gap_ms / 1000:9.1f} "
                   f"{w.reconnects:6}")  # fmt: skip


def record(
    feed: FeedNames = None,
    config: FeedsConfig = None,
    data: DataDir = Path("data"),
    db: Annotated[Path | None, typer.Option(help="Metrics database (default: <data>/quanty.sqlite)")] = None,
    duration: Annotated[
        float | None, typer.Option(help="Stop after this many seconds (default: run until Ctrl-C)")
    ] = None,
    window: Annotated[float, typer.Option(help="Seconds per health/metrics window")] = 60,
    take_over: Annotated[
        bool,
        typer.Option(
            "--take-over",
            help="If another recorder has these feeds, record beside it and take over when it stops "
            "(how `quanty deploy` replaces a recorder without a gap)",
        ),
    ] = False,
) -> None:
    """Record feeds to disk and track their health. Raw messages: <data>/<feed>/<date>/<hour>.jsonl.gz."""
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))
    try:
        feeds = load_feeds(config, feed)
        keep = keep_days(config)
    except ValueError as exc:
        raise _fail(exc) from None
    data.mkdir(parents=True, exist_ok=True)
    session = RecordSession(
        feeds, data, db or data / "quanty.sqlite", window_s=window, keep_days=keep, take_over=take_over
    )
    typer.echo(f"recording {', '.join(feeds)} to {data}/ (Ctrl-C to stop)")
    try:
        asyncio.run(session.run(duration, on_window=print_windows))
    except TaskCrashed as exc:
        if not exc.expected:
            raise
        raise _fail(exc) from None
    except KeyboardInterrupt:
        typer.echo("stopped")


def replay(
    data: DataDir = Path("data"),
    feed: FeedNames = None,
    config: FeedsConfig = None,
    start: Start = None,
    end: End = None,
    show: Annotated[int, typer.Option(help="Print the first N events")] = 0,
) -> None:
    """Parse recordings back into events and summarize them."""
    try:
        feeds = load_feeds(replay_config(config), feed or recorded(data))
        lo, hi = recorded_range(data, list(feeds), start, end)
    except ValueError as exc:
        raise _fail(exc) from None
    rp = Replay(feeds, data, lo, hi)
    counts: Counter[tuple[str, str]] = Counter()
    for i, ev in enumerate(rp):
        if i < show:
            typer.echo(repr(ev))
        counts[
            (getattr(ev, "key", None) or getattr(ev, "market_id", "?").split(":")[0], type(ev).__name__)
        ] += 1
    for (key, kind), n in sorted(counts.items()):
        typer.echo(f"{key:28} {kind:12} {n:10}")
    for name, n in rp.errors.items():
        typer.echo(f"{name}: {n} messages failed to parse", err=True)


@feeds_app.command("list")
def list_kinds() -> None:
    """Feed kinds quanty can record."""
    for kind, cls in sorted(feedlib.KINDS.items()):
        doc = (sys.modules[cls.__module__].__doc__ or cls.__doc__ or "").strip().splitlines()[0]
        typer.echo(f"{kind:14} parser v{cls.parser_version}  {doc}")


@feeds_app.command()
def health(
    data: DataDir = Path("data"),
    feed: FeedNames = None,
    config: FeedsConfig = None,
    start: Start = None,
    end: End = None,
) -> None:
    """Health and receive lag of recorded feeds over a period, computed from the recordings."""
    try:
        feeds = load_feeds(replay_config(config), feed or recorded(data))
        lo, hi = recorded_range(data, list(feeds), start, end)
    except ValueError as exc:
        raise _fail(exc) from None
    rp = Replay(feeds, data, lo, hi)
    for _ in rp:
        pass

    def utc(t: int) -> str:
        return datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d %H:%M")

    typer.echo(f"{utc(lo)} to {utc(hi)} UTC")
    print_windows(rp.windows())
