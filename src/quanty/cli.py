"""quanty: record, backtest, paper and live trade repeated prediction markets."""

import asyncio
import contextlib
import dataclasses
import json
import random
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import webbrowser
from collections.abc import Iterable, Iterator
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Annotated, Any

import structlog
import typer

from quanty import config, experiment, plugins, synthetic, version
from quanty.brokers.live import LiveBroker
from quanty.control import Controller, ControlStore, Playback
from quanty.core import Engine, Event, Ledger, Summary
from quanty.feeds.cli import (
    feeds_app,
    keep_days,
    load_feeds,
    print_windows,
    record,
    recorded,
    recorded_range,
    replay,
)
from quanty.feeds.health import Status, Window
from quanty.feeds.replay import Replay
from quanty.feeds.session import RecordSession, TaskCrashed

if TYPE_CHECKING:
    from quanty.doctor import Check

app = typer.Typer(help=__doc__, no_args_is_help=True, add_completion=False)
app.command()(record)
app.command()(replay)
app.add_typer(feeds_app, name="feeds")

Hours = Annotated[float, typer.Option(help="Synthetic hours to simulate")]
DEFAULT_HOURS = 6.0
Seed = Annotated[int, typer.Option(help="Random seed for the synthetic markets")]
LedgerPath = Annotated[Path | None, typer.Option("--ledger", help="Write the ledger to this new SQLite file")]


def _strategies() -> dict[str, ModuleType]:
    cfg = config.load()
    return plugins.discover(cfg.strategy_paths, python_paths=cfg.python_paths)


def _fail(exc: Exception) -> typer.Exit:
    typer.echo(f"quanty: {exc}", err=True)
    return typer.Exit(2)


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="Project folder to create")] = Path("my-quanty"),
    strategy: Annotated[str | None, typer.Option(help="Strategy name (a Python module name)")] = None,
    venue: Annotated[list[str] | None, typer.Option(help="kalshi and/or polymarket_us (repeat)")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Accept the defaults; ask nothing")] = False,
    force: Annotated[bool, typer.Option(help="Overwrite files that already exist")] = False,
    git: Annotated[bool, typer.Option(help="Make it a git repository with a first commit")] = True,
) -> None:
    """Create a project: a strategy skeleton, configs, API keys, and Docker files to deploy it, as a
    git repository of its own (each run records the commit it ran)."""
    from quanty import init as project

    a = project.Answers()
    try:
        a.strategy = project.check_name(
            strategy or (a.strategy if yes else typer.prompt("Strategy name", a.strategy))
        )
        if venue is not None:
            a.venues = venue
        elif not yes:
            raw = typer.prompt("Venues (kalshi, polymarket_us, or none)", "kalshi")
            a.venues = [] if raw.strip() == "none" else [v.strip() for v in raw.split(",") if v.strip()]
        unknown = set(a.venues) - set(project.DEFAULT_SERIES)
        if unknown:
            raise ValueError(f"unknown venue {', '.join(sorted(unknown))} (kalshi, polymarket_us)")
        if not yes:
            for v in a.venues:
                raw = typer.prompt(f"{v} series", ", ".join(project.DEFAULT_SERIES[v]))
                a.series[v] = [x.strip() for x in raw.split(",") if x.strip()]
            typer.echo("API keys (leave blank to fill in .env later):")
            if "kalshi" in a.venues:
                a.kalshi_key_id = typer.prompt("  Kalshi key id", "", show_default=False)
                a.kalshi_key_path = typer.prompt("  Kalshi private key file", "", show_default=False)
            if "polymarket_us" in a.venues:
                a.polymarket_key_id = typer.prompt("  Polymarket US key id", "", show_default=False)
                a.polymarket_secret = typer.prompt(
                    "  Polymarket US secret", "", hide_input=True, show_default=False
                )
            if a.venues:
                typer.echo("Live trading caps, the largest single order (leave blank to set them later):")
                qty = typer.prompt("  contracts", "", show_default=False)
                notional = typer.prompt("  dollars", "", show_default=False)
                if qty.strip() and notional.strip():
                    a.max_qty, a.max_notional = float(qty), float(notional)
                    if a.max_qty <= 0 or a.max_notional <= 0:
                        raise ValueError("caps must be above 0")
        written = project.create(path, a, force=force)
    except ValueError as exc:
        raise _fail(exc) from None
    typer.echo(f"created {path}/: " + ", ".join(written))
    if git:
        typer.echo(project.git_init(path))
    typer.echo(f"next: cd {path} && quanty list   (then see README.md)")


@app.command("list")
def list_() -> None:
    """List the strategies quanty can find."""
    for name, module in sorted(_strategies().items()):
        try:
            desc = experiment.strategy_class(module).description
        except ValueError as exc:
            desc = f"(invalid: {exc})"
        typer.echo(f"{name:28} {desc}")


@app.command()
def params(strategy: Annotated[str, typer.Argument(help="Strategy name, as `quanty list` shows it")]) -> None:
    """Show a strategy's parameters, defaults and descriptions."""
    try:
        module = _strategies()[strategy]
    except KeyError:
        raise _fail(ValueError(f"no strategy named {strategy!r}")) from None
    schema = experiment.strategy_class(module).params_type.model_json_schema()
    for name, prop in schema.get("properties", {}).items():
        kind = prop.get("type") or "|".join(p.get("type", "?") for p in prop.get("anyOf", []))
        typer.echo(f"{name:20} {kind:10} {prop.get('default')!s:>10}   {prop.get('description', '')}")


