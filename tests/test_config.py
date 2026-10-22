from pathlib import Path

import pytest

from quanty import config


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    cfg = config.load(tmp_path / "nope.toml")
    assert cfg.strategy_paths == []
    assert cfg.source is None


def test_relative_paths_resolve_against_config_dir(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('strategy_paths = ["strats", "/abs/elsewhere"]\n')
    cfg = config.load(f)
    assert cfg.strategy_paths == [(tmp_path / "strats").resolve(), Path("/abs/elsewhere")]


def test_env_var_selects_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "c.toml"
    f.write_text('strategy_paths = ["x"]\n')
    monkeypatch.setenv("QUANTY_CONFIG", str(f))
    assert config.load().source == f


def test_rejects_non_list(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text('strategy_paths = "strats"\n')
    with pytest.raises(ValueError):
        config.load(f)
