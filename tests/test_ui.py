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


def test_health_runs_doctor_once_then_again_at_most_once_a_minute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quanty import doctor

    runs: list[Path] = []

    def fake_run(
        project: Path, experiments: list[Path], feeds: Path | None, data: Path, offline: bool
    ) -> list[doctor.Check]:
        runs.append(project)
        return [doctor.Check("Keys", "kalshi", "fail", "no key", fix="add KALSHI_KEY_ID to .env")]

    monkeypatch.setattr(doctor, "run", fake_run)
    monkeypatch.chdir(tmp_path)
    clock = [1000.0]
    monkeypatch.setattr("quanty.ui.api.time.time", lambda: clock[0])
    c = TestClient(create_app(tmp_path))
    got = c.get("/api/health").json()
    assert got["checks"] == [
        {
            "section": "Keys",
            "name": "kalshi",
            "status": "fail",
            "detail": "no key",
            "fix": "add KALSHI_KEY_ID to .env",
        }
    ]
    c.get("/api/health")
    c.get("/api/health?fresh=1")
    assert len(runs) == 1  # cached, and a fresh run waits a minute
    clock[0] += 61
    c.get("/api/health")
    assert len(runs) == 1  # only a fresh request runs it again
    assert c.get("/api/health?fresh=1").json()["ran"] == 1061.0 and len(runs) == 2


def test_a_demo_has_no_project_to_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from quanty import doctor

    monkeypatch.setattr(doctor, "run", lambda *a, **k: pytest.fail("a demo runs no checks"))
    got = TestClient(create_app(tmp_path, demo=True)).get("/api/health").json()
    assert got["demo"] and got["checks"] == []


def test_unknown_api_paths_are_json_404s_not_the_page(tmp_path: Path) -> None:
    c = TestClient(create_app(tmp_path))
    r = c.get("/api/nope")
    assert r.status_code == 404 and "restart it" in r.json()["detail"]
    assert c.get("/runs/session/x").status_code == 200  # pages still get the app


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
    results = ("filled", "partial", "missed", "unfilled", "rejected", "resting")
    assert stats["missed"] > 0 and stats["orders"] == sum(stats[k] for k in results)
    assert stats["needs_look"] == stats["orders"] - stats["filled"]  # no stop losses in this run
    issues = queries.activity(run, needs_look=True)
    assert issues and all(a.result != "filled" for a in issues)
    assert len(issues) == min(100, stats["orders"] - stats["filled"])


def test_stop_losses_are_marked_everywhere(tmp_path: Path) -> None:
    from quanty import experiment, plugins, synthetic
    from quanty.core import Engine, Ledger
    from quanty.ui import queries

    (tmp_path / "paper").mkdir()
    raw = {
        "instances": [
            {"id": "s", "strategy": "example_favorite", "params": {"min_conf": 0.9, "stop_conf": 0.7}}
        ]
    }
    exp = experiment.parse(raw)
    ledger = Ledger(tmp_path / "paper" / "stops.sqlite")
    Engine(experiment.build(exp, plugins.discover([], local=None), synthetic.FEES), ledger=ledger).run(
        synthetic.events(synthetic.Spec(hours=24, seed=0))
    )
    ledger.close()
    run = queries.run_by_name(tmp_path, "paper/stops")
    assert run is not None
    stats = queries.order_stats(run)["s"]
    stopped = [t for t in queries.trades(run, limit=1000) if t["outcome"] == "stopped"]
    assert stats["stops"] > 0 and stopped and all(t["sold"] > 0 and t["stopped"] for t in stopped)
    assert any(a.order.tag == "stop" for a in queries.activity(run, needs_look=True))  # filled, but shown
    assert stats["needs_look"] >= stats["orders"] - stats["filled"] + 1
    tape_stops = [i for i in queries.tape(run, limit=2000) if i["kind"] == "fill" and i["tag"] == "stop"]
    assert tape_stops and all(i["action"] == "sell" for i in tape_stops)