@app.command()
def demo(
    strategy: Annotated[str, typer.Option(help="Strategy to run")] = "example_favorite",
    param: Annotated[list[str] | None, typer.Option(help="Override a parameter: KEY=VALUE")] = None,
    latency_ms: Annotated[int, typer.Option(help="Simulated order latency")] = 50,
    hours: Hours = DEFAULT_HOURS,
    seed: Seed = 0,
    ledger: LedgerPath = None,
    ui: Annotated[bool, typer.Option("--ui", help="Then open the dashboard on the result")] = False,
    # not 8765, so a project's dashboard (and `quanty mcp`, which looks there) never finds the demo
    port: Annotated[int, typer.Option(help="Dashboard port, with --ui")] = 8764,
    browser: Annotated[bool, typer.Option(help="Open a browser, with --ui")] = True,
    live: Annotated[
        bool, typer.Option("--live", help="Play the markets in real time with the dashboard open (paper)")
    ] = False,
    speed: Annotated[float, typer.Option(help="With --live: market time per wall-clock second")] = 10.0,
    history: Annotated[float, typer.Option(help="With --live: hours already traded when it opens")] = 24.0,
    public: Annotated[
        bool,
        typer.Option(
            "--public",
            help="With --live: a hosted demo (all interfaces, a banner saying it's shared, starts over "
            "every --reset-hours by exiting, for the host to restart it)",
        ),
    ] = False,
    reset_hours: Annotated[float, typer.Option(help="With --public: hours until it starts over")] = 6.0,
) -> None:
    """Backtest one strategy on synthetic up/down markets.

    With --ui (and no --param), four variants of the example (one with a stop loss) run side by side
    and the dashboard opens on them: the whole loop with no keys and no setup.
    """
    ui = ui or live
    try:
        instances: list[dict[str, Any]] = [
            {"id": strategy, "strategy": strategy, "params": _params(param or [])}
        ]
        if ui and not param and strategy == "example_favorite":
            instances = [
                {"id": f"fav_{round(c * 100)}", "strategy": strategy, "params": {"min_conf": c}}
                for c in (0.90, 0.95, 0.97)
            ]  # and fav_90 with a model stop, to compare: fewer bad days, a lower average
            instances.insert(
                1, {"id": "fav_90_stop", "strategy": strategy, "params": {"min_conf": 0.90, "stop_conf": 0.7}}
            )
        if live and not param and strategy == "example_favorite":  # the live demo also has range markets
            instances.append({"id": "temp_buckets", "strategy": "example_bucket", "params": {}})
        for inst in instances:  # the demo trades 100 contracts a time, unless --param size= says otherwise
            if inst["strategy"] in ("example_favorite", "example_bucket"):
                inst["params"].setdefault("size", 100)
        if (own := _synthetic_series(strategy, live)) is not None:
            instances[0]["series"] = own
        raw = {"brokers": {"sim": {"kind": "sim", "latency_ms": latency_ms}}, "instances": instances}
        if ui and ledger is not None:
            raise ValueError("--ui keeps its ledger in a temporary demo folder; leave out --ledger")
        if speed <= 0:
            raise ValueError("--speed must be positive")
        data = Path(tempfile.mkdtemp(prefix="quanty-demo-")) if ui else None
        exp = experiment.parse(raw, source="demo")
        if live:
            assert data is not None
            start = _now_window_ms() - int(history * 3_600_000) // 300_000 * 300_000
            hours = hours if hours != DEFAULT_HOURS else 24 * 365  # runs until stopped unless --hours says
            spec = synthetic.Spec(hours=hours + history, seed=seed, start_ms=start)
            _demo_live(exp, data, spec, speed, port, browser and not public,
                       reset_s=reset_hours * 3600 if public else None)  # fmt: skip
            return
        if data is not None:
            ledger = data / "backtest" / f"demo-{hours:g}h-seed{seed}.sqlite"
            ledger.parent.mkdir(parents=True)
        _backtest(exp, hours, seed, ledger)
    except ValueError as exc:
        raise _fail(exc) from None
    if data is not None:
        import uvicorn

        from quanty.ui.app import create_app

        assert ledger is not None
        url = f"http://127.0.0.1:{port}/runs/backtest/{ledger.stem}"
        typer.echo(f"dashboard: {url} (demo data in {data}; Ctrl-C to stop)")
        if browser:
            threading.Timer(1.0, webbrowser.open, args=[url]).start()
        uvicorn.run(create_app(data), host="127.0.0.1", port=port, log_level="warning")


def _synthetic_series(strategy: str, live: bool) -> list[str] | None:
    """For a strategy written for a venue's series: the synthetic up/down series to try it on
    instead (None when it already trades them, or trades everything)."""
    module = _strategies().get(strategy)
    if module is None:
        return None  # reported by the experiment
    series = experiment.strategy_class(module).series
    synth = [f"{synthetic.VENUE}:updown_{w}" for w in (("5m", "15m", "1h") if live else ("5m",))]
    if not series or any(s in series for s in synth):
        return None
    typer.echo(f"note: {strategy} trades {', '.join(series)}; trying it on {', '.join(synth)}", err=True)
    return synth


@app.command()
def backtest(
    path: Annotated[Path, typer.Argument(help="Experiment config (TOML)", exists=True, dir_okay=False)],
    hours: Hours = 6.0,
    seed: Seed = 0,
    ledger: LedgerPath = None,
    data: Annotated[
        Path | None, typer.Option(help="Replay these recordings instead of synthetic markets")
    ] = None,
    start: Annotated[str | None, typer.Option(help="UTC start of the replay (with --data)")] = None,
    end: Annotated[str | None, typer.Option(help="UTC end of the replay (with --data)")] = None,
) -> None:
    """Run an experiment config on synthetic markets, or on recordings with --data."""
    try:
        _backtest(experiment.load(path), hours, seed, ledger, data, start, end)
    except ValueError as exc:
        raise _fail(exc) from None


FeedsOpt = Annotated[
    Path, typer.Option(help="Feed config (see feeds.example.toml)", exists=True, dir_okay=False)
]
DataOpt = Annotated[Path, typer.Option(help="Recordings and databases directory")]
LedgerOpt = Annotated[Path | None, typer.Option(help="Ledger (default: <data>/<mode>/<experiment>.sqlite)")]
DurationOpt = Annotated[float | None, typer.Option(help="Stop after this many seconds")]
WindowOpt = Annotated[float, typer.Option(help="Seconds between status reports")]
ExpArg = Annotated[Path, typer.Argument(help="Experiment config (TOML)", exists=True, dir_okay=False)]
UiOpt = Annotated[bool, typer.Option("--ui", help="Also serve the dashboard (a separate process)")]
UiHostOpt = Annotated[str, typer.Option(help="Dashboard interface, with --ui")]
UiPortOpt = Annotated[int, typer.Option(help="Dashboard port, with --ui")]
RecordOpt = Annotated[
    bool,
    typer.Option(
        "--record/--no-record",
        help="Record the feeds too; --no-record when a separate `quanty record` does (its own core)",
    ),
]


