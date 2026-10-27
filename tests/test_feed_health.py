from quanty.core import Trade
from quanty.feeds.base import FeedConfig, Link, Raw
from quanty.feeds.health import FeedHealth


def trade(t: int, src: int | None) -> Trade:
    return Trade(ts=t, src_ts=src, key="k", price=1.0, size=1.0)


def test_status_lifecycle() -> None:
    h = FeedHealth("f", FeedConfig(kind="x", expect_every_s=5), start=0)
    assert h.status(0) == "waiting"
    h.on_link(Link("f", 0, "up"))
    h.on_raw(Raw("f", 1000, "m"), [])
    assert h.status(5999) == "ok"
    assert h.status(6001) == "stale"
    h.on_link(Link("f", 7000, "down"))
    assert h.status(7001) == "down"


def test_window_stats_and_reset() -> None:
    h = FeedHealth("f", FeedConfig(kind="x", expect_every_s=1), start=0)
    h.on_link(Link("f", 0, "up"))
    for t, lag in [(100, 10), (200, 20), (300, 30), (2500, 40)]:  # 2.2 s silence = one gap
        h.on_raw(Raw("f", t, "m"), [trade(t, t - lag)])
    h.on_raw(Raw("f", 2600, "m"), [trade(2600, 2700)])  # source clock ahead: negative lag
    h.on_raw(Raw("f", 2700, "bad"), None)
    h.on_link(Link("f", 2800, "down"))
    h.on_link(Link("f", 2900, "up"))
    w = h.window(3000)
    assert (w.start, w.end, w.messages, w.events, w.parse_errors) == (0, 3000, 6, 5, 1)
    assert (w.lag_count, w.negative_lags, w.gaps, w.max_gap_ms, w.reconnects) == (4, 1, 1, 2200, 1)
    assert w.lag_p50 == 20 and w.lag_max == 40
    nxt = h.window(4000)
    assert (nxt.start, nxt.messages, nxt.lag_p50, nxt.reconnects) == (3000, 0, None, 0)


def test_lag_percentiles() -> None:
    h = FeedHealth("f", FeedConfig(kind="x"), start=0)
    for lag in [*range(100, 0, -1), 0]:  # 1..100 ms in any order, and a zero (counted as 1 ms)
        h.on_raw(Raw("f", 10_000, "m"), [trade(10_000, 10_000 - lag)])
    w = h.window(20_000)
    assert (w.lag_count, w.lag_p50, w.lag_p90, w.lag_p99, w.lag_max) == (101, 50, 90, 99, 100)


def test_no_cadence_never_stale() -> None:
    h = FeedHealth("f", FeedConfig(kind="x", expect_every_s=None), start=0)
    h.on_raw(Raw("f", 0, "m"), [])
    h.on_raw(Raw("f", 10**9, "m"), [])
    assert h.status(10**10) == "ok"
    assert h.window(10**10).gaps == 0
