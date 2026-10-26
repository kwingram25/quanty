"""Which code a run ran: its git commit, or what `quanty deploy` sent; each strategy file's hash."""

import json
import subprocess
from pathlib import Path

import pytest
from helpers import Hooks

import quanty
from quanty import version
from quanty.brokers.sim import SimBroker
from quanty.core import Engine, Instance
from quanty.db.models import LogRow
from quanty.init import Answers, create, git_init


@pytest.fixture(autouse=True)
def identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@example.com")


def test_init_makes_a_repository_that_keeps_the_keys_out(tmp_path: Path) -> None:
    dest = tmp_path / "bot"
    create(dest, Answers(), vendor=False)
    assert "first commit" in git_init(dest)
    tracked = subprocess.run(
        ["git", "-C", str(dest), "ls-files"], capture_output=True, text=True
    ).stdout.split()
    assert "experiments/main.toml" in tracked and ".env" not in tracked
    got = version.git(dest)
    assert got is not None and len(got["commit"]) == 12 and not got["dirty"]
    (dest / "experiments" / "main.toml").write_text("# edited\n")
    assert version.git(dest)["dirty"] is True  # type: ignore[index]
    assert "already inside" in git_init(dest / "strategies")  # never a repository within one


def test_a_server_without_git_reads_what_deploy_sent(tmp_path: Path) -> None:
    (tmp_path / version.DEPLOYED_MARK).write_text(json.dumps({"commit": "abc123", "dirty": True}))
    assert version.project(tmp_path) == {
        "quanty": quanty.__version__,
        "commit": "abc123",
        "dirty": True,
    }
    assert version.project(tmp_path / "nowhere") == {"quanty": quanty.__version__}


def test_each_strategy_records_the_code_it_ran(tmp_path: Path) -> None:
    code = {"commit": "abc123", "dirty": False}
    engine = Engine([Instance("a", Hooks(), SimBroker("sim"))], clock_ms=None, code=code)
    row = engine.ledger.execute(LogRow.__table__.select().where(LogRow.msg == "strategy")).first()
    assert row is not None
    recorded = row.fields["code"]
    assert recorded["commit"] == "abc123" and len(recorded["file"]) == 12  # the strategy file's hash
