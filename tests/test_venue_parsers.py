import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from venue_msgs import (
    MIN,
    T0,
    k_delta,
    k_index,
    k_index_5hz,
    k_snapshot,
    k_trade,
    kalshi_market,
    kalshi_rest,
    pm_book,
    pm_market,
    pm_rest,
    pm_trade,
)

from quanty import feeds
from quanty.core import Action, Book, IndexValue, Liquidity, MarketOpen, MarketTrade, Settled, Side, Status
from quanty.feeds.base import Link, Raw
from quanty.venues import auth
from quanty.venues.kalshi import Kalshi
from quanty.venues.polymarket_us import PolymarketUS, slug_for

TK = "KXBTC15M-26OCT230015-15"


def raw(text: str, t: int = T0) -> Raw:
    return Raw("v", t, text)


def kalshi(**cfg: object) -> Kalshi:
    feed = feeds.make("kalshi", cfg)
    assert isinstance(feed, Kalshi)
    return feed


def pm(**cfg: object) -> PolymarketUS:
    feed = feeds.make("polymarket_us", cfg)
    assert isinstance(feed, PolymarketUS)
    return feed


# --- Kalshi ---


def test_kalshi_markets_announce_update_and_settle() -> None:
    k = kalshi()
    (ev,) = k.parse(raw(kalshi_rest([kalshi_market(TK, T0, T0 + 15 * MIN)])))
    assert isinstance(ev, MarketOpen)
    m = ev.market
    assert (m.id, m.series, m.group, m.open_ts, m.close_ts) == (
        f"kalshi:{TK}", "kalshi:KXBTC15M", "kalshi:KXBTC15M-26OCT230015", T0, T0 + 15 * MIN)  # fmt: skip
    assert m.on_tick(0.995) and m.on_tick(0.05) and m.on_tick(0.5) and not m.on_tick(0.505)
    assert m.fee is not None and m.fee.fee(0.5, 10, Liquidity.TAKER) == pytest.approx(
        0.18
    )  # 0.175 rounded up
    assert k.parse(raw(kalshi_rest([kalshi_market(TK, T0, T0 + 15 * MIN)]))) == []  # unchanged: no event
    (upd,) = k.parse(raw(kalshi_rest([kalshi_market(TK, T0, T0 + 15 * MIN, floor_strike=81000.5)])))
    assert isinstance(upd, MarketOpen) and upd.market.terms["floor_strike"] == 81000.5
    settled = k.parse(
        raw(kalshi_rest([kalshi_market(TK, T0, T0 + 15 * MIN, floor_strike=81000.5, result="no")]))
    )
    assert settled == [Settled(ts=T0, market_id=f"kalshi:{TK}", outcome=Side.NO)]
    assert (
        k.parse(raw(kalshi_rest([kalshi_market(TK, T0, T0 + 15 * MIN, floor_strike=81000.5, result="no")])))
        == []
    )


def test_kalshi_book_from_snapshot_and_deltas() -> None:
    k = kalshi(book_depth=2)
    (b,) = k.parse(
        raw(
            k_snapshot(
                TK, yes=[("0.40", "10"), ("0.41", "5"), ("0.39", "7")], no=[("0.55", "3"), ("0.57", "4")]
            )
        )
    )
    assert isinstance(b, Book)
    assert b.bids == ((0.41, 5.0), (0.40, 10.0))
    assert b.asks == ((0.43, 4.0), (0.45, 3.0))  # YES asks = 1 − NO bids, best first
    assert k.parse(raw(k_delta(TK, "yes", "0.39", "2.5", seq=2))) == []  # below the top 2: no event
    (b,) = k.parse(raw(k_delta(TK, "yes", "0.41", "-5", seq=3)))  # best bid removed
    assert isinstance(b, Book) and b.bids == ((0.40, 10.0), (0.39, 9.5)) and b.src_ts == T0
    (b,) = k.parse(raw(k_delta(TK, "no", "0.58", "1.25", seq=4)))
    assert isinstance(b, Book) and b.asks[0] == (0.42, 1.25)


