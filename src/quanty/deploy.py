"""`quanty deploy you@server`: copy a project to a Docker server, check it there, and (re)start it.

The same command for the first deploy and every update. It runs, in order:

1. `ssh` to make the project folder on the server
2. `rsync` of the project, never .env, data/, .venv/ or .git/
3. on the server: stop unless the server has its keys (.env, put there by `quanty keys`), record what
   was sent (the git commit, and whether it had uncommitted changes) in .quanty-deployed, build the
   image, `quanty doctor` in it (stops here if anything fails), then `docker compose up -d`, which
   restarts the trader and the dashboard on the new code

data/ on the server (recordings, ledgers, the live order journal) and its .env are never sent or
deleted: keys travel apart from code, once, with `quanty keys you@server --file .env.server`.
"""

import json
import shlex
from pathlib import Path
from typing import Any

from quanty.version import DEPLOYED_MARK

EXCLUDE = ("data/", ".venv/", ".git/", "__pycache__/", ".DS_Store", ".env", ".env.*", DEPLOYED_MARK)
KEY_NAMES = ("KALSHI_KEY_ID", "KALSHI_SECRET_KEY", "POLYMARKET_KEY_ID", "POLYMARKET_SECRET_KEY")


def commands(
    host: str, project: Path, remote_dir: str, check: bool = True, stamp: dict[str, Any] | None = None
) -> list[list[str]]:
    """stamp: what's being sent (quanty.version.git), kept on the server for the runs to record."""
    for need in ("compose.yaml", "Dockerfile"):
        if not (project / need).exists():
            raise ValueError(f"{project.resolve()} has no {need}: run this in a folder from `quanty init`")
    d = shlex.quote(remote_dir)
    rsync = ["rsync", "-az", "--delete", *(f"--exclude={x}" for x in EXCLUDE)]
    # the marker: sessions here keep dashboard changes with the run (the next deploy replaces these
    # files); `quanty pull` and `quanty deploy` bring them into the local experiment files. It holds
    # the commit sent, which each run there records (the image has no .git)
    mark = shlex.quote(json.dumps(stamp or {}, separators=(",", ":")))
    no_keys = (
        f"no .env on the server: send its keys first (quanty keys HOST --file .env.server{_to(remote_dir)})"
    )
    steps = [
        f"cd {d}",
        f"{{ test -f .env || {{ echo {shlex.quote(no_keys)} >&2; exit 4; }}; }}",
        f"printf '%s\\n' {mark} > {DEPLOYED_MARK}",
        "docker compose build",
    ]
    if check:
        steps.append("docker compose run --rm --entrypoint quanty trader doctor")
    steps += ["docker compose up -d", "docker compose ps"]
    return [
        ["ssh", host, f"mkdir -p {d}"],
        [*rsync, f"{project.resolve()}/", f"{host}:{remote_dir.rstrip('/')}/"],
        ["ssh", host, " && ".join(steps)],
    ]


def _to(remote_dir: str) -> str:
    return "" if "/" not in remote_dir and not remote_dir.startswith("~") else f" --to {remote_dir}"


def check_keys(path: Path) -> list[str]:
    """The names a keys file sets (never their values): API keys and settings like QUANTY_UI_PASSWORD;
    refuses a file with none of the API keys."""
    names = []
    for line in path.read_text().splitlines():
        k, sep, v = line.partition("=")
        if sep and not k.lstrip().startswith("#") and k.strip() and v.strip():
            names.append(k.strip())
            if "$" in v and not (v.strip().startswith("'") and v.strip().endswith("'")):
                raise ValueError(
                    f"{path}: {k.strip()} contains `$`, which Docker Compose would substitute as a variable "
                    "(a QUANTY_UI_PASSWORD hash ends up blank): put the value in single quotes"
                )
    if not set(names) & set(KEY_NAMES):
        raise ValueError(f"{path} sets none of {', '.join(KEY_NAMES)}")
    return names


def keys_command(host: str, remote_dir: str) -> list[str]:
    """ssh that writes stdin as the server's .env (owner-only, replaced atomically), then recreates
    the containers if they're running, since they read .env when created."""
    d = shlex.quote(remote_dir)
    script = (
        f"mkdir -p {d} && cd {d} && umask 077 && cat > .env.new && chmod 600 .env.new && mv .env.new .env"
        ' && if test -f compose.yaml && test -n "$(docker compose ps -q 2>/dev/null)";'
        " then docker compose up -d --force-recreate; fi"
    )
    return ["ssh", host, script]
