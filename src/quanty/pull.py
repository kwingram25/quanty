"""`quanty pull you@server`: bring the dashboard changes a server is holding into the local
experiment files, so the repo stays the source of truth (`quanty deploy` does it first).

A session on a server (in a container, or in a folder `quanty deploy` put there) never writes its
experiment file, because the next deploy replaces it: param edits and switches to live or paper are
kept with the run in data/control.sqlite. This reads them there (`quanty changes --json`, over ssh)
and writes them here the way Save to file and `quanty promote` would. Nothing is cleared on the
server: once the deployed file says the same, the next session drops what it no longer needs.

With --recordings it also copies the server's recent recordings into data/ here (read-only on the
server; nothing here is deleted), so a new strategy can be backtested on real markets before it's
deployed: `quanty backtest experiments/main.toml --data data`.
"""

import json
import shlex
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any

from quanty import experiment, promote


def fetch(host: str, remote_dir: str) -> dict[str, dict[str, Any]]:
    """The server's held changes, per run ({} when there's no deployed project there yet)."""
    d = shlex.quote(remote_dir)
    cmd = (
        f"cd {d} 2>/dev/null || exit 3; docker compose run --rm -T --entrypoint quanty trader changes --json"
    )
    done = subprocess.run(["ssh", host, cmd], capture_output=True, text=True)
    if done.returncode == 3:
        return {}
    if done.returncode != 0:
        raise ValueError(
            f"couldn't read the server's changes: {done.stderr.strip()[-300:] or done.returncode}"
        )
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    return json.loads(lines[-1]) if lines else {}


def apply(project: Path, changes: dict[str, dict[str, Any]]) -> list[str]:
    """Write each run's held changes into experiments/<name>.toml; returns what it did, and what it
    couldn't (an instance or broker the local file no longer has)."""
    out = []
    for run, held in sorted(changes.items()):
        path = project / "experiments" / f"{run.split('/', 1)[1]}.toml"
        if not path.exists():
            out.append(f"{run}: no {path.relative_to(project)} here; its changes stay on the server")
            continue
        raw = tomllib.loads(path.read_text())
        ids = {i.get("id") for i in raw.get("instances", [])}
        brokers = experiment.load(path).brokers
        for iid, params in sorted(held.get("params", {}).items()):
            if iid not in ids:
                out.append(f"{iid}: not in {path.name} (a grid or removed?); edit by hand: {params}")
                continue
            experiment.save_params(path, iid, params)
            out.append(f"{iid}: " + ", ".join(f"{k} = {v}" for k, v in params.items()))
        for iid, sw in sorted(held.get("switches", {}).items()):
            name = sw["broker"]
            if iid not in ids or name not in brokers:
                out.append(f"{iid}: switched to {name} on the server, but {path.name} lacks it; not applied")
                continue
            try:
                done = promote.promote(
                    path, iid, brokers[name].kind, broker=name, twin=sw.get("twin") is not None
                )
            except ValueError as exc:
                if "already runs on" in str(exc):
                    continue
                raise
            out.append(
                f"{iid}: broker {done.from_broker} → {name} ({done.to_kind})"
                + (f", paper twin {done.twin_added}" if done.twin_added else "")
            )
    return out


LEDGERS = ("session", "backtest", "paper", "live")  # data/ folders that hold runs, not recordings


def recordings_command(
    host: str, remote_dir: str, data: Path, hours: float, now_ms: int | None = None
) -> list[str]:
    """rsync of the server's last `hours` of recordings (every feed's hourly files) into data/."""
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    first = (now - int(hours * 3_600_000)) // 3_600_000 * 3_600_000
    wanted = sorted(
        {
            time.strftime("*/%Y-%m-%d/%H.jsonl.gz", time.gmtime(t / 1000))
            for t in range(first, now + 1, 3_600_000)
        }
    )
    return [
        "rsync",
        "-az",
        "--prune-empty-dirs",
        *(f"--exclude=/{d}/" for d in LEDGERS),
        "--include=*/",
        *(f"--include={w}" for w in wanted),
        "--exclude=*",
        f"{host}:{remote_dir.rstrip('/')}/data/",
        f"{data}/",
    ]
