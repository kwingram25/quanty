import time
from pathlib import Path

import pytest
from helpers import M, book, opened
from sqlalchemy import insert

from quanty import db, synthetic
from quanty.brokers.sim import SimBroker
from quanty.core import Engine, Instance, Ledger, Side, Tif
from quanty.db.models import FeedMetricRow
from quanty.experiment import strategy_class
from quanty.plugins import discover
from quanty.ui import queries

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from quanty.ui.app import create_app


def backtest_ledger(path: Path) -> Ledger:
    mods = discover([], include_examples=True, local=None)
    cls = strategy_class(mods["example_favorite"])
    broker = SimBroker()
    insts = [
        Instance(n, cls(cls.params_type.from_config({"min_conf": c}, where=n)), broker)
        for n, c in (("fav_90", 0.90), ("fav_97", 0.97))
    ]
    ledger = Ledger(path, durable=True)
    Engine(insts, ledger=ledger, clock_ms=None).run(synthetic.events(synthetic.Spec(hours=2, seed=1)))
    return ledger


def live_ledger(path: Path) -> Ledger:
    """One open position, one closed round trip, and a mix of log rows."""
    from helpers import Hooks

    def act(ctx: object, b: object) -> None:
        ctx.buy(M.id, Side.YES, 0.45, 10, tif=Tif.IOC)  # type: ignore[attr-defined]

    ledger = Ledger(path, durable=True)
    s = Hooks(book=lambda ctx, b: act(ctx, b) if not s.fills() and not s.statuses() else None)
    e = Engine([Instance("live_a", s, SimBroker())], ledger=ledger, clock_ms=None, strict=False)
    e.run([*opened(), book(1000, [(0.40, 50)], [(0.45, 50)])])
    for msg, level in (
        ("shadow preview", "info"),
        ("live order rejected", "warning"),
        ("tick", "info"),
        ("live broker disabled", "error"),
    ):
        ledger.log(2000, "live_a", msg, {"order_id": "x", "request": {"big": "body"}}, level=level)
    ledger.commit()
    return ledger


@pytest.fixture
def data(tmp_path: Path) -> Path:
    (tmp_path / "backtest").mkdir()
    (tmp_path / "live").mkdir()
    backtest_ledger(tmp_path / "backtest" / "bt.sqlite").close()
    live_ledger(tmp_path / "live" / "acct.sqlite").close()
    metrics = db.connect(tmp_path / "quanty.sqlite")
    now = int(time.time() * 1000)
    rows = [
        dict(
            feed="kalshi",
            start=now - 120_000 + i * 60_000,
            end=now - 60_000 + i * 60_000,
            messages=900,
            events=800,
            parse_errors=0,
            lag_count=900,
            lag_p50=30.0,
            lag_p90=60.0,
            lag_p99=100.0 + i,
            lag_max=150.0,
            negative_lags=0,
            gaps=0,
            max_gap_ms=200,
            reconnects=0,
        )
        for i in range(2)
    ]
    with metrics.begin() as conn:
        conn.execute(insert(FeedMetricRow), rows)
    metrics.dispose()
    return tmp_path


def test_api_lists_runs_and_snapshots_with_the_ledgers_numbers(data: Path) -> None:
    c = TestClient(create_app(data))
    bt = Ledger(data / "backtest" / "bt.sqlite")
    total = sum(s.pnl for s in bt.summary())
    bt.close()
    runs = {r["name"]: r for r in c.get("/api/runs").json()}
    assert set(runs) == {"backtest/bt", "live/acct"} and not runs["backtest/bt"]["running"]
    snap = c.get("/api/runs/backtest/bt").json()
    assert abs(snap["stats"]["pnl"] - total) < 1e-9
    assert set(snap["curves"]) == {"fav_90", "fav_97"} and snap["session"] is None
    assert snap["activity"] and {"avg_price", "fees", "status"} <= set(snap["activity"][0])
    assert c.get("/api/runs/live/missing").status_code == 404
    (feed,) = c.get("/api/feeds").json()
    assert feed["feed"] == "kalshi" and feed["status"] == "ok"


def test_spa_is_served_with_the_control_token(data: Path) -> None:
    c = TestClient(create_app(data))
    page = c.get("/runs/backtest/bt")  # client-side route: the app's index page
    assert page.status_code == 200 and '<div id="root">' in page.text
    assert "__QUANTY_TOKEN__" not in page.text and 'name="quanty-token"' in page.text


def test_tape_merges_fills_and_only_notable_events(data: Path) -> None:
    run = queries.run_by_name(data, "live/acct")
    assert run is not None
    (pos,) = queries.positions(run)
    assert (pos.instance, pos.qty, round(pos.avg_price, 2)) == ("live_a", 10, 0.45)
    tape = queries.tape(run)
    assert {t["kind"] for t in tape} == {"fill", "log"}
    msgs = {t["msg"] for t in tape if t["kind"] == "log"}
    assert msgs == {"shadow preview", "live order rejected", "live broker disabled"}
    assert all("request" not in t.get("fields", {}) for t in tape)  # request bodies left out


def test_stream_pushes_a_snapshot(data: Path) -> None:
    c = TestClient(create_app(data))
    with c.websocket_connect("/api/runs/live/acct/stream") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "snapshot" and msg["data"]["run"]["name"] == "live/acct"


