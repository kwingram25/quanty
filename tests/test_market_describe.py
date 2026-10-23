from pathlib import Path

import pytest

from quanty import synthetic
from quanty.brokers.sim import SimBroker
from quanty.control import Controller, ControlStore
from quanty.core import Engine, Instance, Ledger, Market
from quanty.strategies.example_bucket import BucketEdge, BucketParams
from quanty.strategies.example_favorite import FavoriteLate, FavoriteParams
from quanty.ui import queries


def market(**terms: object) -> Market:
    return Market(id="kalshi:KXHIGHNY-26OCT24-B72.5", venue="kalshi", series="kalshi:KXHIGHNY", open_ts=0,
                  close_ts=1, terms=terms)  # fmt: skip


def test_describe_reads_each_venues_terms() -> None:
    kalshi = market(title="Highest temperature in NYC on Oct 24?", yes_sub_title="72° to 73°",
                    event_ticker="KXHIGHNY-26OCT24", floor_strike=72)  # fmt: skip
    d = kalshi.describe()
    assert (d["title"], d["subtitle"], d["event"], d["strike"]) == (
        "Highest temperature in NYC on Oct 24?",
        "72° to 73°",
        "KXHIGHNY-26OCT24",
        72,
    )
    pm = market(title="BTC Up or Down 15m", subtitle="Oct 23, 3:30AM - 3:45AM", price_to_beat=81631.52,
                reference="cfbenchmarks:BRTI")  # fmt: skip
    assert pm.describe()["strike"] == 81631.52 and pm.describe()["reference"] == "cfbenchmarks:BRTI"
    bare = market().describe()
    assert (
        bare["title"] == "KXHIGHNY"
        and bare["event"] == "kalshi:KXHIGHNY-26OCT24-B72.5"
        and bare["strike"] is None
    )


def run_demo(tmp: Path) -> tuple[Engine, Controller]:
    broker = SimBroker(fees=synthetic.FEES)
    insts = [Instance("fav", FavoriteLate(FavoriteParams()), broker),
             Instance("temp_buckets", BucketEdge(BucketParams()), broker)]  # fmt: skip
    e = Engine(insts, ledger=Ledger(tmp / "paper" / "run.sqlite", durable=True), clock_ms=None)
    c = Controller(ControlStore(tmp / "control.sqlite"), "paper/run", e, can_arm=False)
    events = synthetic.events_multi(synthetic.Spec(hours=6, seed=7), ladder=True)
    e.run(ev for ev in events if ev.ts < synthetic.Spec().start_ms + 6 * 3_600_000 - 1_200_000)
    return e, c


def test_ladder_is_traded_named_and_reported(tmp_path: Path) -> None:
    (tmp_path / "paper").mkdir()
    e, c = run_demo(tmp_path)
    summary = {s.instance: s for s in e.ledger.summary()}
    assert summary["temp_buckets"].fills > 0 and summary["fav"].fills > 0

    # the session reports open markets with what they are and the value they settle against
    ladder = [m for m in c.markets() if m["series"] == "synthetic:temp_1h"]
    assert len(ladder) == 5 and len({m["event"] for m in ladder}) == 1
    assert ladder[0]["title"].startswith("Synthville temperature at") and ladder[0]["unit"] == "°F"
    assert isinstance(ladder[0]["reference_value"], float)
    updown = next(m for m in c.markets() if m["series"] == "synthetic:updown_5m")
    assert updown["strike"] is not None and updown["reference_value"] is not None

    # the ledger names what it traded, so a finished run can still be read
    e.ledger.commit()
    run = queries.run_by_name(tmp_path, "paper/run")
    assert run is not None
    ids = {a.order.market_id for a in queries.activity(run)}
    names = queries.names(run, ids)
    assert set(names) == ids
    assert any(n["subtitle"].endswith("°F") or "°F or above" in n["subtitle"] for n in names.values())


def test_strategy_details_are_recorded_and_read_back(tmp_path: Path) -> None:
    from quanty.core import Limits

    (tmp_path / "paper").mkdir()
    insts = [
        Instance("fav", FavoriteLate(FavoriteParams(min_conf=0.93)), SimBroker(), Limits(max_order_qty=10))
    ]
    ledger = Ledger(tmp_path / "paper" / "r.sqlite", durable=True)
    Engine(insts, ledger=ledger, clock_ms=None).run(synthetic.events(synthetic.Spec(hours=1, seed=2)))
    ledger.close()
    run = queries.run_by_name(tmp_path, "paper/r")
    assert run is not None
    info = queries.strategy_info(run)["fav"]
    assert info["strategy"] == "example_favorite" and "favorite" in info["description"]
    assert info["series"] == [
        "synthetic:updown_5m",
        "synthetic:updown_15m",
        "synthetic:updown_1h",
    ]  # a list, not text
    assert info["params_set"] == ["min_conf"] and info["params"]["min_conf"] == 0.93
    assert info["limits"] == {"max_order_qty": 10} and info["broker"] == "sim"
    assert info["markets_traded"] > 0


def test_trades_explain_every_position_and_add_up(tmp_path: Path) -> None:
    from collections import defaultdict

    (tmp_path / "paper").mkdir()
    e, _ = run_demo(tmp_path)
    e.ledger.commit()
    totals = {s.instance: s.pnl for s in e.ledger.summary()}
    run = queries.run_by_name(tmp_path, "paper/run")
    assert run is not None
    trades = queries.trades(run, limit=100_000)
    by: dict[str, float] = defaultdict(float)
    for t in trades:
        by[t["instance"]] += t["pnl"] or 0.0
        if t["outcome"] == "won":
            assert t["pnl"] > 0 and t["closed"] is not None
        if t["outcome"] == "lost":
            assert t["pnl"] == pytest.approx(-t["cost"])  # a losing position loses what it cost
    assert {k: round(v, 6) for k, v in by.items()} == {k: round(v, 6) for k, v in totals.items()}
    assert {t["outcome"] for t in trades} >= {"won", "lost"}