@app.command()
def paper(
    path: ExpArg,
    feeds: FeedsOpt,
    data: DataOpt = Path("data"),
    ledger: LedgerOpt = None,
    duration: DurationOpt = None,
    window: WindowOpt = 60,
    ui: UiOpt = False,
    ui_host: UiHostOpt = "127.0.0.1",
    ui_port: UiPortOpt = 8765,
    record: RecordOpt = True,
) -> None:
    """Paper trade: run an experiment's strategies on live feeds with simulated fills (no orders).

    Strategies on the shadow broker also have every order checked by the venue (none placed). Records
    into the experiment's run (<data>/session/<name>.sqlite), continuing it: positions are restored,
    and the history its strategies need (Strategy.warmup_s) is replayed from recordings first.
    """
    with _dashboard(data, ui_host, ui_port) if ui else contextlib.nullcontext():
        _session("paper", path, feeds, data, ledger, duration, window, armed=False, record=record)


@app.command()
def live(
    path: ExpArg,
    feeds: FeedsOpt,
    arm: Annotated[bool, typer.Option("--arm", help="Let the live broker place real orders")] = False,
    data: DataOpt = Path("data"),
    ledger: LedgerOpt = None,
    duration: DurationOpt = None,
    window: WindowOpt = 60,
    ui: UiOpt = False,
    ui_host: UiHostOpt = "127.0.0.1",
    ui_port: UiPortOpt = 8765,
    record: RecordOpt = True,
) -> None:
    """A session that may trade real money (needs --arm): strategies on the live broker place orders
    on the venues, within its caps.

    Strategies on sim or shadow paper trade in the same session and can be switched live while it
    runs (the dashboard's Go live, quanty promote --now), so it can start with everything on paper.
    """
    if not arm:
        raise _fail(ValueError("quanty live places real orders: pass --arm to confirm"))
    with _dashboard(data, ui_host, ui_port) if ui else contextlib.nullcontext():
        _session("live", path, feeds, data, ledger, duration, window, armed=True, record=record)


@contextlib.contextmanager
def _dashboard(data: Path, host: str, port: int) -> Iterator[None]:
    """`quanty ui` alongside a session, in its own process so serving pages never delays trading;
    stopped with the session. For one-container deployments (Fly.io)."""
    cmd = [sys.executable, "-m", "quanty", "ui", "--data", str(data), "--host", host, "--port", str(port)]
    proc = subprocess.Popen(cmd)
    try:
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


@app.command()
def doctor(
    path: Annotated[Path, typer.Argument(help="Project folder")] = Path("."),
    experiment_: Annotated[
        list[Path] | None,
        typer.Option("--experiment", help="Experiment(s) to check (default: experiments/*.toml)"),
    ] = None,
    feeds: Annotated[Path | None, typer.Option(help="Feed config (default: feeds.toml)")] = None,
    data: Annotated[Path | None, typer.Option(help="Data folder (default: data)")] = None,
    offline: Annotated[bool, typer.Option(help="Skip the network checks")] = False,
) -> None:
    """Check a project before trading: config, keys, clock, each venue, leftovers, disk. Read-only."""
    from quanty import doctor as dr

    exps = experiment_ or sorted((path / "experiments").glob("*.toml"))
    checks = dr.run(path.resolve(), exps, feeds or path / "feeds.toml", data or path / "data", offline)
    if _print_checks(checks):
        raise typer.Exit(1)


def _print_checks(checks: "list[Check]", only_problems: bool = False) -> int:
    """Doctor's checks, by section; returns how many failed. only_problems: just warnings and failures."""
    marks = {"ok": ("✓", "green"), "warn": ("!", "yellow"), "fail": ("✗", "red"), "skip": ("–", None)}
    section = None
    for c in checks:
        if only_problems and c.status not in ("warn", "fail"):
            continue
        if c.section != section:
            section = c.section
            typer.echo(typer.style(f"\n{section}", bold=True))
        mark, color = marks[c.status]
        typer.echo(f"  {typer.style(mark, fg=color)} {c.name}: {c.detail}")
        if c.fix:
            typer.echo(typer.style(f"      fix: {c.fix}", dim=True))
    fails = sum(c.status == "fail" for c in checks)
    warns = sum(c.status == "warn" for c in checks)
    typer.echo(f"\n{fails} problem(s), {warns} warning(s)" if fails or warns else "\nall good")
    return fails


@app.command()
def promote(
    instance: Annotated[str, typer.Argument(help="Instance id, from the experiment's [[instances]]")],
    to: Annotated[str, typer.Option("--to", help="sim (paper), shadow (checked by the venue) or live")],
    path: Annotated[Path, typer.Option("--experiment", help="Experiment file")] = Path(
        "experiments/main.toml"
    ),
    broker: Annotated[
        str | None, typer.Option(help="Broker to use or create (default: the first of that kind)")
    ] = None,
    max_qty: Annotated[
        float | None, typer.Option(help="New live broker: largest single order, contracts")
    ] = None,
    max_notional: Annotated[
        float | None, typer.Option(help="New live broker: largest single order, $")
    ] = None,
    data: DataOpt = Path("data"),
    check: Annotated[bool, typer.Option(help="Run quanty doctor on the result")] = True,
    twin: Annotated[bool, typer.Option(help="Going live: keep a paper twin beside it, for Compare")] = True,
    now: Annotated[
        bool, typer.Option("--now", help="Switch it in the running session (no restart); it saves the file")
    ] = False,
) -> None:
    """Move an instance to another kind of broker (paper → shadow → live, or back) by editing its
    experiment file, keeping a paper twin beside it when it goes live. Carries its dashboard edits
    along. Starts nothing: restart, reload, or --now to switch it in the running session."""
    from quanty import promote as pm

    if now:
        _switch_now(instance, to, broker, twin, pm.run_name(path), data)
        return
    try:
        experiment.load(path)  # a valid file to start from
        if pm.needs_caps(path, to, broker) and (max_qty is None or max_notional is None):
            typer.echo("The live broker has no caps yet; they bound every single order (a fat-finger guard):")
            max_qty = max_qty if max_qty is not None else typer.prompt("  max_qty (contracts)", type=float)
            max_notional = (
                max_notional if max_notional is not None else typer.prompt("  max_notional ($)", type=float)
            )
        run = pm.run_name(path)
        store = ControlStore(data / "control.sqlite") if (data / "control.sqlite").exists() else None
        carried = store.overrides(run).get(instance, {}) if store else {}
        done = pm.promote(
            path,
            instance,
            to,
            broker=broker,
            max_qty=max_qty,
            max_notional=max_notional,
            overrides=carried,
            twin=twin,
        )
        if store is not None and carried:
            store.set_override(run, instance, {})  # now in the file
        live_after = bool(experiment.live_brokers(experiment.load(path)))
    except (ValueError, OSError) as exc:
        raise _fail(exc) from None
    for line in done.lines():
        typer.echo(f"  {line}")
    typer.echo(
        f"  its history stays in the run {run}; from now on each order records whether it's real money"
    )
    if done.from_kind == "live" and to != "live":
        typer.echo("  note: what it holds on the venue stays there; the paper run doesn't know it", err=True)
    if check:
        from quanty import doctor as dr

        project = path.resolve().parent.parent
        checks = dr.run(project, [path], project / "feeds.toml", data, offline=to != "live")
        if _print_checks(checks, only_problems=True):
            typer.echo("fix those before starting it", err=True)
    cmd = f"quanty {'live' if live_after else 'paper'} {path} --feeds feeds.toml --data {data}"
    typer.echo(f"next: (re)start the session with `{cmd}{' --arm' if live_after else ''}`")


