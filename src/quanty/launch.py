"""The `quanty` command's entry point. Inside a project from `quanty init`, an installed `quanty`
(`uv tool install quanty-py`) hands the command to the project's own quanty, `uv run quanty ...`:
the version its uv.lock pins, with the project's dependencies, the same as a server runs. Elsewhere,
and when it's already the project's, it runs itself. Kept apart from cli.py, which is slow to
import, so handing over costs little."""

import os
import shutil
import sys
import tomllib
from pathlib import Path

HANDED = "QUANTY_HANDED_OVER"  # set on the handover, so it happens at most once


def project_root(start: Path) -> Path | None:
    """The nearest folder at or above start whose pyproject.toml depends on quanty."""
    for d in (start, *start.parents):
        path = d / "pyproject.toml"
        if path.is_file():
            try:
                deps = tomllib.loads(path.read_text()).get("project", {}).get("dependencies", [])
            except (OSError, tomllib.TOMLDecodeError):
                return None
            names = {_name(str(dep)) for dep in deps}
            return d if names & {"quanty-py", "quanty"} else None
    return None


def _name(dep: str) -> str:
    for i, ch in enumerate(dep):
        if not (ch.isalnum() or ch in "-_."):
            return dep[:i].strip().lower().replace("_", "-")
    return dep.strip().lower().replace("_", "-")


def handover(cwd: Path, prefix: Path, env: dict[str, str], package: Path) -> list[str] | None:
    """The command to run instead (uv run in the project), or None to run here. package: this
    quanty's source folder (a checkout's src/quanty runs itself: development, vendored upgrades)."""
    if env.get(HANDED) or env.get("QUANTY_IN_IMAGE") or (package.parents[1] / "pyproject.toml").exists():
        return None
    root = project_root(cwd)
    if root is None or prefix.resolve() == (root / ".venv").resolve():
        return None
    uv = shutil.which("uv")
    if uv is None:
        return None
    return [uv, "run", "--project", str(root), "quanty"]


def main() -> None:
    command = handover(Path.cwd(), Path(sys.prefix), dict(os.environ), Path(__file__).resolve().parent)
    if command is not None:
        os.environ[HANDED] = "1"
        os.execv(command[0], [*command, *sys.argv[1:]])
    from quanty.cli import app

    app()
