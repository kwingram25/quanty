import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
from test_control import Sized, SizeParams
from typer.testing import CliRunner

from quanty import cli, pull
from quanty.brokers.live import LiveBroker
from quanty.brokers.sim import SimBroker
from quanty.control import Controller, ControlStore
from quanty.core import Engine, Instance, Ledger
from quanty.deploy import commands
from quanty.init import Answers, create
from quanty.version import DEPLOYED_MARK

EXP = """[brokers.paper]
kind = "sim"

[brokers.real]
kind = "live"
max_qty = 5
max_notional = 5

[[instances]]
id = "fav"
strategy = "example_favorite"
broker = "paper"
"""


def test_a_deployed_folder_keeps_dashboard_changes_with_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("QUANTY_IN_IMAGE", raising=False)
    (tmp_path / "experiments").mkdir()
    exp = tmp_path / "experiments" / "main.toml"
    exp.write_text(EXP)
    (tmp_path / DEPLOYED_MARK).touch()  # `quanty deploy` put this folder here
    engine = Engine(
        [Instance("fav", Sized(SizeParams(size=5)), SimBroker("paper"))],
        ledger=Ledger(tmp_path / "l.sqlite"),
        clock_ms=None,
        spare_brokers=[LiveBroker("real", armed=True, max_qty=10, max_notional=10)],
    )
    store = ControlStore(tmp_path / "data" / "control.sqlite")
    c = Controller(store, "session/main", engine, can_arm=True)
    c.experiment = exp
    assert c.save_mode == "copy"  # the next deploy replaces this folder's files
    store.submit("session/main", "params", "fav", {"size": 25})
    store.submit("session/main", "switch", "fav", {"to": "live"})
    c.tick()
    assert exp.read_text() == EXP  # untouched
    assert c.state()["instances"]["fav"]["switched"] == {
        "broker": "real",
        "kind": "live",
        "twin": "fav_paper",
    }
    held = store.held_changes()
    assert held == {
        "session/main": {
            "params": {"fav": {"size": 25}},
            "switches": {"fav": {"broker": "real", "twin": "fav_paper"}},
        }
    }
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(cli.app, ["changes", "--json"])
    assert res.exit_code == 0 and json.loads(res.output) == held


def test_pull_writes_the_servers_changes_into_the_files(tmp_path: Path) -> None:
    (tmp_path / "experiments").mkdir()
    exp = tmp_path / "experiments" / "main.toml"
    exp.write_text(EXP)
    held: dict[str, dict[str, Any]] = {
        "session/main": {
            "params": {"fav": {"size": 25}, "gone": {"size": 1}},
            "switches": {"fav": {"broker": "real", "twin": "fav_paper"}},
        },
        "session/other": {"params": {"x": {"size": 2}}, "switches": {}},
    }
    lines = pull.apply(tmp_path, held)
    by_id = {i["id"]: i for i in tomllib.loads(exp.read_text())["instances"]}
    assert by_id["fav"]["broker"] == "real" and by_id["fav"]["params"] == {"size": 25}
    assert by_id["fav_paper"]["twin_of"] == "fav" and by_id["fav_paper"]["broker"] == "paper"
    assert any("gone: not in main.toml" in line for line in lines)  # reported, not dropped silently
    assert any("session/other: no experiments/other.toml here" in line for line in lines)
    assert not [line for line in pull.apply(tmp_path, held) if line.startswith("fav: broker")]  # twice: no-op


def test_fetch_reads_the_json_among_dockers_noise(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        assert cmd[:2] == ["ssh", "me@srv"] and "changes --json" in cmd[2]
        out = 'Creating trader_run ... done\n{"session/main": {"params": {}, "switches": {}}}\n'
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(subprocess, "run", run)
    assert pull.fetch("me@srv", "bot") == {"session/main": {"params": {}, "switches": {}}}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 3, "", ""))
    assert pull.fetch("me@srv", "bot") == {}  # nothing deployed there yet


def test_deploy_marks_the_servers_folder(tmp_path: Path) -> None:
    create(tmp_path / "bot", Answers(), vendor=False)
    _, rsync, remote = commands("me@srv", tmp_path / "bot", "bot", stamp={"commit": "abc123", "dirty": False})
    assert f"--exclude={DEPLOYED_MARK}" in rsync  # never deleted by --delete
    assert f"""printf '%s\\n' '{{"commit":"abc123","dirty":false}}' > {DEPLOYED_MARK}""" in remote[2]
