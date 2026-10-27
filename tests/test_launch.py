"""An installed `quanty` hands a project's commands to the project's own quanty (uv run)."""

from pathlib import Path

import pytest

from quanty import launch
from quanty.init import Answers, create

INSTALLED = Path("/opt/tools/quanty-py/lib/python3.12/site-packages/quanty")  # not a checkout


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    create(tmp_path / "bot", Answers(), vendor=False)
    (tmp_path / "bot" / "strategies" / "deep").mkdir()
    return tmp_path / "bot"


def test_a_project_runs_its_own_quanty(project: Path) -> None:
    tool = Path("/opt/tools/quanty-py")
    got = launch.handover(project / "strategies" / "deep", tool, {}, INSTALLED)  # from a subfolder too
    assert got == ["/usr/bin/uv", "run", "--project", str(project), "quanty"]


def test_runs_itself_when_it_is_the_projects_or_outside_one(project: Path, tmp_path: Path) -> None:
    assert launch.handover(project, project / ".venv", {}, INSTALLED) is None  # already the project's
    assert launch.handover(tmp_path, Path("/opt/x"), {}, INSTALLED) is None  # no project here
    assert launch.handover(project, Path("/opt/x"), {launch.HANDED: "1"}, INSTALLED) is None  # once
    assert launch.handover(project, Path("/opt/x"), {"QUANTY_IN_IMAGE": "1"}, INSTALLED) is None
    checkout = Path(__file__).parents[1] / "src" / "quanty"  # development, vendored upgrades
    assert launch.handover(project, Path("/opt/x"), {}, checkout) is None


def test_no_uv_runs_itself(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert launch.handover(project, Path("/opt/x"), {}, INSTALLED) is None


def test_only_projects_that_depend_on_quanty(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = ["quanty_py[ui]>=0.1"]\n')
    assert launch.project_root(tmp_path) == tmp_path
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "pyproject.toml").write_text('[project]\nname = "y"\ndependencies = ["quantyx"]\n')
    assert launch.project_root(tmp_path / "other") is None  # the nearest pyproject.toml decides
    (tmp_path / "pyproject.toml").write_text("not toml [")
    assert launch.project_root(tmp_path) is None
