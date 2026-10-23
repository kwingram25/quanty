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
    assert first.exit_code == 0 and "example_favorite" in first.stdout
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
    assert "example_favorite" in run("list").stdout
    res = run("params", "example_favorite")
    assert res.exit_code == 0 and "min_conf" in res.stdout and "Model probability" in res.stdout
    assert run("params", "nope").exit_code == 2


def test_live_brokers_are_gated(tmp_path: Path) -> None:
    exp = tmp_path / "live.toml"
    exp.write_text(
        '[brokers.real]\nkind = "live"\nmax_qty = 10\nmax_notional = 5\n\n'
        '[[instances]]\nid = "a"\nstrategy = "example_favorite"\nbroker = "real"\n'
    )
    feeds = tmp_path / "feeds.toml"
    feeds.write_text('[feeds.coinbase]\nkind = "coinbase"\n')
    res = run("paper", str(exp), "--feeds", str(feeds), "--data", str(tmp_path))
    assert res.exit_code == 2 and "are live: use quanty live --arm" in res.stderr
    res = run("live", str(exp), "--feeds", str(feeds), "--data", str(tmp_path))
    assert res.exit_code == 2 and "pass --arm" in res.stderr
    res = run("backtest", str(exp), "--hours", "0.5")
    assert res.exit_code == 2 and "backtests need sim or shadow brokers" in res.stderr
    missing = tmp_path / "nocap.toml"
    missing.write_text(exp.read_text().replace("max_notional = 5\n", ""))
    assert "max_notional" in run("backtest", str(missing)).stderr


def test_backtest_ledger_folder_is_created(tmp_path: Path) -> None:
    db = tmp_path / "data" / "backtest" / "run.sqlite"
    assert (
        run(
            "backtest", str(ROOT / "experiments" / "example.toml"), "--hours", "0.5", "--ledger", str(db)
        ).exit_code
        == 0
    )
    assert db.exists()


def test_demo_ui_refuses_a_custom_ledger(tmp_path: Path) -> None:
    res = run("demo", "--ui", "--ledger", str(tmp_path / "x.sqlite"))
    assert res.exit_code == 2 and "leave out --ledger" in res.stderr


def test_live_demo_plays_and_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tempfile.mkdtemp", lambda prefix="": str(tmp_path))
    res = run("demo", "--live", "--no-browser", "--port", "0", "--hours", "0.05", "--history", "1",
              "--speed", "500")  # fmt: skip
    assert res.exit_code == 0, res.output
    from quanty.core import Ledger

    ledger = Ledger(tmp_path / "paper" / "demo-live.sqlite")
    assert sum(s.fills for s in ledger.summary()) > 0
    ledger.close()
