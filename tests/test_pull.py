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


def test_recordings_come_home_for_the_hours_asked_and_nothing_else(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from quanty.pull import recordings_command

    server = tmp_path / "srv" / "data"
    now = int(datetime(2026, 10, 25, 22, 13, tzinfo=UTC).timestamp() * 1000)
    for rel in (
        "kalshi/2026-10-25/22.jsonl.gz",  # this hour
        "kalshi/2026-10-25/21.jsonl.gz",  # the one before
        "kalshi/2026-10-24/10.jsonl.gz",  # too old
        "coinbase/2026-10-25/22.jsonl.gz",
        "session/main.sqlite",  # runs stay on the server
        "quanty.sqlite",
    ):
        (server / rel).parent.mkdir(parents=True, exist_ok=True)
        (server / rel).write_bytes(b"x")
    here = tmp_path / "data"
    cmd = recordings_command("me@srv", "bot", here, hours=1.5, now_ms=now)
    assert cmd[-2] == "me@srv:bot/data/"
    subprocess.run([*cmd[:-2], f"{server}/", f"{here}/"], check=True)  # the same filters, locally
    got = sorted(str(p.relative_to(here)) for p in here.rglob("*") if p.is_file())
    assert got == [
        "coinbase/2026-10-25/22.jsonl.gz",
        "kalshi/2026-10-25/21.jsonl.gz",
        "kalshi/2026-10-25/22.jsonl.gz",
    ]


def test_deploy_never_sends_keys_and_needs_them_on_the_server(tmp_path: Path) -> None:
    create(tmp_path / "bot", Answers(), vendor=False)
    _, rsync, remote = commands("me@srv", tmp_path / "bot", "bot")
    assert "--exclude=.env" in rsync and "--exclude=.env.*" in rsync and "--delete-excluded" not in rsync
    first = remote[2].split(" && ")[1]
    assert first.startswith("{ test -f .env ||") and "quanty keys" in first and "exit 4" in first
    (tmp_path / "bot" / ".env").unlink()
    commands("me@srv", tmp_path / "bot", "bot")  # no local keys needed to deploy


def test_keys_go_to_the_server_once_apart_from_the_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quanty import deploy

    (tmp_path / "bot").mkdir()
    keys = tmp_path / "bot" / ".env.server"
    keys.write_text("# the server's own\nKALSHI_KEY_ID=abc\nKALSHI_SECRET_KEY=-----BEGIN-----\nOTHER=1\n")
    assert deploy.check_keys(keys) == ["KALSHI_KEY_ID", "KALSHI_SECRET_KEY", "OTHER"]  # names, not values
    sent: dict[str, object] = {}

    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[bytes]:
        sent.update(cmd=cmd, input=kw.get("input"))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", run)
    res = CliRunner().invoke(
        cli.app, ["keys", "me@srv", "--file", str(keys), "--path", str(tmp_path / "bot")]
    )
    assert res.exit_code == 0, res.output
    assert "KALSHI_KEY_ID, KALSHI_SECRET_KEY, OTHER" in res.output and "abc" not in res.output  # never values
    cmd = sent["cmd"]
    assert isinstance(cmd, list) and cmd[:2] == ["ssh", "me@srv"]
    assert (
        "umask 077" in cmd[2] and "mv .env.new .env" in cmd[2] and cmd[2].startswith("mkdir -p bot && cd bot")
    )
    assert sent["input"] == keys.read_bytes()  # through ssh's stdin, never a command line
    empty = tmp_path / "empty.env"
    empty.write_text("NOTHING=1\n")
    assert CliRunner().invoke(cli.app, ["keys", "me@srv", "--file", str(empty)]).exit_code == 2


def test_keys_refuse_a_dollar_compose_would_substitute(tmp_path: Path) -> None:
    from quanty import deploy

    f = tmp_path / ".env.server"
    f.write_text("KALSHI_KEY_ID=abc\nQUANTY_UI_PASSWORD=scrypt$16384$8$1$salt$digest\n")
    with pytest.raises(ValueError, match="single quotes"):
        deploy.check_keys(f)
    f.write_text("KALSHI_KEY_ID=abc\nQUANTY_UI_PASSWORD='scrypt$16384$8$1$salt$digest'\n")
    assert deploy.check_keys(f) == ["KALSHI_KEY_ID", "QUANTY_UI_PASSWORD"]