def test_kalshi_sequence_gap_is_reported_and_reconnect_resets() -> None:
    k = kalshi()
    k.parse(raw(k_snapshot(TK, yes=[("0.40", "10")], no=[("0.55", "3")])))
    events = k.parse(raw(k_delta(TK, "yes", "0.42", "1", seq=5)))  # 2..4 missing
    assert isinstance(events[0], Book) and events[0].bids[0] == (0.42, 1.0)  # the update still applies
    assert (
        isinstance(events[1], Status) and events[1].key == "kalshi:seq_gap" and "2 -> 5" not in events[1].text
    )
    assert "1 -> 5" in events[1].text
    assert k.parse(raw(k_delta(TK, "yes", "0.44", "1", seq=1))) != []  # seq 1: a new subscription, not a gap
    k.on_link(Link("v", T0, "up"))
    assert k.parse(raw(k_delta(TK, "yes", "0.43", "1", seq=6))) == []  # no snapshot yet on this connection


def test_kalshi_trades_and_index() -> None:
    k = kalshi()
    (t,) = k.parse(raw(k_trade(TK, "a", "0.6800", "1.81", "yes")))
    assert t == MarketTrade(
        ts=T0, src_ts=T0, market_id=f"kalshi:{TK}", price=0.68, qty=1.81, taker=Action.BUY
    )
    assert k.parse(raw(k_trade(TK, "a", "0.6800", "1.81", "yes"))) == []  # duplicate id
    (t,) = k.parse(raw(k_trade(TK, "b", "0.6700", "2", "no")))
    assert isinstance(t, MarketTrade) and t.taker is Action.SELL  # bought NO = sold YES
    (i,) = k.parse(raw(k_index("81280.82")))
    assert i == IndexValue(ts=T0, src_ts=T0, key="cfbenchmarks:BRTI", value=81280.82)
    (i,) = k.parse(raw(k_index_5hz("81281.5", t=T0 - 40)))
    assert i == IndexValue(ts=T0, src_ts=T0 - 40, key="cfbenchmarks:BRTI", value=81281.5)
    with pytest.raises(ValueError):
        k.parse(raw('{"type":"error","msg":{"code":6,"msg":"Already subscribed"}}'))
    (old,) = k.parse(raw(k_trade(TK, "c", "0.6700", "2", "no", legacy=True)))  # an older recording
    assert isinstance(old, MarketTrade) and old.taker is Action.SELL


def test_kalshi_subscriptions() -> None:
    k = kalshi(index_ids=["BRTI"])
    msgs = k.subscribe_messages(frozenset({"B", "A"}))
    assert [m for m in msgs if '"orderbook_delta"' in m and '["A", "B"]' in m]
    assert any('"cfbenchmarks_value_5hz"' in m and "BRTI" in m for m in msgs)


# --- Polymarket US ---

SLUG = "cpc-btc-updown-15m-2026-10-23-0000z"


def test_pm_slugs_and_series() -> None:
    spec = pm().config.series[0]
    assert slug_for(spec, T0) == SLUG
    (ev,) = pm().parse(raw(pm_rest(pm_market(SLUG, T0, T0 + 15 * MIN), SLUG)))
    assert isinstance(ev, MarketOpen) and ev.market.series == "polymarket_us:btc_updown_15m"


def test_pm_market_strike_update_and_settlement() -> None:
    p = pm()
    (ev,) = p.parse(raw(pm_rest(pm_market(SLUG, T0, T0 + 15 * MIN), SLUG)))
    assert isinstance(ev, MarketOpen)
    m = ev.market
    assert (m.open_ts, m.close_ts, m.qty_step, m.group) == (T0, T0 + 15 * MIN, 0.01, "polymarket_us:BRTI")
    assert "price_to_beat" not in m.terms and m.valid_qty(0.37) and not m.valid_qty(0.375)
    assert m.fee is not None and m.fee.fee(0.5, 100, Liquidity.MAKER) == pytest.approx(-0.3125)  # rebate
    (upd,) = p.parse(raw(pm_rest(pm_market(SLUG, T0, T0 + 15 * MIN, ptb="81317.63"), SLUG)))
    assert isinstance(upd, MarketOpen) and upd.market.terms["price_to_beat"] == 81317.63
    done = pm_market(
        SLUG, T0, T0 + 15 * MIN, ptb="81317.63", settle="81317.63", status="MARKET_STATUS_RESOLVED"
    )
    events = p.parse(raw(pm_rest(done, SLUG)))
    assert events[-1] == Settled(ts=T0, market_id=f"polymarket_us:{SLUG}", outcome=Side.YES)  # >= wins


