import gzip
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from quanty.feeds.base import Link, Raw
from quanty.feeds.recording import HOUR_MS, Recorder, decode, encode, read, read_all, recorded_feeds, span

T0 = 1_792_710_000_000 - 1_792_710_000_000 % HOUR_MS  # an hour boundary


@pytest.mark.parametrize(
    "text",
    ['{"a":1,"b":[1,2]}', "[1,2,3]", "PONG", "not {json", '{"broken":', 'quote " and \\ and ünïcode', ""],
)
def test_encode_decode_round_trip(text: str) -> None:
    raw = Raw("f", 123, text)
    assert decode("f", encode(raw)) == raw


def test_json_messages_are_stored_inline() -> None:
    assert encode(Raw("f", 5, '{"x":1}')) == '{"t":5,"m":{"x":1}}\n'
    assert encode(Raw("f", 5, "PONG")) == '{"t":5,"m":"PONG"}\n'


def test_links_round_trip() -> None:
    link = Link("f", 9, "down", 'ConnectionClosed: "bye"')
    assert decode("f", encode(link)) == link


@given(st.text())
def test_any_text_round_trips(text: str) -> None:
    raw = Raw("f", 1, text.replace("\n", ""))
    assert decode("f", encode(raw)) == raw


def test_recorder_rotates_hourly_and_reads_ranges(tmp_path: Path) -> None:
    rec = Recorder(tmp_path)
    items: list[Raw | Link] = [
        Raw("a", T0 + 10, "1"),
        Raw("a", T0 + HOUR_MS - 1, "2"),
        Link("a", T0 + HOUR_MS, "down"),
        Raw("a", T0 + HOUR_MS + 5, "3"),
    ]
    for it in items:
        rec.write(it)
    rec.close()
    assert len(list((tmp_path / "a").glob("*/*.jsonl.gz"))) == 2
    assert list(read(tmp_path, "a", T0, T0 + 2 * HOUR_MS)) == items
    assert [i.t for i in read(tmp_path, "a", T0 + 11, T0 + HOUR_MS + 1)] == [T0 + HOUR_MS - 1, T0 + HOUR_MS]
    assert span(tmp_path, "a") == (T0, T0 + 2 * HOUR_MS)
    assert recorded_feeds(tmp_path) == ["a"]


def test_reopening_appends(tmp_path: Path) -> None:
    for text in ("1", "2"):
        rec = Recorder(tmp_path)
        rec.write(Raw("a", T0 + int(text), text))
        rec.close()
    assert [r.text for r in read(tmp_path, "a", T0, T0 + HOUR_MS) if isinstance(r, Raw)] == ["1", "2"]


def test_merge_across_feeds_is_time_ordered(tmp_path: Path) -> None:
    rec = Recorder(tmp_path)
    for t, feed in [(1, "a"), (2, "b"), (3, "a"), (3, "b"), (7, "b")]:
        rec.write(Raw(feed, T0 + t, str(t)))
    rec.close()
    merged = list(read_all(tmp_path, ["a", "b"], T0, T0 + HOUR_MS))
    assert [i.t - T0 for i in merged] == [1, 2, 3, 3, 7]


def test_truncated_file_yields_what_it_can(tmp_path: Path) -> None:
    rec = Recorder(tmp_path)
    for i in range(100):
        rec.write(Raw("a", T0 + i, f'{{"i":{i}}}'))
    rec.close()
    path = next((tmp_path / "a").glob("*/*.jsonl.gz"))
    data = path.read_bytes()
    path.write_bytes(data[: len(data) - 10])  # the writer died mid-flush
    got = list(read(tmp_path, "a", T0, T0 + HOUR_MS))
    assert 0 < len(got) <= 100
    assert gzip.decompress(data).decode().count("\n") == 100


def test_a_session_that_doesnt_record_writes_no_files(tmp_path: Path) -> None:
    from quanty import feeds
    from quanty.feeds.session import RecordSession

    feed = feeds.make("coinbase", {})
    for record, want in ((False, []), (True, ["coinbase"])):
        root = tmp_path / str(record)
        s = RecordSession({"coinbase": feed}, root, None, record=record)
        s.sink(Raw("coinbase", 1_700_000_000_000, '{"type":"heartbeat"}'))
        s.recorder.close()
        assert sorted(p.name for p in root.glob("*")) == want


