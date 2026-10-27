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


def test_a_projects_own_strategies_shadow_config_paths(tmp_path: Path) -> None:
    local, extra = tmp_path / "strategies", tmp_path / "extra"
    local.mkdir()
    extra.mkdir()
    (local / "s.py").write_text("X = 1\n")
    (extra / "s.py").write_text("X = 2\n")
    assert plugins.discover([extra], include_examples=False, local=local)["s"].X == 1  # what it deploys


def test_strategies_import_helpers_beside_them_the_projects_first(tmp_path: Path) -> None:
    project, other = tmp_path / "proj", tmp_path / "other"
    for root, value in ((project, "here"), (other, "elsewhere")):
        (root / "beside_pkg").mkdir(parents=True)
        (root / "beside_pkg" / "__init__.py").write_text(f"WHERE = {value!r}\n")
    (project / "strategies").mkdir()
    (project / "strategies" / "s.py").write_text("from beside_pkg import WHERE\n")
    got = plugins.discover([], include_examples=False, local=project / "strategies", python_paths=[other])
    assert got["s"].WHERE == "here"


def test_missing_local_folder_is_fine(tmp_path: Path) -> None:
    assert plugins.discover([], include_examples=False, local=tmp_path / "none") == {}


def test_the_template_ships_in_the_package_and_is_not_a_strategy() -> None:
    assert (plugins.EXAMPLES / "_template.py").is_file()
    assert "_template" not in plugins.discover([], include_examples=True, local=None)


def test_python_paths_make_helper_packages_importable(tmp_path: Path) -> None:
    (tmp_path / "helpers_pkg").mkdir()
    (tmp_path / "helpers_pkg" / "__init__.py").write_text("VALUE = 42\n")
    strategies = tmp_path / "strats"
    strategies.mkdir()
    (strategies / "uses_helper.py").write_text("from helpers_pkg import VALUE\n")
    found = plugins.discover([strategies], include_examples=False, local=None, python_paths=[tmp_path])
    assert found["uses_helper"].VALUE == 42