def _switch_now(instance: str, to: str, broker: str | None, twin: bool, run: str, data: Path) -> None:
    """Send the running session of `run` a switch, and wait for what it did."""
    path = data / "control.sqlite"
    store = ControlStore(path) if path.exists() else None
    if store is None or store.session(run) is None:
        raise _fail(ValueError(f"no running session of {run} here: start it, or drop --now to edit the file"))
    if to == "live" and not store.session(run)["can_arm"]:  # type: ignore[index]
        raise _fail(ValueError(f"{run} wasn't started with quanty live --arm: it can't switch anything live"))
    cid = store.submit(run, "switch", instance, {"to": to, "broker": broker, "twin": twin})
    for _ in range(100):
        row = next((c for c in store.history(run) if c["id"] == cid), None)
        if row is not None and row["status"] != "pending":
            if row["status"] != "done":
                raise _fail(ValueError(row["result"]))
            typer.echo(f"  {row['result']}")
            return
        time.sleep(0.1)
    typer.echo(f"sent; {run} hasn't answered yet (see the dashboard's Commands tab)", err=True)


@app.command()
def reload(
    path: Annotated[Path, typer.Option("--experiment", help="Experiment file")] = Path(
        "experiments/main.toml"
    ),
    data: DataOpt = Path("data"),
) -> None:
    """Make the running session of an experiment pick up its file: new instances start, removed ones
    retire (no new entries), changed params and brokers apply. No restart."""
    from quanty import promote as pm

    try:
        experiment.load(path)  # a valid file first: a broken one would only fail in the session
    except ValueError as exc:
        raise _fail(exc) from None
    run = pm.run_name(path)
    store = ControlStore(data / "control.sqlite") if (data / "control.sqlite").exists() else None
    if store is None or store.session(run) is None:
        raise _fail(ValueError(f"no running session of {run} here: start it with quanty paper or live"))
    cid = store.submit(run, "reload")
    for _ in range(100):
        row = next((c for c in store.history(run) if c["id"] == cid), None)
        if row is not None and row["status"] != "pending":
            if row["status"] != "done":
                raise _fail(ValueError(row["result"]))
            for part in str(row["result"]).split("; "):
                typer.echo(f"  {part}")
            return
        time.sleep(0.1)
    typer.echo(f"sent; {run} hasn't answered yet (see the dashboard's Commands tab)", err=True)


@app.command()
def changes(
    data: DataOpt = Path("data"),
    as_json: Annotated[bool, typer.Option("--json", help="As JSON (what quanty pull reads)")] = False,
) -> None:
    """Dashboard changes this machine's sessions are holding, not yet in their experiment files."""
    path = data / "control.sqlite"
    held = ControlStore(path).held_changes() if path.exists() else {}
    if as_json:
        typer.echo(json.dumps(held))
        return
    if not held:
        typer.echo("nothing held: the experiment files say it all")
    for run, h in sorted(held.items()):
        typer.echo(typer.style(run, bold=True))
        for iid, params in sorted(h["params"].items()):
            typer.echo(f"  {iid}: " + ", ".join(f"{k} = {v}" for k, v in params.items()))
        for iid, sw in sorted(h["switches"].items()):
            typer.echo(
                f"  {iid}: broker {sw['broker']}" + (f" (paper twin {sw['twin']})" if sw.get("twin") else "")
            )


@app.command()
def pull(
    host: Annotated[str, typer.Argument(help="The server, as you'd give ssh")],
    path: Annotated[Path, typer.Option(help="Project folder")] = Path("."),
    to: Annotated[str | None, typer.Option(help="Folder on the server (default: the project's name)")] = None,
    recordings: Annotated[
        bool, typer.Option("--recordings", help="Also copy the server's recent recordings into data/ here")
    ] = False,
    hours: Annotated[float, typer.Option(help="With --recordings: how many recent hours")] = 24.0,
    data: Annotated[Path, typer.Option(help="With --recordings: the data folder here")] = Path("data"),
) -> None:
    """Bring the dashboard changes a server is holding (param edits, switches to live or paper)
    into this project's experiment files. Review and commit them like any change. --recordings also
    copies its recent recordings here, to backtest new code on real markets before deploying it."""
    from quanty import pull as pl

    remote = to or path.resolve().name
    try:
        lines = pl.apply(path, pl.fetch(host, remote))
    except (ValueError, OSError) as exc:
        raise _fail(exc) from None
    typer.echo(
        "\n".join(f"  {line}" for line in lines) if lines else "nothing to bring home: the files say it all"
    )
    if recordings:
        data.mkdir(parents=True, exist_ok=True)
        cmd = pl.recordings_command(host, remote, data, hours)
        typer.echo(f"copying the last {hours:g} h of recordings into {data}/ ...")
        if subprocess.run(cmd).returncode:
            raise _fail(ValueError("rsync failed: see above"))
        from quanty.feeds.recording import recorded_feeds, span

        for feed in recorded_feeds(data):
            got = span(data, feed)
            size = sum(p.stat().st_size for p in (data / feed).rglob("*.jsonl.gz")) / 1e6
            if got:
                start = time.strftime("%Y-%m-%d %H:%M", time.gmtime(got[0] / 1000))
                typer.echo(f"  {feed}: from {start} UTC, {size:.0f} MB here")
        typer.echo(f"backtest on them: quanty backtest experiments/main.toml --data {data}")