def test_whole_history_pages_filters_and_downloads(tmp_path: Path) -> None:
    import csv
    import io

    from quanty import experiment, plugins, synthetic
    from quanty.core import Engine, Ledger

    (tmp_path / "backtest").mkdir()
    raw = {
        "instances": [
            {"id": "a", "strategy": "example_favorite", "params": {"stop_conf": 0.7}},
            {"id": "b", "strategy": "example_favorite", "params": {"min_conf": 0.97}},
        ]
    }
    exp = experiment.parse(raw)
    ledger = Ledger(tmp_path / "backtest" / "day.sqlite")
    Engine(experiment.build(exp, plugins.discover([], local=None), synthetic.FEES), ledger=ledger).run(
        synthetic.events(synthetic.Spec(hours=24, seed=0))
    )
    ledger.close()
    c = TestClient(create_app(tmp_path))
    url = "/api/runs/backtest/day"
    first = c.get(f"{url}/trades?limit=50").json()
    total = first["summary"]["trades"]
    assert total > 100 and len(first["rows"]) == 50  # more than the dashboard's first page
    seen = []
    for off in range(0, total, 50):
        seen += [
            (t["instance"], t["market_id"], t["side"])
            for t in c.get(f"{url}/trades?limit=50&offset={off}").json()["rows"]
        ]
    assert len(seen) == total == len(set(seen))  # every position once
    assert sum(first["summary"][o] for o in ("won", "lost", "stopped", "sold", "open")) == total

    stopped = c.get(f"{url}/trades?outcome=stopped&instance=a&limit=1000").json()
    assert stopped["rows"] and all(
        t["outcome"] == "stopped" and t["instance"] == "a" for t in stopped["rows"]
    )
    assert stopped["summary"]["trades"] == len(stopped["rows"])
    later = c.get(f"{url}/trades?since={first['rows'][-1]['opened']}&limit=5000").json()
    assert 0 < later["summary"]["trades"] < total

    dl = c.get(f"{url}/trades?format=csv&instance=b")
    assert (
        dl.headers["content-type"].startswith("text/csv")
        and "attachment" in dl.headers["content-disposition"]
    )
    rows = list(csv.DictReader(io.StringIO(dl.text)))
    assert len(rows) == c.get(f"{url}/trades?instance=b").json()["summary"]["trades"]
    assert rows[0]["market_title"] and rows[0]["opened"].endswith("+00:00")

    looks = c.get(f"{url}/orders?view=look&limit=5000").json()
    assert len(looks["rows"]) == sum(s["needs_look"] for s in looks["stats"].values())
    assert c.get(f"{url}/orders?view=stops&instance=a").json()["rows"]
    assert c.get(f"{url}/trades?outcome=lucky").status_code == 400
    assert c.get(f"{url}/orders?view=lucky").status_code == 400


def test_live_strategies_compare_with_their_paper_twins(tmp_path: Path) -> None:
    from quanty import experiment, plugins, synthetic
    from quanty.brokers.sim import SimBroker
    from quanty.core import Engine, Instance, Ledger

    class LooksLive(SimBroker):  # slow simulated fills, recorded as real money
        kind = "live"

    (tmp_path / "session").mkdir()
    cls = experiment.strategy_class(plugins.discover([], local=None)["example_favorite"])
    insts = [
        Instance("fav", cls(cls.params_type()), LooksLive("live", latency_ms=3000, fees=synthetic.FEES)),
        Instance(
            "fav_paper",
            cls(cls.params_type()),
            SimBroker("sim", latency_ms=50, fees=synthetic.FEES),
            twin_of="fav",
        ),
    ]
    ledger = Ledger(tmp_path / "session" / "main.sqlite")
    Engine(insts, ledger=ledger).run(synthetic.events(synthetic.Spec(hours=6, seed=0)))
    ledger.close()
    c = TestClient(create_app(tmp_path))
    snap = c.get("/api/runs/session/main").json()
    assert snap["twins"] == {"fav": "fav_paper"} and snap["real"] is True
    assert snap["stats"]["pnl_real"] == pytest.approx(
        snap["pnl_kinds"]["fav"]["live"]
    )  # by what each fill was
    pair = c.get("/api/runs/session/main/compare").json()["pairs"]["fav"]
    assert pair["twin"] == "fav_paper" and pair["since"] is not None
    assert pair["live"]["missed_pct"] > pair["paper"]["missed_pct"]  # slow fills miss more
    (tmp_path / "backtest").mkdir()
    Ledger(tmp_path / "backtest" / "x.sqlite").close()
    assert c.get("/api/runs/backtest/x/compare").status_code == 404


def test_real_money_and_simulated_are_never_one_total(tmp_path: Path) -> None:
    from quanty import experiment, plugins, synthetic
    from quanty.brokers.sim import SimBroker
    from quanty.core import Engine, Instance, Ledger

    class LooksLive(SimBroker):  # simulated fills reported as live: what the dashboard reads
        kind = "live"

    (tmp_path / "live").mkdir()
    cls = experiment.strategy_class(plugins.discover([], local=None)["example_favorite"])
    paper = SimBroker("paper", fees=synthetic.FEES)
    insts = [
        Instance("real", cls(cls.params_type()), LooksLive("live", fees=synthetic.FEES)),
        Instance("sim", cls(cls.params_type.from_config({"min_conf": 0.97})), paper),
    ]
    ledger = Ledger(tmp_path / "live" / "mixed.sqlite")
    Engine(insts, ledger=ledger).run(synthetic.events(synthetic.Spec(hours=6)))
    ledger.close()
    c = TestClient(create_app(tmp_path))
    snap = c.get("/api/runs/live/mixed").json()
    assert snap["kinds"] == {"real": "live", "sim": "sim"}
    by = {s["instance"]: s["pnl"] for s in snap["strategies"]}
    assert snap["stats"]["pnl_real"] == pytest.approx(by["real"]) and snap["stats"][
        "pnl_simulated"
    ] == pytest.approx(by["sim"])
    real = c.get("/api/runs/live/mixed/trades?money=real&limit=5000").json()
    assert {t["instance"] for t in real["rows"]} == {"real"} and real["summary"]["pnl"] == pytest.approx(
        by["real"]
    )
    sim = c.get("/api/runs/live/mixed/orders?money=simulated&limit=5000").json()
    assert {a["instance"] for a in sim["rows"]} == {"sim"}
    assert c.get("/api/runs/live/mixed/trades?money=both").status_code == 400


