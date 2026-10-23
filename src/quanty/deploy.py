"""`quanty deploy you@server`: copy a project to a Docker server, check it there, and (re)start it.

The same command for the first deploy and every update. It runs, in order:

1. `ssh` to make the project folder on the server
2. `rsync` of the project, with .env (kept owner-only) but never data/, .venv/ or .git/
3. on the server: build the image, `quanty doctor` in it (stops here if anything fails), then
   `docker compose up -d`, which restarts the trader and the dashboard on the new code

data/ on the server (recordings, ledgers, the live order journal) is never sent or deleted.
"""

import shlex
from pathlib import Path

EXCLUDE = ("data/", ".venv/", ".git/", "__pycache__/", ".DS_Store")


def commands(host: str, project: Path, remote_dir: str, check: bool = True) -> list[list[str]]:
    for need in ("compose.yaml", "Dockerfile", ".env"):
        if not (project / need).exists():
            raise ValueError(f"{project.resolve()} has no {need}: run this in a folder from `quanty init`")
    d = shlex.quote(remote_dir)
    rsync = ["rsync", "-az", "--delete", *(f"--exclude={x}" for x in EXCLUDE)]
    steps = [f"cd {d}", "docker compose build"]
    if check:
        steps.append("docker compose run --rm --entrypoint quanty trader doctor")
    steps += ["docker compose up -d", "docker compose ps"]
    return [
        ["ssh", host, f"mkdir -p {d}"],
        [*rsync, f"{project.resolve()}/", f"{host}:{remote_dir.rstrip('/')}/"],
        ["ssh", host, " && ".join(steps)],
    ]
