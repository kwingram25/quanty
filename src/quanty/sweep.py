"""Backtests started from the dashboard's Variants tab: one strategy of a run, as it runs now and
over a grid of its params, replayed over the last hours of recordings, to rank beside it.

The dashboard writes a small experiment (data/backtest/<name>.toml: the strategy with its current
params, dashboard edits included, as a baseline, plus a `grid` entry, all on one simulated broker)
and runs `quanty backtest` on it in the background, niced, one at a time. The ledger appears as a
backtest run (data/backtest/<name>.sqlite) once it has finished; the .toml stays beside it, so the
same backtest runs again by hand. Nothing here touches the session or its experiment file.
"""

import itertools
import os
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tomlkit

from quanty import experiment
from quanty.control import ControlStore
from quanty.feeds.recording import recorded_feeds

MAX_VARIANTS = 50
MAX_HOURS = 24 * 14
SESSION_MODES = ("session", "live", "paper")  # runs that have an experiment file behind them


@dataclass
class Job:
    run: str  # the run it came from
    instance: str
    name: str  # the backtest run it makes: backtest/<name>
    variants: int
    hours: float
    started: float
    finished: float | None = None
    error: str | None = None
    proc: subprocess.Popen[bytes] | None = field(default=None, repr=False)

    @property
    def state(self) -> str:
        if self.finished is None:
            return "running"
        return "failed" if self.error else "done"

    def public(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "run": self.run,
            "instance": self.instance,
            "result": f"backtest/{self.name}",
            "variants": self.variants,
            "hours": self.hours,
            "started": self.started,
            "finished": self.finished,
            "error": self.error,
        }


def plan(
    project: Path, data: Path, run: str, instance: str, grid: dict[str, list[Any]], hours: float
) -> dict[str, Any]:
    """The experiment for a backtest of `instance` from `run`: it as it runs now (`<instance>`), and
    one copy per combination of `grid` (`<instance>_<values>`), on one simulated broker."""
    mode, _, stem = run.partition("/")
    if mode not in SESSION_MODES:
        raise ValueError("backtests start from a paper or live run's strategies")
    path = project / "experiments" / f"{stem}.toml"
    if not path.exists():
        raise ValueError(f"no experiments/{stem}.toml in {project}")
    if not 0 < hours <= MAX_HOURS:
        raise ValueError(f"hours must be above 0 and at most {MAX_HOURS}")
    grid = {k: v for k, v in grid.items() if v}
    if not grid:
        raise ValueError("give at least one param a list of values to try")
    count = len(list(itertools.product(*grid.values())))
    if count > MAX_VARIANTS:
        raise ValueError(f"{count} combinations; at most {MAX_VARIANTS} at once")
    if not recorded_feeds(data):
        raise ValueError(f"no recordings in {data}/ to replay (paper and live sessions record them)")
    exp = experiment.load(path)
    spec = next((i for i in exp.instances if i.id == instance), None)
    if spec is None:
        raise ValueError(f"{path.name} has no instance {instance!r}")
    params = dict(spec.params)
    control = data / "control.sqlite"
    if control.exists():
        store = ControlStore(control, create=False)
        try:
            params.update(store.held_changes("").get(run, {}).get("params", {}).get(instance, {}))
        finally:
            store.close()
    raw = tomllib.loads(path.read_text())
    src = exp.brokers[exp.broker_of(spec)]
    broker: dict[str, Any] = {"kind": "sim"}
    if src.kind != "live":
        broker["latency_ms"] = src.model_dump()["latency_ms"]
    entry: dict[str, Any] = {"id": instance, "strategy": spec.strategy, "broker": "sim"}
    if spec.series:
        entry["series"] = list(spec.series)
    if params:
        entry["params"] = params
    if limits := spec.limits.model_dump(exclude_defaults=True):
        entry["limits"] = limits
    out = {k: v for k, v in raw.items() if k not in ("brokers", "instances", "warmup_s")}
    out["brokers"] = {"sim": broker}
    out["instances"] = [entry, {**entry, "grid": grid}]
    experiment.parse(out, source="backtest")  # it loads, or nothing runs
    return out


class Runner:
    """One backtest at a time, in the background; the last one's outcome is kept until the next."""

    def __init__(self, project: Path, data: Path) -> None:
        self.project, self.data = project, data
        self.job: Job | None = None
        self._lock = threading.Lock()

    def start(self, run: str, instance: str, grid: dict[str, list[Any]], hours: float) -> Job:
        with self._lock:
            if self.job is not None and self.job.state == "running":
                raise RuntimeError(f"a backtest of {self.job.instance} is running; wait for it or stop it")
            exp = plan(self.project, self.data, run, instance, grid, hours)
            now = time.time()
            stamp = datetime.fromtimestamp(now, UTC).strftime("%Y%m%d-%H%M%S")
            name = f"{run.partition('/')[2]}-{instance}-{stamp}"
            folder = self.data / "backtest"
            folder.mkdir(parents=True, exist_ok=True)
            config = folder / f"{name}.toml"
            config.write_text(
                f"# Backtest of {instance} from {run}, started on the dashboard at {stamp} UTC:\n"
                f"# quanty backtest {config} --data {self.data} --start <UTC start>\n" + tomlkit.dumps(exp)
            )
            part = folder / f"{name}.sqlite.part"
            log = folder / f".{name}.log"
            start = datetime.fromtimestamp(now - hours * 3600, UTC).isoformat(timespec="seconds")
            cmd = [sys.executable, "-m", "quanty", "backtest", str(config), "--data", str(self.data)]
            cmd += ["--start", start, "--ledger", str(part)]
            if shutil.which("nice"):
                cmd = ["nice", "-n", "10", *cmd]  # a server's trader comes first
            variants = len(list(itertools.product(*(v for v in grid.values() if v)))) + 1
            job = Job(run, instance, name, variants, hours, now)
            with log.open("wb") as out:
                job.proc = subprocess.Popen(
                    cmd, cwd=self.project, stdout=out, stderr=subprocess.STDOUT, env={**os.environ}
                )
            self.job = job
            threading.Thread(target=self._finish, args=(job, part, log), daemon=True).start()
            return job

    def _finish(self, job: Job, part: Path, log: Path) -> None:
        assert job.proc is not None
        code = job.proc.wait()
        if code == 0 and part.exists():
            part.replace(part.with_suffix(""))  # <name>.sqlite: now the dashboard lists it
        else:
            tail = log.read_text(errors="replace").strip().splitlines()[-6:] if log.exists() else []
            job.error = "\n".join(tail) or ("stopped" if code < 0 else f"exited {code}")
            part.unlink(missing_ok=True)
        log.unlink(missing_ok=True)
        job.finished = time.time()

    def stop(self) -> bool:
        job = self.job
        if job is None or job.state != "running" or job.proc is None:
            return False
        job.proc.terminate()
        return True

    def status(self) -> dict[str, Any] | None:
        return self.job.public() if self.job else None
