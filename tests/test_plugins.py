from pathlib import Path

import pytest

from quanty import plugins


def test_examples_are_discovered(tmp_path: Path) -> None:
    assert "example_favorite" in plugins.discover([], local=tmp_path)


def test_user_paths_add_and_shadow(tmp_path: Path) -> None:
    (tmp_path / "mine.py").write_text("X = 1\n")
    (tmp_path / "example_favorite.py").write_text("X = 2\n")
    (tmp_path / "_helper.py").write_text("raise RuntimeError\n")
    found = plugins.discover([tmp_path], local=None)
    assert found["mine"].X == 1
    assert found["example_favorite"].X == 2
    assert "_helper" not in found


def test_examples_can_be_excluded(tmp_path: Path) -> None:
    assert plugins.discover([tmp_path], include_examples=False, local=None) == {}


def test_missing_path_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        plugins.discover([tmp_path / "missing"], local=None)


def test_local_folder_and_nested_repo(tmp_path: Path) -> None:
    local = tmp_path / "strategies"
    (local / "private").mkdir(parents=True)
    (local / ".git").mkdir()
    (local / "_template.py").write_text("raise RuntimeError\n")
    (local / "mine.py").write_text("X = 'local'\n")
    (local / "private" / "secret.py").write_text("X = 'nested'\n")
    (local / "private" / "mine.py").write_text("X = 'nested wins'\n")
    found = plugins.discover([], include_examples=False, local=local)
    assert set(found) == {"mine", "secret"}
    assert found["mine"].X == "nested wins"
    assert found["secret"].X == "nested"


def test_config_paths_shadow_local(tmp_path: Path) -> None:
    local, extra = tmp_path / "strategies", tmp_path / "extra"
    local.mkdir()
    extra.mkdir()
    (local / "s.py").write_text("X = 1\n")
    (extra / "s.py").write_text("X = 2\n")
    assert plugins.discover([extra], include_examples=False, local=local)["s"].X == 2


def test_missing_local_folder_is_fine(tmp_path: Path) -> None:
    assert plugins.discover([], include_examples=False, local=tmp_path / "none") == {}


def test_repo_template_is_not_a_strategy() -> None:
    repo_local = Path(__file__).parents[1] / "strategies"
    assert "_template" not in plugins.discover([], include_examples=False, local=repo_local)


def test_python_paths_make_helper_packages_importable(tmp_path: Path) -> None:
    (tmp_path / "helpers_pkg").mkdir()
    (tmp_path / "helpers_pkg" / "__init__.py").write_text("VALUE = 42\n")
    strategies = tmp_path / "strats"
    strategies.mkdir()
    (strategies / "uses_helper.py").write_text("from helpers_pkg import VALUE\n")
    found = plugins.discover([strategies], include_examples=False, local=None, python_paths=[tmp_path])
    assert found["uses_helper"].VALUE == 42