def test_an_instance_promoted_between_sessions_keeps_one_history(tmp_path: Path) -> None:
    from quanty import experiment, plugins, synthetic
    from quanty.brokers.sim import SimBroker
    from quanty.core import Engine, Instance, Ledger
    from quanty.ui import queries

    class LooksLive(SimBroker):
        kind = "live"

    (tmp_path / "session").mkdir()
    cls = experiment.strategy_class(plugins.discover([], local=None)["example_favorite"])
    path = tmp_path / "session" / "main.sqlite"
    for broker, start in (
        (SimBroker("sim", fees=synthetic.FEES), 0),
        (LooksLive("live", fees=synthetic.FEES), 1),
    ):
        spec = synthetic.Spec(hours=6, seed=start, start_ms=synthetic.Spec().start_ms + start * 6 * 3_600_000)
        ledger = Ledger(path)  # the same run, a later session
        Engine([Instance("fav", cls(cls.params_type()), broker)], ledger=ledger).run(synthetic.events(spec))
        ledger.close()
    run = queries.run_by_name(tmp_path, "session/main")
    assert run is not None
    by_kind = queries.pnl_by_kind(run)["fav"]
    assert set(by_kind) == {"sim", "live"}  # its paper days and its live ones, told apart
    snap = TestClient(create_app(tmp_path)).get("/api/runs/session/main").json()
    assert snap["stats"]["pnl_real"] == pytest.approx(by_kind["live"])
    assert snap["stats"]["pnl_simulated"] == pytest.approx(by_kind["sim"])
    assert snap["kinds"]["fav"] == "live"  # what it is now
    real = queries.trades(run, limit=5000, real=True)
    assert real and all(t["opened"] >= synthetic.Spec().start_ms + 6 * 3_600_000 for t in real)


def test_ledgers_from_before_kinds_still_open(tmp_path: Path) -> None:
    import sqlite3

    from quanty import experiment, plugins, synthetic
    from quanty.brokers.sim import SimBroker
    from quanty.core import Engine, Instance, Ledger

    for mode in ("live", "backtest"):  # an old live run, and an old backtest
        (tmp_path / mode).mkdir()
        cls = experiment.strategy_class(plugins.discover([], local=None)["example_favorite"])
        path = tmp_path / mode / "old.sqlite"
        ledger = Ledger(path)
        Engine([Instance("fav", cls(cls.params_type()), SimBroker(fees=synthetic.FEES))], ledger=ledger).run(
            synthetic.events(synthetic.Spec(hours=2))
        )
        ledger.close()
        with sqlite3.connect(path) as db:  # as a ledger written before kinds were recorded
            for table in ("orders", "fills", "settlements"):
                db.execute(f"ALTER TABLE {table} DROP COLUMN kind")
    c = TestClient(create_app(tmp_path))
    assert c.get("/api/runs").status_code == 200
    for run in ("live/old", "backtest/old"):
        assert c.get(f"/api/runs/{run}").status_code == 200, run
        for what in ("trades", "orders", "trades?money=real", "orders?view=look"):
            assert c.get(f"/api/runs/{run}/{what}").status_code == 200, (run, what)


def test_variants_are_ranked_by_their_worst_likely_case(tmp_path: Path) -> None:
    from quanty import experiment, plugins, synthetic
    from quanty.core import Engine, Ledger

    (tmp_path / "backtest").mkdir()
    grid = {"id": "fav_{min_conf}", "strategy": "example_favorite", "grid": {"min_conf": [0.9, 0.97]}}
    raw = {"instances": [grid]}
    exp = experiment.parse(raw)
    ledger = Ledger(tmp_path / "backtest" / "grid.sqlite")
    Engine(experiment.build(exp, plugins.discover([], local=None), synthetic.FEES), ledger=ledger).run(
        synthetic.events(synthetic.Spec(hours=24, seed=1))
    )
    ledger.close()
    c = TestClient(create_app(tmp_path))
    rows = c.get("/api/runs/backtest/grid/variants").json()
    assert [r["instance"] for r in rows] and {r["instance"] for r in rows} == {"fav_0.9", "fav_0.97"}
    for r in rows:
        assert r["per_trade"] == pytest.approx(r["pnl"] / r["trades"]) and r["range"] > 0
        assert r["lower"] < r["per_trade"]  # what it makes at worst is less than what it made
    assert rows[0]["lower"] >= rows[1]["lower"]
    from quanty.ui.queries import _wilson_low

    assert 0.7 < _wilson_low(11, 11) < 0.75 and _wilson_low(110, 110) > 0.96  # more wins, a tighter bound