def test_pm_resolved_without_prices_uses_outcome_prices() -> None:
    m = pm_market(SLUG, T0, T0 + 15 * MIN, status="MARKET_STATUS_RESOLVED", outcome_prices='["0","1"]')
    events = pm().parse(raw(pm_rest(m, SLUG)))
    assert isinstance(events[-1], Settled) and events[-1].outcome is Side.NO
    assert pm().parse(raw(pm_rest(None, SLUG))) == []


def test_pm_books_and_snapshot_lag() -> None:
    p = pm(book_depth=2)
    p.on_link(Link("v", T0, "up"))
    (b,) = p.parse(
        raw(
            pm_book(
                SLUG,
                bids=[("0.67", "10"), ("0.68", "1415.5"), ("0.66", "1")],
                offers=[("0.70", "3"), ("0.69", "0.01")],
            )
        )
    )
    assert isinstance(b, Book)
    assert b.bids == ((0.68, 1415.5), (0.67, 10.0)) and b.asks == ((0.69, 0.01), (0.70, 3.0))
    assert b.src_ts is None  # first snapshot after connecting: its time is the last change, not lag
    assert (
        p.parse(
            raw(
                pm_book(
                    SLUG, bids=[("0.67", "10"), ("0.68", "1415.5")], offers=[("0.69", "0.01"), ("0.70", "3")]
                )
            )
        )
        == []
    )
    (b,) = p.parse(raw(pm_book(SLUG, bids=[("0.68", "1")], offers=[("0.69", "1")], ts=T0 + 1000)))
    assert isinstance(b, Book) and b.src_ts == T0 + 1123


def test_pm_trades() -> None:
    p = pm()
    (t,) = p.parse(raw(pm_trade(SLUG, "X1", "0.69", "0.01", "ORDER_SIDE_BUY")))
    assert t == MarketTrade(
        ts=T0, src_ts=T0 + 500, market_id=f"polymarket_us:{SLUG}", price=0.69, qty=0.01, taker=Action.BUY
    )
    assert p.parse(raw(pm_trade(SLUG, "X1", "0.69", "0.01", "ORDER_SIDE_BUY"))) == []  # overlap duplicate
    (t,) = p.parse(raw(pm_trade(SLUG, "X2", "0.77", "5", "ORDER_SIDE_SELL")))
    assert isinstance(t, MarketTrade) and t.taker is Action.SELL
    with pytest.raises(ValueError):
        p.parse(raw('{"error":"bad subscription"}'))


# --- signing ---


def test_polymarket_signature_verifies() -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    secret = base64.b64encode(key.private_bytes_raw()).decode()
    h = auth.PolymarketSigner("kid", secret).headers("GET", "/v1/ws/markets")
    msg = f"{h['X-PM-Timestamp']}GET/v1/ws/markets".encode()
    key.public_key().verify(base64.b64decode(h["X-PM-Signature"]), msg)
    assert h["X-PM-Access-Key"] == "kid"


@pytest.mark.parametrize("kind", ["ed25519", "rsa"])
def test_kalshi_signature_with_one_line_pem(kind: str) -> None:
    key = ed25519.Ed25519PrivateKey.generate() if kind == "ed25519" else rsa.generate_private_key(65537, 2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    one_line = pem.decode().replace("\n", "\\n")  # as pasted into a .env file
    h = auth.KalshiSigner("kid", one_line).headers("GET", "/trade-api/ws/v2")
    assert set(h) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP", "KALSHI-ACCESS-SIGNATURE"}


def test_missing_keys_name_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "keys", lambda: auth.Keys(_env_file=None))
    monkeypatch.delenv("KALSHI_KEY_ID", raising=False)
    with pytest.raises(RuntimeError, match="KALSHI_KEY_ID"):
        auth.KalshiSigner.from_env()
    with pytest.raises(RuntimeError, match="POLYMARKET_KEY_ID"):
        auth.PolymarketSigner.from_env()
    secret = auth.Keys(_env_file=None, polymarket_secret_key="abc")
    assert "abc" not in repr(secret)  # SecretStr keeps keys out of logs and reprs