def test_reads_while_a_run_is_writing(tmp_path: Path) -> None:
    (tmp_path / "paper").mkdir()
    writer = live_ledger(tmp_path / "paper" / "now.sqlite")  # still open, as in a running session
    c = TestClient(create_app(tmp_path))
    assert c.get("/api/runs/paper/now").status_code == 200
    writer.log(4000, "live_a", "live order rejected", {}, level="warning")
    writer.commit()
    assert c.get("/api/runs/paper/now").json()["tape"][0]["ts"] == 4000  # the new row, read mid-write
    writer.close()


def test_ui_never_creates_files(tmp_path: Path) -> None:
    c = TestClient(create_app(tmp_path))
    assert c.get("/api/runs").json() == [] and c.get("/api/feeds").json() == []
    assert list(tmp_path.iterdir()) == []


class FakeAccountAPI:
    """Stands in for a venue's OrderAPI: keys or not, a balance or a refusal."""

    def __init__(self, label: str, keys: bool = True) -> None:
        self.label, self.keys, self.calls, self.fail = label, keys, 0, False

    @property
    def signer(self) -> object:
        if not self.keys:
            raise RuntimeError("KEY is not set")
        return object()

    async def balance(self, client: object) -> object:
        from quanty.venues.orders import AccountBalance, VenueError

        self.calls += 1
        if self.fail:
            raise VenueError(429, "too many requests")
        return AccountBalance(cash=100.0 + self.calls, buying_power=90.0, positions_value=5.0)


def test_accounts_are_cached_hide_venues_without_keys_and_back_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from quanty.ui.api import Api

    a = Api(tmp_path)
    k, p = FakeAccountAPI("Kalshi"), FakeAccountAPI("Polymarket US", keys=False)
    a.apis = {"kalshi": k, "polymarket_us": p}  # type: ignore[dict-item]
    clock = [1000.0]
    monkeypatch.setattr("quanty.ui.api.time.time", lambda: clock[0])

    first = asyncio.run(a.accounts())
    assert [(x["label"], x["cash"]) for x in first] == [("Kalshi", 101.0)]  # no keys: not shown
    asyncio.run(a.accounts())
    assert k.calls == 1  # cached
    clock[0] += 31
    k.fail = True
    busy = asyncio.run(a.accounts())[0]
    assert busy["busy"] and busy["cash"] == 101.0 and "rate limited" in busy["error"]  # last balance kept
    clock[0] += 31
    asyncio.run(a.accounts())
    assert k.calls == 2  # backing off after a refusal
    clock[0] += 120
    k.fail = False
    assert asyncio.run(a.accounts())[0]["cash"] == 103.0 and k.calls == 3


def test_session_reports_platforms(tmp_path: Path) -> None:
    from quanty.control import Controller, ControlStore

    (tmp_path / "paper").mkdir()
    ledger = backtest_ledger(tmp_path / "paper" / "x.sqlite")
    from quanty.core import Engine

    e = Engine([], ledger=ledger, clock_ms=None)
    c = Controller(ControlStore(tmp_path / "control.sqlite"), "paper/x", e, can_arm=False)
    assert c.platforms() == []
    c.feeds = lambda: {"kalshi": "ok", "polymarket_us": "stale"}
    assert c.platforms() == [{"name": "kalshi", "status": "ok"}, {"name": "polymarket_us", "status": "stale"}]


def test_orders_are_classified_and_counted(tmp_path: Path) -> None:
    from quanty import experiment, plugins, synthetic
    from quanty.core import Engine, Ledger
    from quanty.enums import OrderStatus, Tif
    from quanty.ui import queries

    R = queries.order_result
    assert [
        R(OrderStatus.FILLED, 10, 10, Tif.IOC),
        R(OrderStatus.CANCELED, 4, 10, Tif.IOC),
        R(OrderStatus.CANCELED, 0, 10, Tif.IOC),
        R(OrderStatus.EXPIRED, 0, 10, Tif.GTC),
        R(OrderStatus.REJECTED, 0, 10, Tif.IOC),
        R(OrderStatus.OPEN, 2, 10, Tif.GTC),
    ] == ["filled", "partial", "missed", "unfilled", "rejected", "resting"]

    (tmp_path / "backtest").mkdir()
    path = tmp_path / "backtest" / "slow.sqlite"
    raw = {
        "brokers": {"sim": {"kind": "sim", "latency_ms": 3000}},  # slow: some IOCs find nothing
        "instances": [{"id": "x", "strategy": "example_favorite"}],
    }
    exp = experiment.parse(raw)
    ledger = Ledger(path)
    engine = Engine(experiment.build(exp, plugins.discover([], local=None), synthetic.FEES), ledger=ledger)
    engine.run(synthetic.events(synthetic.Spec(hours=6)))
    ledger.close()
    run = queries.run_by_name(tmp_path, "backtest/slow")
    assert run is not None
    stats = queries.order_stats(run)["x"]
    assert stats["missed"] > 0 and stats["orders"] == sum(v for k, v in stats.items() if k != "orders")
    issues = queries.activity(run, needs_look=True)
    assert issues and all(a.result != "filled" for a in issues)
    assert len(issues) == min(100, stats["orders"] - stats["filled"])
