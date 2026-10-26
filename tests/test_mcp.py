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
    return sc.get("result", sc) if isinstance(sc, dict) else json.loads(res.content[0].text)


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
    assert read_only == {"list_runs", "run_summary", "rank_strategies", "trades", "health"}