@app.command()
def deploy(
    host: Annotated[
        str, typer.Argument(help="The server, as you'd give ssh: user@host or a ~/.ssh/config name")
    ],
    path: Annotated[Path, typer.Option(help="Project folder")] = Path("."),
    to: Annotated[
        str | None, typer.Option(help="Folder on the server (default: the project's name, in your home)")
    ] = None,
    check: Annotated[bool, typer.Option(help="Run `quanty doctor` on the server before starting")] = True,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the commands; run nothing")] = False,
    bring_home: Annotated[
        bool,
        typer.Option("--pull/--no-pull", help="First bring the server's dashboard changes into the files"),
    ] = True,
) -> None:
    """Copy this project to a server with Docker, check it there, and (re)start it. Run it again after
    every change. Never sends or deletes data/ or the server's keys (`quanty keys` puts those there).
    First brings the dashboard changes the server is holding into your experiment files, so a deploy
    never undoes them."""
    from quanty import deploy as dp
    from quanty import pull as pl

    if bring_home and not dry_run:
        try:
            lines = pl.apply(path, pl.fetch(host, to or path.resolve().name))
        except (ValueError, OSError) as exc:
            raise _fail(exc) from None
        if lines:
            typer.echo("brought home from the server (now in your files; commit them):")
            typer.echo("\n".join(f"  {line}" for line in lines))

    sent = version.git(path)
    if sent is None:
        typer.echo("note: not a git repo, so the server won't know which commit it runs", err=True)
    elif sent["dirty"]:
        files = ", ".join(sent["changed"][:5]) + (", …" if len(sent["changed"]) > 5 else "")
        typer.echo(
            f"note: uncommitted changes ({files}): runs there will record {sent['commit']} with changes; "
            "commit first to match one exactly",
            err=True,
        )
    stamp = {
        **({"commit": sent["commit"], "dirty": sent["dirty"]} if sent else {}),
        "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        cmds = dp.commands(host, path, to or path.resolve().name, check=check, stamp=stamp)
    except ValueError as exc:
        raise _fail(exc) from None
    for cmd in cmds:
        typer.echo(typer.style("$ " + shlex.join(cmd), dim=True))
        if dry_run:
            continue
        try:
            done = subprocess.run(cmd)
        except FileNotFoundError:
            raise _fail(ValueError(f"{cmd[0]} isn't installed here")) from None
        if done.returncode:
            typer.echo(f"stopped: {cmd[0]} exited {done.returncode}", err=True)
            raise typer.Exit(1)
    if not dry_run:
        typer.echo(f"deployed. Dashboard: ssh -N -L 8765:127.0.0.1:8765 {host}, then http://127.0.0.1:8765")


@app.command()
def mcp(
    url: Annotated[str, typer.Option(help="The dashboard (a local one, or the tunnel's end)")] = (
        "http://127.0.0.1:8765"
    ),
    ssh: Annotated[
        str | None, typer.Option(help="A server: open an SSH tunnel to its dashboard and use that")
    ] = None,
    remote_port: Annotated[int, typer.Option(help="The dashboard's port on the server, with --ssh")] = 8765,
) -> None:
    """The dashboard as tools for an AI agent (an MCP server on stdin/stdout): summarize and rank
    strategies, read trades and health, edit params, halt, resume, switch brokers, reload. The same
    checks as the dashboard; it can't arm or raise caps. Add it to Claude Code with
    `claude mcp add quanty -- uv run quanty mcp` (or `... mcp --ssh you@server`)."""
    from quanty import mcp as server

    try:
        server.run(url, ssh, remote_port)
    except RuntimeError as exc:
        raise _fail(exc) from None


@app.command()
def upgrade(
    source: Annotated[
        Path | None,
        typer.Option(help="A vendored project: the quanty checkout to build from (default: this one's)"),
    ] = None,
    path: Annotated[Path, typer.Option(help="Project folder")] = Path("."),
    refresh_only: Annotated[
        bool, typer.Option("--refresh-only", hidden=True, help="Only refresh the generated files")
    ] = False,
) -> None:
    """Bring this project onto a newer quanty, then refresh the generated files that still carry
    their "Generated by quanty init" line (Dockerfile, compose.yaml, README.md, AGENTS.md, ...). From
    PyPI: the latest release its pyproject.toml allows. Vendored: a new wheel built from a quanty
    checkout. Your strategies, experiments, feeds and keys are never touched. Then review, commit,
    and `quanty deploy`."""
    from quanty import init as project

    if refresh_only:  # run by upgrade itself, under the newly installed release
        refreshed, kept = project.refresh(path)
        typer.echo("refreshed: " + ", ".join(refreshed))
        typer.echo("kept: " + ", ".join(kept))
        return
    here = Path(project.__file__).resolve().parents[2]
    if source is None and (here / "pyproject.toml").exists() and (here / "src" / "quanty").is_dir():
        source = here
    if source is None and project.vendored(path):
        raise _fail(
            ValueError(
                "this project vendors quanty: pass --source ~/code/quanty (a checkout), or run "
                "`uv run --project ~/code/quanty quanty upgrade` here"
            )
        )
    try:
        what, refreshed, kept = project.upgrade(path, source.expanduser() if source else None)
    except ValueError as exc:
        raise _fail(exc) from None
    typer.echo(f"now on {what}")
    if refreshed:
        typer.echo("refreshed: " + ", ".join(refreshed))
    if kept:
        typer.echo("kept as yours (no generated line): " + ", ".join(kept))
    typer.echo("next: review the changes (git diff), commit, then `quanty deploy` to put them on a server")


@app.command()
def server(
    host: Annotated[str, typer.Argument(help="The server, as you'd give ssh: root@host, or a sudo user")],
    tailscale: Annotated[
        bool,
        typer.Option("--tailscale", help="Also Tailscale, with the dashboard served to your tailnet only"),
    ] = False,
    hostname: Annotated[str, typer.Option(help="With --tailscale: its name on your tailnet")] = "",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the script; run nothing")] = False,
) -> None:
    """Prepare a fresh Ubuntu or Debian server for `quanty deploy`, over SSH: Docker (signed apt
    repository), a firewall with only SSH open, swap on small machines, automatic security updates,
    and with --tailscale, Tailscale with the dashboard at https://<name>.<tailnet>.ts.net. Safe to
    run again: each step skips what's already there."""
    from quanty import deploy as dp

    script = dp.server_script(tailscale, hostname)
    if dry_run:
        typer.echo(script)
        return
    run_as = "bash -s" if host.split("@")[0] == "root" else "sudo bash -s"
    done = subprocess.run(["ssh", host, run_as], input=script.encode())
    if done.returncode:
        raise _fail(ValueError(f"setup stopped (exit {done.returncode}): see above; it's safe to run again"))


@app.command()
def keys(
    host: Annotated[str, typer.Argument(help="The server, as you'd give ssh")],
    file: Annotated[
        Path,
        typer.Option(
            help="The server's keys: KALSHI_KEY_ID=... lines, like .env", exists=True, dir_okay=False
        ),
    ] = Path(".env.server"),
    path: Annotated[Path, typer.Option(help="Project folder")] = Path("."),
    to: Annotated[str | None, typer.Option(help="Folder on the server (default: the project's name)")] = None,
) -> None:
    """Put a server's API keys in place, apart from the code: `quanty deploy` never sends or touches
    them. Run it before the first deploy, and again to rotate keys (running containers restart on
    them). Best with keys made for that server alone, so they can be revoked without touching others."""
    _send_keys(host, file, path, to)


def _send_keys(host: str, file: Path, path: Path, to: str | None) -> None:
    from quanty import deploy as dp

    try:
        names = dp.check_keys(file)
    except (ValueError, OSError) as exc:
        raise _fail(exc) from None
    remote = to or path.resolve().name
    done = subprocess.run(dp.keys_command(host, remote), input=file.read_bytes())
    if done.returncode:
        raise _fail(ValueError(f"ssh exited {done.returncode}: the server's keys weren't changed"))
    typer.echo(f"{host}:{remote}/.env now sets {', '.join(names)} (owner-only)")


@app.command()
def password(
    write: Annotated[
        bool, typer.Option("--write", help="Save it in the file instead of printing it")
    ] = False,
    file: Annotated[
        Path | None, typer.Option(help="With --write: the file (default .env, or .env.server with --server)")
    ] = None,
    server: Annotated[
        str | None,
        typer.Option(help="A server (as for ssh): save it in .env.server and send that there (quanty keys)"),
    ] = None,
    to: Annotated[
        str | None, typer.Option(help="With --server: its folder (default: the project's name)")
    ] = None,
) -> None:
    """Set the dashboard password: prints a QUANTY_UI_PASSWORD line for .env (only a salted hash).
    --server you@host saves it in .env.server and sends that file to the server, in one step."""
    from quanty.ui.auth import hash_password

    pw = typer.prompt("Dashboard password", hide_input=True, confirmation_prompt=True)
    if len(pw) < 8:
        raise _fail(ValueError("use at least 8 characters"))
    # single-quoted: Docker Compose substitutes $NAME in .env values, and the hash is full of `$`
    line = f"QUANTY_UI_PASSWORD='{hash_password(pw)}'"
    if not write and server is None:
        typer.echo(line)
        return
    env = file or Path(".env.server" if server else ".env")
    lines = [
        x
        for x in (env.read_text().splitlines() if env.exists() else [])
        if not x.startswith("QUANTY_UI_PASSWORD=")
    ]
    env.write_text("\n".join([*lines, line]) + "\n")
    env.chmod(0o600)
    if server is not None:
        typer.echo(f"saved to {env}")
        _send_keys(server, env, Path("."), to)  # the dashboard restarts on it
        return
    typer.echo(f"saved to {env}; restart `quanty ui` to use it (on a server: send it with `quanty keys`)")


@app.command()
def ui(
    data: DataOpt = Path("data"),
    host: Annotated[
        str, typer.Option(help="Interface to listen on (beyond localhost, set a password and use HTTPS)")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port")] = 8765,
    refresh: Annotated[int, typer.Option(help="Seconds between page refreshes")] = 5,
) -> None:
    """Dashboard of runs (ledgers under <data>/session for paper and live sessions, <data>/backtest)
    and feed health.

    With QUANTY_UI_PASSWORD set (`quanty password`), it asks for a password first."""
    import uvicorn

    from quanty.ui.app import create_app
    from quanty.venues.auth import keys

    login = keys().quanty_ui_password is not None
    if host not in ("127.0.0.1", "localhost", "::1") and not login:
        typer.echo(
            f"warning: listening on {host} with no login: anyone who can reach it sees the dashboard "
            "and its controls. Set one with `quanty password --write`.",
            err=True,
        )
    try:
        app_ = create_app(data, refresh_s=refresh)
    except ValueError as exc:
        raise _fail(exc) from None
    access = "login on" if login else "no login"
    typer.echo(f"quanty ui on http://{host}:{port} (data {data}; {access}; Ctrl-C to stop)")
    uvicorn.run(app_, host=host, port=port, log_level="warning")


def _session(
    mode: str,
    path: Path,
    feeds: Path,
    data: Path,
    ledger: Path | None,
    duration: float | None,
    window: float,
    *,
    armed: bool,
    record: bool = True,
) -> None:
    try:
        exp = experiment.load(path)
        live_names = experiment.live_brokers(exp)
        if mode == "paper" and live_names:
            raise ValueError(
                f"instances on {', '.join(live_names)} place real orders: use quanty live --arm, "
                'or broker = "shadow"'
            )
        if mode == "live" and not live_names and not experiment.parked_live_brokers(exp):
            raise ValueError(
                "the live broker has no caps: set max_qty and max_notional under [brokers.live] "
                "(the largest single order), or use quanty paper"
            )
        instances, spares = experiment.build_session(exp, _strategies(), armed=armed)
        feed_objs = load_feeds(feeds, None)
        keep = keep_days(feeds)
    except ValueError as exc:
        raise _fail(exc) from None
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))  # after any refusal
    # one run per experiment, paper or live: each order records whether it was real money
    ledger_path = ledger or data / "session" / f"{path.stem}.sqlite"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    for b in [*(i.broker for i in instances), *spares]:
        if isinstance(b, LiveBroker):
            b.journal = ledger_path.with_name(f"{path.stem}.{b.name}.orders.jsonl")
    book = Ledger(ledger_path, durable=True)
    trader = Engine(
        instances,
        ledger=book,
        limits=exp.limits,
        clock_ms=exp.clock_ms,
        strict=False,
        signals=experiment.signals(exp),
        spare_brokers=spares,
        code=version.project(Path.cwd()),  # the code it ran, with each strategy
    )
    _warm_and_restore(trader, exp, feeds, data, ledger_path)
    if armed:  # what it holds for real, against what the venues say the account holds
        from quanty import reconcile

        found = asyncio.run(reconcile.on_start(trader))
        for f in found:
            typer.echo(f"{'halted' if f.halts else 'note'}: {f.text()}", err=True)
    run_name = f"session/{ledger_path.stem}"
    store = ControlStore(data / "control.sqlite")
    controller = Controller(store, run_name, trader, can_arm=armed)
    controller.experiment = path.resolve()  # the dashboard's "Save to file" writes edits here
    controller.rebuild = lambda: (experiment.load(path), _strategies())  # "reload"
    session = RecordSession(
        feed_objs,
        data,
        data / "quanty.sqlite" if record else None,  # the recorder keeps the feed metrics then
        window_s=window,
        trader=trader,
        controller=controller,
        record=record,
        keep_days=keep,
    )
    ids = [i.id for i in instances]

    def report(windows: list[Window], status: dict[str, Status]) -> None:
        print_windows(windows, status)
        _print_summary(book.summary(), ids)
        if trader.halted:
            typer.echo(f"halted: {', '.join(sorted(trader.halted))}")

    verb = "LIVE trading" if mode == "live" else "paper trading"
    typer.echo(f"{verb} {', '.join(ids)} on {', '.join(feed_objs)}; ledger {ledger_path} (Ctrl-C to stop)")
    try:
        asyncio.run(session.run(duration, on_window=report))
    except TaskCrashed as exc:
        if not exc.expected:
            raise
        raise _fail(exc) from None
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        book.close()
        store.close()


