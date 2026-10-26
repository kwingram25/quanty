"""`quanty mcp`: the dashboard's reads and controls as tools, with the dashboard's own checks."""

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from test_control import make

from quanty.core import Ledger
from quanty.mcp import Dashboard, server
from quanty.ui.app import create_app


def call(mcp: Any, name: str, **args: Any) -> Any:
    res = asyncio.run(mcp.call_tool(name, args))
    sc = res.structured_content
    if isinstance(sc, dict):  # lists and None come wrapped as {"result": ...}
        return sc["result"] if set(sc) == {"result"} else sc
    return json.loads(res.content[0].text)


def test_tools_read_and_change_through_the_dashboard(tmp_path: Path) -> None:
    (tmp_path / "live").mkdir()
    c, _, engine = make(tmp_path, can_arm=True)  # a session with a live broker: real money, confirm needed
    Ledger(tmp_path / "live" / "acct.sqlite").close()
    c.tick()
    stop = threading.Event()

    def tick() -> None:  # the session answering commands, as it does every second
        while not stop.wait(0.05):
            c.tick()

    ticker = threading.Thread(target=tick)
    ticker.start()
    try:
        mcp = server(Dashboard("http://testserver", http=TestClient(create_app(tmp_path))))  # type: ignore[arg-type]
        assert [r["run"] for r in call(mcp, "list_runs")] == ["live/acct"]
        summary = call(mcp, "run_summary", run="live/acct")
        assert summary["running"] and {s["id"] for s in summary["strategies"]} <= {"a", "b"}
        with pytest.raises(ToolError, match="type acct to confirm"):
            call(mcp, "halt", run="live/acct", instance="a")  # a live run needs its name
        assert "halted" in str(
            asyncio.run(mcp.call_tool("halt", {"run": "live/acct", "instance": "a", "confirm": "acct"}))
        )
        assert engine.halted == {"a"}
        asyncio.run(mcp.call_tool("resume", {"run": "live/acct", "instance": "a", "confirm": "acct"}))
        assert engine.halted == set()
        with pytest.raises(ToolError, match="sim, shadow or live"):
            call(mcp, "switch_broker", run="live/acct", instance="a", to="moon", confirm="acct")
        with pytest.raises(ToolError, match="no run"):
            call(mcp, "run_summary", run="live/nope")
    finally:
        stop.set()
        ticker.join()


def test_no_tool_arms_or_raises_caps(tmp_path: Path) -> None:
    mcp = server(Dashboard("http://testserver", http=TestClient(create_app(tmp_path))))  # type: ignore[arg-type]
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert not names & {"arm", "disarm", "set_caps"} and "edit_params" in names
    read_only = {
        t.name for t in asyncio.run(mcp.list_tools()) if t.annotations and t.annotations.read_only_hint
    }
    assert read_only == {"list_runs", "run_summary", "rank_strategies", "trades", "health", "backtest_status"}


def test_backtesting_variants_on_the_dashboards_recordings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from test_sweep import project

    proj, data = project(tmp_path)  # experiments/main.toml and an hour of recordings
    (data / "session").mkdir()
    Ledger(data / "session" / "main.sqlite").close()
    monkeypatch.chdir(proj)  # the dashboard runs in the project
    mcp = server(Dashboard("http://testserver", http=TestClient(create_app(data))))  # type: ignore[arg-type]
    assert call(mcp, "backtest_status") is None
    job = call(
        mcp,
        "backtest_variants",
        run="session/main",
        instance="fav_90",
        grid={"min_conf": [0.85, 0.95]},
        hours=2,
    )
    assert job["state"] == "running" and job["variants"] == 3
    for _ in range(300):
        status = call(mcp, "backtest_status")
        if status["state"] != "running":
            break
        time.sleep(0.1)
    assert status["state"] == "done", status
    assert status["result"] in {r["run"] for r in call(mcp, "list_runs")}
    with pytest.raises(ToolError, match="at most 50"):
        call(
            mcp,
            "backtest_variants",
            run="session/main",
            instance="fav_90",
            grid={"a": list(range(8)), "b": list(range(8))},
        )
