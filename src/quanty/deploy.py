"""`quanty deploy you@server`: copy a project to a Docker server, check it there, and (re)start it.

The same command for the first deploy and every update. It runs, in order:

1. `ssh` to make the project folder on the server
2. `rsync` of the project, with .env (kept owner-only) but never data/, .venv/ or .git/
3. on the server: record what was sent (the git commit, and whether it had uncommitted changes) in
   .quanty-deployed, build the image, `quanty doctor` in it (stops here if anything fails), then
   `docker compose up -d`, which restarts the trader and the dashboard on the new code

data/ on the server (recordings, ledgers, the live order journal) is never sent or deleted.
"""

import json
import shlex
from pathlib import Path
from typing import Any

from quanty.version import DEPLOYED_MARK

EXCLUDE = ("data/", ".venv/", ".git/", "__pycache__/", ".DS_Store", DEPLOYED_MARK)


def commands(
    host: str, project: Path, remote_dir: str, check: bool = True, stamp: dict[str, Any] | None = None
) -> list[list[str]]:
    """stamp: what's being sent (quanty.version.git), kept on the server for the runs to record."""
    for need in ("compose.yaml", "Dockerfile", ".env"):
        if not (project / need).exists():
            raise ValueError(f"{project.resolve()} has no {need}: run this in a folder from `quanty init`")
    d = shlex.quote(remote_dir)
    rsync = ["rsync", "-az", "--delete", *(f"--exclude={x}" for x in EXCLUDE)]
    # the marker: sessions here keep dashboard changes with the run (the next deploy replaces these
    # files); `quanty pull` and `quanty deploy` bring them into the local experiment files. It holds
    # the commit sent, which each run there records (the image has no .git)
    mark = shlex.quote(json.dumps(stamp or {}, separators=(",", ":")))
    steps = [f"cd {d}", f"printf '%s\\n' {mark} > {DEPLOYED_MARK}", "docker compose build"]
    if check:
        steps.append("docker compose run --rm --entrypoint quanty trader doctor")
    steps += ["docker compose up -d", "docker compose ps"]
    return [
        ["ssh", host, f"mkdir -p {d}"],
        [*rsync, f"{project.resolve()}/", f"{host}:{remote_dir.rstrip('/')}/"],
        ["ssh", host, " && ".join(steps)],
    ]