def _params(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise ValueError(f"--param {pair!r}: expected KEY=VALUE")
        try:
            out[key] = tomllib.loads(f"v = {value}")["v"]
        except tomllib.TOMLDecodeError:
            out[key] = value
    return out


def _now_window_ms(window_s: int = 300) -> int:
    now = int(time.time() * 1000)
    return now - now % (window_s * 1000)


def _span(seconds: int) -> str:
    """4 h 10 min, 45 min, 30 s."""
    h, m = divmod(seconds // 60, 60)
    return " ".join([f"{h} h"] * bool(h) + [f"{m} min"] * bool(m)) or f"{seconds} s"


def _warm_and_restore(
    trader: Engine, exp: experiment.Experiment, feeds: Path, data: Path, ledger: Path
) -> None:
    """Before a session trades: replay the recent recordings (orders off) for the history its
    strategies need (or the experiment's warmup_s), then restore what the run holds from its ledger."""
    seconds, data_only = experiment.warmup(exp, (i.strategy for i in trader.instances.values()))
    if seconds:
        end = int(time.time() * 1000)
        typer.echo(
            f"warming up: replaying the last {_span(seconds)} of recordings in {data}/ (orders off"
            + (", data only)" if data_only else ")")
        )
        n = 0
        with trader.warming():
            for ev in Replay(load_feeds(feeds, None), data, end - seconds * 1000, end, data_only=data_only):
                trader.advance(ev.ts)
                trader.process(ev)
                n += 1
        typer.echo(f"  warmed up on {n:,} events" + ("" if n else " (no recordings for that time yet)"))
    got = trader.restore()
    if got["fills"]:
        typer.echo(
            f"continuing {ledger.name}: {got['open']} open position(s) restored from {got['fills']:,} fills"
        )
        assert trader.ledger is not None
        trader.ledger.log(trader.now, "session", "restored positions", got)


def _demo_live(
    exp: experiment.Experiment,
    data: Path,
    spec: synthetic.Spec,
    speed: float,
    port: int,
    browser: bool,
    reset_s: float | None = None,
) -> None:
    """Synthetic markets played against the wall clock (sped up), traded as a paper session, with
    the dashboard and its controls open on it: what a running session looks like, without keys.
    The dashboard's "New day" starts over on a freshly generated day (a new seed). reset_s: a public
    demo, on every interface, that exits after this long so its host starts a fresh one."""
    import uvicorn

    from quanty.ui.app import create_app

    run = "demo-live"
    (data / "session").mkdir(parents=True)
    ledger_path = data / "session" / f"{run}.sqlite"
    store = ControlStore(data / "control.sqlite")
    playback = Playback(speed, tuple(sorted({1.0, speed})))  # the dashboard toggles fast / real time
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(data, refresh_s=2, demo=True, public=reset_s),
            host="0.0.0.0" if reset_s else "127.0.0.1",  # a hosted demo serves everyone
            port=port,
            log_level="warning",
        )
    )

    def build(seed: int) -> tuple[Engine, Ledger, synthetic.Spec]:
        book = Ledger(ledger_path, durable=True)
        trader = Engine(
            experiment.build(exp, _strategies(), synthetic.FEES),
            ledger=book,
            limits=exp.limits,
            clock_ms=exp.clock_ms,
            strict=False,
        )
        return trader, book, dataclasses.replace(spec, seed=seed)

    trader, book, day = build(spec.seed)
    playback.seed = spec.seed
    controller = Controller(store, f"session/{run}", trader, can_arm=False, playback=playback)

    async def play(trader: Engine, day: synthetic.Spec) -> None:
        # the past (before now) at once, so the dashboard opens on a run with history; then live
        live_from = _now_window_ms()
        playback.catching_up, playback.progress = True, 0.0
        playback.set(playback.speed)  # re-anchor for this day
        book = trader.ledger
        assert book is not None
        book.durable = False  # the history is written in bulk (several times faster), committed when live
        span = max(1, live_from - day.start_ms)
        for n, ev in enumerate(synthetic.events_multi(day, ladder=True)):
            if ev.ts > live_from:
                if playback.catching_up:
                    book.commit()
                    book.durable = True
                    playback.catching_up, playback.progress = False, 1.0
                await playback.wait_for(ev.ts)
            elif n % 5000 == 0:
                playback.progress = (ev.ts - day.start_ms) / span
                await asyncio.sleep(0)  # the history plays at once; let the dashboard serve meanwhile
            trader.advance(ev.ts)
            trader.process(ev)
        server.should_exit = True

    state: dict[str, Any] = {"task": None, "book": book}

    async def new_day(seed: int) -> None:
        old = state["task"]
        old.cancel()
        await asyncio.gather(old, return_exceptions=True)
        state["book"].close()
        for suffix in ("", "-wal", "-shm"):  # the demo's own ledger, recreated for the new day
            Path(f"{ledger_path}{suffix}").unlink(missing_ok=True)
        trader, book, day = build(seed)
        state["book"] = book
        controller.attach(trader)
        state["task"] = asyncio.create_task(play(trader, day))

    def restart() -> int:
        seed = random.randrange(1, 1_000_000)
        playback.seed = seed
        playback.catching_up, playback.progress = True, 0.0  # reported at once: the page shows progress
        asyncio.get_running_loop().create_task(new_day(seed))
        return seed

    playback.on_new_day = restart

    async def main() -> None:
        state["task"] = asyncio.create_task(play(trader, day))
        tasks = [asyncio.create_task(controller.run_forever())]
        if reset_s:  # everything visitors changed goes with the process; the host starts a fresh one

            async def reset() -> None:
                await asyncio.sleep(reset_s)
                server.should_exit = True

            tasks.append(asyncio.create_task(reset()))
        try:
            await server.serve()
        finally:
            for t in [*tasks, state["task"]]:
                t.cancel()
            await asyncio.gather(*tasks, state["task"], return_exceptions=True)

    url = f"http://127.0.0.1:{port}/runs/session/{run}"
    typer.echo(f"live demo: synthetic up/down markets and a temperature ladder at {speed:g}x real time")
    typer.echo(f"dashboard: {url} (Ctrl-C to stop)")
    if browser:
        threading.Timer(1.5, webbrowser.open, args=[url]).start()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        state["book"].close()
        store.close()


