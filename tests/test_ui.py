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
from quanty.ui import charts, queries

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from quanty.ui.app import create_app


def backtest_ledger(path: Path) -> Ledger:
    mods = discover([], include_examples=True, local=None)
    cls = strategy_class(mods["example_favorite"])
    broker = SimBroker()
    insts = [Instance(n, cls(cls.params_type.from_config({"min_conf": c}, where=n)), broker)
             for n, c in (("fav_90", 0.90), ("fav_97", 0.97))]  # fmt: skip
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
    for msg, level in (("shadow preview", "info"), ("live order rejected", "warning"), ("tick", "info"),
                       ("live broker disabled", "error")):  # fmt: skip
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


def test_pages_render_with_the_ledgers_numbers(data: Path) -> None:
    c = TestClient(create_app(data))
    bt = Ledger(data / "backtest" / "bt.sqlite")
    total = sum(s.pnl for s in bt.summary())
    bt.close()
    home = c.get("/")
    assert home.status_code == 200
    assert "bt" in home.text and "acct" in home.text and "kalshi" in home.text and "ok</span>" in home.text
    page = c.get("/runs/backtest/bt")
    assert page.status_code == 200
    assert f"${total:,.2f}" in page.text
    assert page.text.count('class="line s') == 2 and page.text.count('class="direct"') == 2
    assert c.get("/runs/live/missing").status_code == 404
    assert c.get("/feeds").status_code == 200


def test_live_run_shows_positions_and_only_notable_events(data: Path) -> None:
    run = queries.run_by_name(data, "live/acct")
    assert run is not None
    (pos,) = queries.positions(run)
    assert (pos.instance, pos.qty, round(pos.avg_price, 2)) == ("live_a", 10, 0.45)
    msgs = [r.msg for r in queries.recent(run)["logs"]]
    assert set(msgs) == {"shadow preview", "live order rejected", "live broker disabled"}
    html = TestClient(create_app(data)).get("/runs/live/acct").text
    events = html.split('id="events"')[1].split("</section>")[0]
    assert "live broker disabled" in events and ">tick<" not in events
    assert '"big"' not in html  # request bodies are left out of the details column


def test_reads_while_a_run_is_writing(tmp_path: Path) -> None:
    (tmp_path / "paper").mkdir()
    writer = live_ledger(tmp_path / "paper" / "now.sqlite")  # still open, as in a running session
    writer.log(3000, "live_a", "live order rejected", {}, level="warning")
    writer.commit()
    c = TestClient(create_app(tmp_path))
    assert c.get("/runs/paper/now").status_code == 200
    writer.log(4000, "live_a", "live order rejected", {}, level="warning")
    writer.commit()
    assert c.get("/runs/paper/now").status_code == 200
    writer.close()


def test_ui_never_creates_files(tmp_path: Path) -> None:
    c = TestClient(create_app(tmp_path))
    assert "No runs yet" in c.get("/").text and "No feed metrics yet" in c.get("/feeds").text
    assert list(tmp_path.iterdir()) == []


def test_chart_colors_follow_names_and_json_is_script_safe() -> None:
    assert charts.color_slots(["b", "a"]) == {"a": 1, "b": 2}
    assert charts.color_slots(["a", "b", "c"])["b"] == 2  # adding a series doesn't repaint the others
    svg = charts.step_chart({"</script><b>": [(0, 1.0), (120_000, 2.0)]}, {"</script><b>": 1}, label="x")
    assert "</script><b>" not in svg and "&lt;/script&gt;&lt;b&gt;" in svg
