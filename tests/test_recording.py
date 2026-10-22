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
