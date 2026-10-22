from collections import Counter
from pathlib import Path

import pytest

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


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = cli.main(list(argv))
    return code, capsys.readouterr().out


def test_demo_is_reproducible(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUANTY_CONFIG", "/nonexistent")
    code, out = run(capsys, "demo", "--hours", "1", "--seed", "2")
    assert code == 0 and "example_favourite" in out
    assert run(capsys, "demo", "--hours", "1", "--seed", "2")[1] == out


def test_demo_param_override_and_errors(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUANTY_CONFIG", "/nonexistent")
    assert run(capsys, "demo", "--hours", "0.5", "--param", "min_conf=0.99")[0] == 0
    assert cli.main(["demo", "--param", "nope=1"]) == 2
    assert cli.main(["demo", "--param", "missing-equals"]) == 2
    assert "unknown field" in capsys.readouterr().err


def test_backtest_writes_ledger(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("QUANTY_CONFIG", "/nonexistent")
    db = tmp_path / "run.sqlite"
    code, out = run(
        capsys, "backtest", str(ROOT / "experiments" / "example.toml"), "--hours", "1", "--ledger", str(db)
    )
    assert code == 0 and "fav_90" in out and "fav_97" in out
    assert db.exists()
    assert cli.main(["backtest", str(ROOT / "experiments" / "example.toml"), "--ledger", str(db)]) == 2


def test_list(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUANTY_CONFIG", "/nonexistent")
    code, out = run(capsys, "list")
    assert code == 0 and "example_favourite" in out