def test_kalshi_ignores_other_series() -> None:
    k = kalshi(series=["KXBTC15M"])
    other = "KXETH15M-26OCT230015-15"
    assert k.parse(raw(k_snapshot(other, yes=[("0.40", "1")], no=[("0.50", "1")]))) == []
    assert k.parse(raw(k_trade(other, "z", "0.5", "1", "yes"))) == []
    assert k.parse(raw(kalshi_rest([kalshi_market(other, T0, T0 + 15 * MIN)], series="KXETH15M"))) == []
    assert k.parse(raw(k_trade(TK, "y", "0.5", "1", "yes"))) != []


def test_kalshi_sequence_spans_series() -> None:
    """Sequence numbers count every market on a subscription, so skipped series still advance them."""
    k = kalshi(series=["KXBTC15M"])
    other = "KXETH15M-26OCT230015-15"
    k.parse(raw(k_snapshot(TK, yes=[("0.40", "1")], no=[("0.50", "1")], seq=1)))
    assert k.parse(raw(k_delta(other, "yes", "0.40", "1", seq=2))) == []
    events = k.parse(raw(k_delta(TK, "yes", "0.41", "1", seq=3)))
    assert not any(isinstance(e, Status) for e in events)


def test_kalshi_top_only_series_follow_the_ticker_channel_as_one_level_books() -> None:
    import asyncio
    import json as js

    k = kalshi(series=["KXBTC15M", "KXBTCD"], top_only=["KXBTCD"])
    ladder = "KXBTCD-26OCT2803-T80999.99"
    subs = {
        m["params"]["channels"][0]: m["params"]["market_tickers"]
        for m in map(js.loads, k.subscribe_messages(frozenset({TK, ladder})))
    }
    assert subs == {"orderbook_delta": [TK], "trade": sorted([TK, ladder]), "ticker": [ladder]}

    def tick(bid: str, bid_q: str, ask: str, ask_q: str) -> str:
        msg = {"market_ticker": ladder, "market_id": "x", "price_dollars": "0.97", "yes_bid_dollars": bid,
               "yes_ask_dollars": ask, "yes_bid_size_fp": bid_q, "yes_ask_size_fp": ask_q,
               "ts_ms": T0}  # fmt: skip
        return js.dumps({"type": "ticker", "sid": 4, "msg": msg})

    (b,) = k.parse(raw(tick("0.9700", "35.00", "0.9900", "120.00")))
    assert isinstance(b, Book) and b.market_id == f"kalshi:{ladder}"
    assert b.bids == ((0.97, 35.0),) and b.asks == ((0.99, 120.0),) and b.src_ts == T0
    assert k.parse(raw(tick("0.9700", "35.00", "0.9900", "120.00"))) == []  # unchanged: nothing new
    (empty,) = k.parse(raw(tick("0.0000", "0.00", "1.0000", "0.00")))  # an empty book (bid 0, ask 1)
    assert isinstance(empty, Book) and empty.bids == () and empty.asks == ()

    class WS:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, text: str) -> None:
            self.sent.append(text)

    ws = WS()
    k.on_connected(ws)  # type: ignore[arg-type]
    other = "KXBTCD-26OCT2803-T81249.99"
    assert asyncio.run(k.update_in_place(frozenset({other}), frozenset())) is False  # no sids yet
    for ch, sid in (("orderbook_delta", 1), ("trade", 2), ("ticker", 3)):
        k.keep(js.dumps({"type": "subscribed", "msg": {"channel": ch, "sid": sid}}))
    assert asyncio.run(k.update_in_place(frozenset({other}), frozenset({TK})))
    sent = [js.loads(s)["params"] for s in ws.sent]
    assert {(p["sid"], p["action"], tuple(p["market_tickers"])) for p in sent} == {
        (3, "add_markets", (other,)),  # the ladder strike: ticker and trades
        (2, "add_markets", (other,)),
        (1, "delete_markets", (TK,)),  # the 15m market: book and trades
        (2, "delete_markets", (TK,)),
    }
