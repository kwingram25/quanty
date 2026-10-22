from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from quanty import cli, synthetic
from quanty.core import MarketOpen, Settled

ROOT = Path(__file__).parents[1]


def test_synthetic_stream_is_ordered_and_settles_every_window() -> None:
    evs = list(synthetic.events(synthetic.Spec(hours=1)))
    assert [e.ts for e in evs] == sorted(e.ts for e in evs)
    kinds = Counter(type(e) for e in evs)
    assert kinds[MarketOpen] == kinds[Settled] == 12


def test_synthetic_is_deterministic_per_seed() -> None:
    spec = synthetic.Spec(hours=0.2, seed=3)
    assert list(synthetic.events(spec)) == list(synthetic.events(spec))
    assert list(synthetic.events(spec)) != list(synthetic.events(synthetic.Spec(hours=0.2, seed=4)))


def test_p_up() -> None:
    assert synthetic.p_up(100, 100, 60, 0.001) == pytest.approx(0.5)
    assert synthetic.p_up(101, 100, 0, 0.001) == 1.0
    assert synthetic.p_up(99, 100, 0, 0.001) == 0.0


runner = CliRunner()


@pytest.fixture(autouse=True)
def no_user_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUANTY_CONFIG", "/nonexistent")


def run(*argv: str) -> Result:
    return runner.invoke(cli.app, list(argv))


def test_demo_is_reproducible() -> None:
    first = run("demo", "--hours", "1", "--seed", "2")
    assert first.exit_code == 0 and "example_favourite" in first.stdout
    assert run("demo", "--hours", "1", "--seed", "2").stdout == first.stdout


def test_demo_param_override_and_errors() -> None:
    assert run("demo", "--hours", "0.5", "--param", "min_conf=0.99").exit_code == 0
    bad = run("demo", "--param", "nope=1")
    assert bad.exit_code == 2 and "params.nope: Extra inputs are not permitted" in bad.stderr
    assert run("demo", "--param", "missing-equals").exit_code == 2


def test_backtest_writes_ledger(tmp_path: Path) -> None:
    db = tmp_path / "run.sqlite"
    example = str(ROOT / "experiments" / "example.toml")
    res = run("backtest", example, "--hours", "1", "--ledger", str(db))
    assert res.exit_code == 0 and "fav_90" in res.stdout and "fav_97" in res.stdout
    assert db.exists()
    again = run("backtest", example, "--ledger", str(db))
    assert again.exit_code == 2 and "already exists" in again.stderr


def test_list_and_params() -> None:
    assert "example_favourite" in run("list").stdout
    res = run("params", "example_favourite")
    assert res.exit_code == 0 and "min_conf" in res.stdout and "Model probability" in res.stdout
    assert run("params", "nope").exit_code == 2