def test_one_recorder_per_feed_folder(tmp_path: Path) -> None:
    first, second = Recorder(tmp_path), Recorder(tmp_path)
    first.write(Raw("kalshi", 1_700_000_000_000, "{}"))
    with pytest.raises(RuntimeError, match="already recording kalshi"):
        second.write(Raw("kalshi", 1_700_000_000_001, "{}"))
    second.write(Raw("coinbase", 1_700_000_000_001, "{}"))  # another feed is fine
    first.close()
    second.write(Raw("kalshi", 1_700_000_000_002, "{}"))  # free once the first is done
    second.close()


def test_a_damaged_recording_is_read_around_not_crashed_on(tmp_path: Path) -> None:
    t = 1_700_000_000_000
    path = tmp_path / "kalshi" / time_dir(t)
    path.parent.mkdir(parents=True)
    with gzip.open(path, "wt") as f:  # a good line, a damaged one, a good one
        f.write(
            encode(Raw("kalshi", t, "{}"))
            + '{"t":17000000spliced"x":1}\n'
            + encode(Raw("kalshi", t + 1, "{}"))
        )
    good = path.read_bytes()
    for mtime in range(200):  # the bytes after the broken block vary (gzip stamps a time): try many
        with path.open("wb") as raw:  # then a writer killed mid-block, and another appending after it
            raw.write(good)
            raw.write(gzip.compress(encode(Raw("kalshi", t + 2, "{}")).encode(), mtime=mtime)[:-12])
            raw.write(gzip.compress(encode(Raw("kalshi", t + 3, "{}")).encode(), mtime=mtime))
        got = [i.t for i in read(tmp_path, "kalshi", t, t + HOUR_MS)]
        assert got[:2] == [t, t + 1]  # the damaged line skipped, and no crash at the broken block


def time_dir(t: int) -> str:
    import time as tm

    return tm.strftime("%Y-%m-%d/%H.jsonl.gz", tm.gmtime(t / 1000))


def test_a_session_stops_cleanly_on_sigterm(tmp_path: Path) -> None:
    import asyncio
    import os
    import signal
    import time as tm

    from quanty.feeds.session import RecordSession

    session = RecordSession({}, tmp_path, None)

    async def main() -> float:
        asyncio.get_running_loop().call_later(0.3, os.kill, os.getpid(), signal.SIGTERM)
        start = tm.monotonic()
        await session.run(duration_s=30)
        return tm.monotonic() - start

    assert asyncio.run(main()) < 3  # not the 30 s asked: SIGTERM ended it


def test_old_recordings_are_pruned_by_age_and_only_the_feeds_asked(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from quanty.feeds.recording import prune

    def at(day: str, hour: int) -> int:
        return int(datetime.fromisoformat(f"{day}T{hour:02}:00:00+00:00").astimezone(UTC).timestamp() * 1000)

    for feed in ("kalshi", "coinbase"):
        for day, hour in (("2026-10-01", 5), ("2026-10-19", 23), ("2026-10-20", 0), ("2026-10-26", 12)):
            path = tmp_path / feed / day / f"{hour:02}.jsonl.gz"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
    now = at("2026-10-27", 0)  # keep 7 days: from 2026-10-20 00:00 on
    assert prune(tmp_path, ["kalshi"], 7, now) == 2
    left = sorted(str(p.relative_to(tmp_path / "kalshi")) for p in (tmp_path / "kalshi").rglob("*.gz"))
    assert left == ["2026-10-20/00.jsonl.gz", "2026-10-26/12.jsonl.gz"]
    assert not (tmp_path / "kalshi" / "2026-10-01").exists()  # its emptied day folder too
    assert len(list((tmp_path / "coinbase").rglob("*.gz"))) == 4  # not a feed it was asked about


def test_keep_days_comes_from_the_feeds_config(tmp_path: Path) -> None:
    from quanty.feeds.cli import keep_days

    cfg = tmp_path / "feeds.toml"
    cfg.write_text('[feeds.coinbase]\nkind = "coinbase"\n')
    assert keep_days(cfg) is None and keep_days(None) is None
    cfg.write_text('[recording]\nkeep_days = 21\n\n[feeds.coinbase]\nkind = "coinbase"\n')
    assert keep_days(cfg) == 21.0
    cfg.write_text("[recording]\nkeep_days = 0\n")
    with pytest.raises(ValueError, match="keep_days"):
        keep_days(cfg)