def _backtest(
    exp: experiment.Experiment,
    hours: float,
    seed: int,
    ledger_path: Path | None,
    data: Path | None = None,
    start: str | None = None,
    end: str | None = None,
) -> None:
    if ledger_path and ledger_path.exists():
        raise ValueError(f"{ledger_path} already exists")
    if ledger_path:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
    if live_names := experiment.live_brokers(exp):
        raise ValueError(f"brokers {', '.join(live_names)} are live; backtests need sim or shadow brokers")
    instances = experiment.build(exp, _strategies(), synthetic.FEES)
    signals = experiment.signals(exp)
    ledger = Ledger(ledger_path or ":memory:")
    engine = Engine(
        instances,
        ledger=ledger,
        limits=exp.limits,
        clock_ms=exp.clock_ms,
        signals=signals,
        code=version.project(Path.cwd()),
    )
    events: Iterable[Event]
    if data is not None:
        feeds = load_feeds(None, recorded(data))
        lo, hi = recorded_range(data, list(feeds), start, end)
        seconds, data_only = experiment.warmup(exp, (i.strategy for i in instances))
        if seconds:  # the history its strategies need, orders off: before --start, or the first hours
            warm_from = lo - seconds * 1000 if start else lo
            lo = max(lo, warm_from + seconds * 1000)
            typer.echo(f"warming up on {_span(seconds)} of recordings (orders off), trading from then")
            with engine.warming():
                for ev in Replay(load_feeds(None, list(feeds)), data, warm_from, lo, data_only=data_only):
                    engine.advance(ev.ts)
                    engine.process(ev)
        events = Replay(feeds, data, lo, hi)
        typer.echo(f"replay {', '.join(feeds)} from {data}/")
    else:
        spec = synthetic.Spec(hours=hours, seed=seed)
        events = synthetic.events(spec)
        typer.echo(f"synthetic {spec.series}: {hours:g} h, seed {seed}")
        for inst in instances:  # a strategy for a venue's series never sees a synthetic market
            series = inst.strategy.series
            if series and spec.series not in series:
                name = type(inst.strategy).__module__.rsplit(".", 1)[-1]
                typer.echo(
                    f"note: {inst.id} trades {', '.join(series)}; synthetic markets are only {spec.series}, "
                    f"so it can't trade here. Try it on them with `quanty demo --strategy {name}` (or "
                    f'series = ["{spec.series}"] in its [[instances]] entry), or record its venue '
                    "(quanty record --config feeds.toml --duration 3600) and backtest with --data data.",
                    err=True,
                )
    engine.run(events)
    _print_summary(ledger.summary(), [i.id for i in instances])
    for sig in signals:
        typer.echo(f"signal {sig.name}: {sig.value}")
    ledger.close()


def _print_summary(rows: list[Summary], ids: list[str]) -> None:
    by_id = {r.instance: r for r in rows}
    typer.echo(
        f"{'instance':24} {'orders':>7} {'rej':>5} {'fills':>6} {'contracts':>10} {'fees':>8} "
        f"{'won':>5} {'lost':>5} {'P&L':>10}"
    )
    for iid in ids:
        r = by_id.get(iid, Summary(iid, 0, 0, 0, 0, 0.0, 0.0, 0, 0))
        typer.echo(
            f"{iid:24} {r.orders:7} {r.rejected:5} {r.fills:6} {r.contracts:10g} {r.fees:8.2f} "
            f"{r.wins:5} {r.losses:5} {r.pnl:10.2f}"
        )


if __name__ == "__main__":
    app()
