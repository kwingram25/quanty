import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quanty import cli, experiment, plugins
from quanty.init import Answers, create

runner = CliRunner()


def test_init_writes_a_working_project(tmp_path: Path) -> None:
    proj = tmp_path / "p"
    written = create(proj, Answers(strategy="edge_finder", venues=["kalshi", "polymarket_us"]), vendor=False)
    assert {"strategies/edge_finder.py", "experiments/main.toml", "feeds.toml", ".env", "Dockerfile",
            "compose.yaml"} <= set(written)  # fmt: skip
    assert stat.S_IMODE((proj / ".env").stat().st_mode) == 0o600  # keys: owner only
    assert ".env" in (proj / ".dockerignore").read_text()  # never baked into the image
    found = plugins.discover([proj / "strategies"], include_examples=False, local=None)
    exp = experiment.load(proj / "experiments" / "main.toml")
    (inst,) = experiment.build(exp, found)
    assert inst.id == "edge_finder" and inst.strategy.series == ("kalshi:KXBTC15M",)
    feeds = (proj / "feeds.toml").read_text()
    assert "[feeds.kalshi]" in feeds and 'slug_prefix = "cpc-btc-updown-15m"' in feeds


def test_init_refuses_to_overwrite_and_checks_names(tmp_path: Path) -> None:
    create(tmp_path, Answers(), vendor=False)
    with pytest.raises(ValueError, match="already has"):
        create(tmp_path, Answers(), vendor=False)
    create(tmp_path, Answers(), force=True, vendor=False)
    with pytest.raises(ValueError, match="module name"):
        create(tmp_path / "x", Answers(strategy="My Strategy"), vendor=False)


def test_init_command_without_questions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUANTY_CONFIG", "/nonexistent")
    res = runner.invoke(
        cli.app, ["init", str(tmp_path / "q"), "--yes", "--strategy", "s1", "--venue", "polymarket_us"]
    )
    assert res.exit_code == 0, res.output
    assert (tmp_path / "q" / "strategies" / "s1.py").exists()
    assert "[feeds.polymarket_us]" in (tmp_path / "q" / "feeds.toml").read_text()
    bad = runner.invoke(cli.app, ["init", str(tmp_path / "r"), "--yes", "--venue", "nasdaq"])
    assert bad.exit_code == 2 and "unknown venue" in bad.stderr
