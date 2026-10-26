"""Backtests from the dashboard's Variants tab: the experiment they run and the background job."""

import time
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quanty import experiment, sweep
from quanty.control import ControlStore
from quanty.feeds.base import Raw
from quanty.feeds.recording import Recorder
from quanty.ui.app import create_app

ROOT = Path(__file__).parent.parent
FIXTURE = (Path(__file__).parent / "fixtures" / "coinbase.jsonl").read_text().splitlines()


def project(tmp_path: Path) -> tuple[Path, Path]:
    """A project with experiments/main.toml (the example's two strategies) and an hour of recordings."""
    (tmp_path / "experiments").mkdir()
    (tmp_path / "experiments" / "main.toml").write_text((ROOT / "experiments" / "example.toml").read_text())
    data = tmp_path / "data"
    rec, now = Recorder(data), int(time.time() * 1000)
    for i, line in enumerate(FIXTURE):
        rec.write(Raw("coinbase", now - 60_000 + i, line))
    rec.close()
    return tmp_path, data


def test_plan_is_the_strategy_as_it_runs_and_its_grid_on_one_sim_broker(tmp_path: Path) -> None:
    proj, data = project(tmp_path)
    store = ControlStore(data / "control.sqlite")
    store.set_override("session/main", "fav_97", {"max_price": 0.99})  # a dashboard edit
    store.close()
    raw = sweep.plan(proj, data, "session/main", "fav_97", {"min_conf": [0.95, 0.98]}, hours=6)
    exp = experiment.parse(raw, source="t")
    assert {exp.broker_of(i) for i in exp.instances} == {"sim"}  # all simulated
    got = {i.id: i.params for i in exp.instances}
    base = {"min_conf": 0.97, "max_price": 0.99, "enter_last_s": 30}
    assert got == {
        "fav_97": base,
        "fav_97_0.95": {**base, "min_conf": 0.95},
        "fav_97_0.98": {**base, "min_conf": 0.98},
    }
    assert "fav_90" not in got


@pytest.mark.parametrize(
    ("run", "instance", "grid", "hours", "error"),
    [
        ("backtest/x", "fav_97", {"min_conf": [0.9]}, 6, "paper or live run"),
        ("session/other", "fav_97", {"min_conf": [0.9]}, 6, "no experiments/other.toml"),
        ("session/main", "nope", {"min_conf": [0.9]}, 6, "no instance 'nope'"),
        ("session/main", "fav_97", {"min_conf": []}, 6, "at least one param"),
        ("session/main", "fav_97", {"a": list(range(8)), "b": list(range(8))}, 6, "64 combinations"),
        ("session/main", "fav_97", {"min_conf": [0.9]}, 0, "hours must be above 0"),
    ],
)
def test_plan_refuses(tmp_path: Path, run: str, instance: str, grid: dict, hours: float, error: str) -> None:  # type: ignore[type-arg]
    proj, data = project(tmp_path)
    with pytest.raises(ValueError, match=error):
        sweep.plan(proj, data, run, instance, grid, hours)


def test_dashboard_runs_one_backtest_at_a_time_and_lists_its_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj, data = project(tmp_path)
    (data / "session").mkdir()
    from quanty.core.ledger import Ledger

    Ledger(data / "session" / "main.sqlite").close()
    monkeypatch.chdir(proj)
    c = TestClient(create_app(data))
    token = c.get("/").text.split('name="quanty-token" content="')[1].split('"')[0]
    body = {"instance": "fav_90", "grid": {"min_conf": [0.85, 0.95]}, "hours": 2}
    assert c.post("/api/runs/session/main/backtest", json=body).status_code == 403  # no token
    first = c.post("/api/runs/session/main/backtest", json=body, headers={"x-quanty-token": token})
    assert first.status_code == 200, first.text
    job = first.json()
    assert job["state"] == "running" and job["variants"] == 3
    again = c.post("/api/runs/session/main/backtest", json=body, headers={"x-quanty-token": token})
    assert again.status_code == 409
    for _ in range(300):
        status = c.get("/api/backtest").json()
        if status["state"] != "running":
            break
        time.sleep(0.1)
    assert status["state"] == "done", status["error"]
    runs = {r["name"] for r in c.get("/api/runs").json()}
    assert status["result"] in runs
    name = status["result"].split("/")[1]
    saved = tomllib.loads((data / "backtest" / f"{name}.toml").read_text())
    assert saved["instances"][1]["grid"] == {"min_conf": [0.85, 0.95]}
    left = {p.name for p in (data / "backtest").iterdir()}
    assert {f"{name}.sqlite", f"{name}.toml"} <= left
    assert not [n for n in left if n.endswith((".part", ".log"))]  # nothing half-written left over
