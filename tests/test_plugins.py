from pathlib import Path

import pytest

from quanty import plugins


def test_examples_are_discovered() -> None:
    assert "example_favourite" in plugins.discover([])


def test_user_paths_add_and_shadow(tmp_path: Path) -> None:
    (tmp_path / "mine.py").write_text("X = 1\n")
    (tmp_path / "example_favourite.py").write_text("X = 2\n")
    (tmp_path / "_helper.py").write_text("raise RuntimeError\n")
    found = plugins.discover([tmp_path])
    assert found["mine"].X == 1
    assert found["example_favourite"].X == 2
    assert "_helper" not in found


def test_examples_can_be_excluded(tmp_path: Path) -> None:
    assert plugins.discover([tmp_path], include_examples=False) == {}


def test_missing_path_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        plugins.discover([tmp_path / "missing"])
