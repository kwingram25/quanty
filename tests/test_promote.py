import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quanty import cli, experiment
from quanty.control import ControlStore
from quanty.promote import promote

TWO = """# two strategies, one (implicit) broker
[[instances]]
id = "a"
strategy = "example_favorite"

[[instances]]
id = "b"  # stays on paper
strategy = "example_favorite"
"""


def test_promote_keeps_the_others_and_the_comments(tmp_path: Path) -> None:
    exp = tmp_path / "main.toml"
    exp.write_text(TWO)
    done = promote(exp, "a", "live", max_qty=5, max_notional=4, overrides={"size": 3})
    raw = tomllib.loads(exp.read_text())
    assert raw["brokers"]["sim"]["kind"] == "sim" and raw["brokers"]["live"] == {
        "kind": "live",
        "max_qty": 5,
        "max_notional": 4,
    }
    by_id = {i["id"]: i for i in raw["instances"]}
    assert by_id["a"]["broker"] == "live" and by_id["a"]["params"] == {"size": 3}
    assert by_id["b"]["broker"] == "sim" and done.pinned == ["b"]  # explicit now that there are two
    assert "# stays on paper" in exp.read_text() and "# two strategies" in exp.read_text()
    assert experiment.live_brokers(experiment.load(exp)) == ["live"]

    promote(exp, "a", "sim")  # back: the live block stays, parked, and isn't "live" any more
    parked = experiment.load(exp)
    assert "live" in parked.brokers and experiment.live_brokers(parked) == []
    again = promote(exp, "a", "live")  # reuses it, caps and all: nothing to ask
    assert not again.added_broker and tomllib.loads(exp.read_text())["brokers"]["live"]["max_qty"] == 5


def test_promote_refusals_leave_the_file_alone(tmp_path: Path) -> None:
    exp = tmp_path / "main.toml"
    exp.write_text(TWO)
    for kw, why in (
        ({"instance": "zz", "to": "shadow"}, "no instance"),
        ({"instance": "a", "to": "moon"}, "must be one of"),
        ({"instance": "a", "to": "live"}, "--max-qty"),
        ({"instance": "a", "to": "sim"}, "already runs"),
    ):
        with pytest.raises(ValueError, match=why):
            promote(exp, kw["instance"], kw["to"])
    assert exp.read_text() == TWO


def test_promote_command_carries_dashboard_edits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "experiments").mkdir()
    exp = tmp_path / "experiments" / "main.toml"
    exp.write_text(TWO)
    store = ControlStore(tmp_path / "data" / "control.sqlite")
    store.set_override("session/main", "a", {"size": 25})  # edited in the dashboard
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(cli.app, ["promote", "a", "--to", "shadow", "--no-check"])
    assert res.exit_code == 0, res.output
    assert "carried dashboard edits into the file: size = 25" in res.output
    assert tomllib.loads(exp.read_text())["instances"][0]["params"] == {"size": 25}
    assert store.overrides("session/main") == {}  # the file holds it now
    live = CliRunner().invoke(cli.app, ["promote", "a", "--to", "live", "--no-check"], input="5\n5\n")
    assert live.exit_code == 0 and "max_qty" in live.output and "a_paper" in live.output, live.output
    assert "--arm" in live.output and "session/main" in live.output  # same run: its history stays
    by_id = {i["id"]: i for i in tomllib.loads(exp.read_text())["instances"]}
    assert by_id["a_paper"] == {
        "id": "a_paper",
        "strategy": "example_favorite",
        "broker": "shadow",
        "twin_of": "a",
        "params": {"size": 25},
    }  # the twin: same strategy and params, the old broker
    back = CliRunner().invoke(cli.app, ["promote", "a", "--to", "sim", "--no-check"])
    assert back.exit_code == 0 and "removed its paper twin a_paper" in back.output
    assert [i["id"] for i in tomllib.loads(exp.read_text())["instances"]] == ["a", "b"]
