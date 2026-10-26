"""Which code a run ran, recorded with each strategy when the run starts: the project's git commit
(on a server, where there's no .git, the one `quanty deploy` sent), whether it had uncommitted
changes, quanty's version, and a hash of the strategy's own file, which changes with any edit,
committed or not."""

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import quanty

DEPLOYED_MARK = ".quanty-deployed"  # written by `quanty deploy`: what it sent, as JSON


def git(root: Path) -> dict[str, Any] | None:
    """The commit checked out at root and whether files differ from it; None outside a git repo."""
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if head.returncode != 0:
            return None
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    changed = [line[3:] for line in status.stdout.splitlines() if line.strip()]
    return {"commit": head.stdout.strip(), "dirty": bool(changed), "changed": changed[:20]}


def project(root: Path) -> dict[str, Any]:
    """The project's code version: git where there's a repo, else what `quanty deploy` recorded."""
    out: dict[str, Any] = {"quanty": quanty.__version__}
    found = git(root)
    if found is None:
        try:
            found = json.loads((root / DEPLOYED_MARK).read_text() or "{}")
        except (OSError, ValueError):
            found = None
    if found:
        out.update(commit=found.get("commit"), dirty=bool(found.get("dirty")))
    return out


def file_hash(path: str | Path | None) -> str | None:
    """The first 12 hex digits of a file's SHA-256 (None when there's no file)."""
    if path is None:
        return None
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]
    except OSError:
        return None
