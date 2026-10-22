from pathlib import Path

import pytest

from quanty import feeds
from quanty.core import Action, DataEvent, Quote, Trade
from quanty.feeds.base import Raw
from quanty.feeds.parse import iso_ms

FIXTURES = Path(__file__).parent / "fixtures"


def raws(name: str) -> list[Raw]:
    lines = (FIXTURES / f"{name}.jsonl").read_text().splitlines()
    return [Raw(name, 1_000 + i, line) for i, line in enumerate(lines)]


def test_iso_ms_handles_nanoseconds() -> None:
    assert iso_ms("2026-10-22T23:37:21.567139864Z") == 1792712241567
    assert iso_ms("2026-10-22T23:37:21Z") == 1792712241000
    assert iso_ms("2026-10-22T23:37:21.5Z") == 1792712241500


def test_coinbase_fixture() -> None:
    feed = feeds.make("coinbase", {})
    events = [ev for r in raws("coinbase") for ev in feed.parse(r)]
    trades = [e for e in events if isinstance(e, Trade)]
    quotes = [e for e in events if isinstance(e, Quote)]
    assert trades and quotes and len(trades) + len(quotes) == len(events)
    assert all(isinstance(e, DataEvent) and e.key == "coinbase:BTC-USD" for e in events)
    assert all(q.bid < q.ask and q.src_ts is not None for q in quotes)
    assert all(t.price > 0 and t.size > 0 and t.taker is None for t in trades)
    # the subscribe-time snapshot of past trades is not replayed as new trades
    first = raws("coinbase")[0]
    assert '"type":"snapshot","trades"' in first.text
    assert feed.parse(first) == []


def test_coinbase_heartbeats_and_errors() -> None:
    feed = feeds.make("coinbase", {})
    hb = next(r for r in raws("coinbase") if '"heartbeats"' in r.text)
    assert feed.parse(hb) == []
    with pytest.raises(ValueError):
        feed.parse(Raw("coinbase", 1, '{"type":"error","message":"bad product"}'))


def test_binance_fixture() -> None:
    feed = feeds.make("binance", {})
    events = [ev for r in raws("binance") for ev in feed.parse(r)]
    assert len(events) == 25
    quotes = [e for e in events if isinstance(e, Quote)]
    trades = [e for e in events if isinstance(e, Trade)]
    assert all(q.src_ts is None for q in quotes)  # bookTicker carries no timestamp
    assert trades and all(t.src_ts is not None and t.taker in (Action.BUY, Action.SELL) for t in trades)
    sample = Raw(
        "binance", 1, '{"stream":"btcusdt@trade","data":{"s":"BTCUSDT","p":"1","q":"2","T":5,"m":true}}'
    )
    (t,) = feed.parse(sample)
    assert isinstance(t, Trade) and t.taker is Action.SELL  # buyer was maker -> taker sold


def test_binance_url_and_config_validation() -> None:
    feed = feeds.make("binance", {"symbols": ["ethusdt", "btcusdt"], "streams": ["trade"]})
    assert feed.url() == "wss://data-stream.binance.vision/stream?streams=ethusdt@trade/btcusdt@trade"  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match=r"feeds\.binance\.streams\.0"):
        feeds.make("binance", {"streams": ["depth"]})
    with pytest.raises(ValueError, match="unknown kind"):
        feeds.make("nope", {})
    assert feeds.make("btc_spot", {"kind": "coinbase"}).name == "btc_spot"
