"""Record from a local WebSocket server, then check replay reproduces exactly what was seen live."""

import asyncio
from pathlib import Path

from sqlalchemy import create_engine, select
from typer.testing import CliRunner
from websockets.asyncio.server import ServerConnection, serve

from quanty import cli, feeds
from quanty.core import Engine, Event
from quanty.db.models import FeedMetricRow
from quanty.feeds.base import Link, Raw
from quanty.feeds.recording import read
from quanty.feeds.replay import Replay
from quanty.feeds.session import RecordSession
from quanty.signals import Last, LastConfig, RealizedVol, RealizedVolConfig

FIXTURE = (Path(__file__).parent / "fixtures" / "coinbase.jsonl").read_text().splitlines()
DROP_AFTER = 10  # the server drops the first connection after this many messages


async def record_from_local_server(tmp_path: Path) -> tuple[RecordSession, list[Raw | Link], list[Event]]:
    connections = 0

    async def handler(ws: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        lines = FIXTURE[:DROP_AFTER] if connections == 1 else FIXTURE[DROP_AFTER:]
        for line in lines:
            await ws.send(line)
            await asyncio.sleep(0.005)
        if connections > 1:
            await ws.wait_closed()  # stay up until the session ends

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        feed = feeds.make("coinbase", {"url": f"ws://127.0.0.1:{port}", "backoff_s": 0.05})
        session = RecordSession({"coinbase": feed}, tmp_path, tmp_path / "q.sqlite", window_s=3600)
        live_items: list[Raw | Link] = []
        live_events: list[Event] = []
        inner = session.sink

        def spy(item: Raw | Link) -> None:
            live_items.append(item)
            if isinstance(item, Raw):
                live_events.extend(feed.parse(item))
            inner(item)

        session.sink = spy  # type: ignore[method-assign]
        await session.run(duration_s=1.0)
    return session, live_items, live_events


def test_replay_reproduces_live_events_and_signals(tmp_path: Path) -> None:
    _, live_items, live_events = asyncio.run(record_from_local_server(tmp_path))
    raws = [i for i in live_items if isinstance(i, Raw)]
    links = [i.state for i in live_items if isinstance(i, Link)]
    assert [r.text for r in raws] == FIXTURE  # nothing lost across the reconnect
    assert links[:3] == ["up", "down", "up"]

    start, end = live_items[0].t, live_items[-1].t + 1
    assert list(read(tmp_path, "coinbase", start, end)) == live_items  # byte-identical recording

    feed = feeds.make("coinbase", {})
    rp = Replay({"coinbase": feed}, tmp_path, start, end)
    replayed = list(rp)
    assert [e for e in replayed if e.__class__.__name__ != "Status"] == live_events
    assert rp.errors == {}

    def signal_values(events: list[Event]) -> tuple[float | None, float | None]:
        sigs = [Last(LastConfig(name="px", key="coinbase:BTC-USD")),
                RealizedVol(RealizedVolConfig(name="vol", key="coinbase:BTC-USD", window_s=60))]  # fmt: skip
        Engine([], signals=sigs).run(events)
        return sigs[0].value, sigs[1].value

    assert signal_values(replayed) == signal_values(live_events)
    assert signal_values(live_events)[0] is not None

    (w,) = rp.windows()
    assert (w.messages, w.reconnects, w.parse_errors) == (len(FIXTURE), 1, 0)
    rows = create_engine(f"sqlite:///{tmp_path / 'q.sqlite'}").connect().execute(select(FeedMetricRow)).all()
    assert sum(r.messages for r in rows) == len(FIXTURE) and sum(r.reconnects for r in rows) == 1


def test_cli_replay_and_health(tmp_path: Path) -> None:
    asyncio.run(record_from_local_server(tmp_path))
    runner = CliRunner()
    res = runner.invoke(cli.app, ["replay", "--data", str(tmp_path), "--show", "2"])
    assert res.exit_code == 0, res.output
    assert "coinbase:BTC-USD" in res.stdout and "Trade" in res.stdout and "Quote" in res.stdout
    res = runner.invoke(cli.app, ["feeds", "health", "--data", str(tmp_path)])
    assert res.exit_code == 0 and "coinbase" in res.stdout
    res = runner.invoke(cli.app, ["replay", "--data", str(tmp_path / "empty")])
    assert res.exit_code == 2 and "no recordings" in res.stderr
