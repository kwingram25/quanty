"""quanty: record, backtest, paper and live trade repeated prediction markets."""

import asyncio
import contextlib
import dataclasses
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
from typing import Annotated, Any

import structlog
import typer

from quanty import config, experiment, plugins, synthetic
from quanty.brokers.live import LiveBroker
from quanty.control import Controller, ControlStore, Playback
from quanty.core import Engine, Event, Ledger, Summary
from quanty.feeds.cli import feeds_app, load_feeds, print_windows, record, recorded, recorded_range, replay
from quanty.feeds.health import Status, Window
from quanty.feeds.replay import Replay
from quanty.feeds.session import RecordSession

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
) -> None:
    """Create a project: a strategy skeleton, configs, API keys, and Docker files to deploy it."""
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
        written = project.create(path, a, force=force)
    except ValueError as exc:
        raise _fail(exc) from None
    typer.echo(f"created {path}/: " + ", ".join(written))
    typer.echo(f"next: cd {path} && uv run quanty list   (then see README.md)")


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
    port: Annotated[int, typer.Option(help="Dashboard port, with --ui")] = 8765,
    browser: Annotated[bool, typer.Option(help="Open a browser, with --ui")] = True,
    live: Annotated[
        bool, typer.Option("--live", help="Play the markets in real time with the dashboard open (paper)")
    ] = False,
    speed: Annotated[float, typer.Option(help="With --live: market time per wall-clock second")] = 10.0,
    history: Annotated[float, typer.Option(help="With --live: hours already traded when it opens")] = 24.0,
) -> None:
    """Backtest one strategy on synthetic up/down markets.

    With --ui (and no --param), three variants of the example run side by side and the dashboard
    opens on them: the whole loop with no keys and no setup.
    """
    ui = ui or live
    try:
        instances = [{"id": strategy, "strategy": strategy, "params": _params(param or [])}]
        if ui and not param and strategy == "example_favorite":
            instances = [
                {"id": f"fav_{round(c * 100)}", "strategy": strategy, "params": {"min_conf": c}}
                for c in (0.90, 0.95, 0.97)
            ]
        if live and not param and strategy == "example_favorite":  # the live demo also has range markets
            instances.append({"id": "temp_buckets", "strategy": "example_bucket", "params": {}})
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
            _demo_live(exp, data, spec, speed, port, browser)
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
) -> None:
    """Paper trade: run an experiment's strategies on live feeds with simulated fills (no orders).

    Brokers with kind = "shadow" also check every order with the venue (nothing is placed).
    """
    with _dashboard(data, ui_host, ui_port) if ui else contextlib.nullcontext():
        _session("paper", path, feeds, data, ledger, duration, window, armed=False)


@app.command()
def live(
    path: ExpArg,
    feeds: FeedsOpt,
    arm: Annotated[
        bool, typer.Option("--arm", help='Place real orders through kind = "live" brokers')
    ] = False,
    data: DataOpt = Path("data"),
    ledger: LedgerOpt = None,
    duration: DurationOpt = None,
    window: WindowOpt = 60,
    ui: UiOpt = False,
    ui_host: UiHostOpt = "127.0.0.1",
    ui_port: UiPortOpt = 8765,
) -> None:
    """Trade for real: kind = "live" brokers place orders on the venues. Needs --arm."""
    if not arm:
        raise _fail(ValueError("quanty live places real orders: pass --arm to confirm"))
    with _dashboard(data, ui_host, ui_port) if ui else contextlib.nullcontext():
        _session("live", path, feeds, data, ledger, duration, window, armed=True)


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
    marks = {"ok": ("✓", "green"), "warn": ("!", "yellow"), "fail": ("✗", "red"), "skip": ("–", None)}
    section = None
    for c in checks:
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
    if fails:
        raise typer.Exit(1)


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
) -> None:
    """Copy this project to a server with Docker, check it there, and (re)start it. Run it again after
    every change. Never sends or deletes data/."""
    from quanty import deploy as dp

    try:
        cmds = dp.commands(host, path, to or path.resolve().name, check=check)
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
def password(
    write: Annotated[bool, typer.Option("--write", help="Save it in ./.env instead of printing it")] = False,
) -> None:
    """Set the dashboard password: prints a QUANTY_UI_PASSWORD line for .env (only a salted hash)."""
    from quanty.ui.auth import hash_password

    pw = typer.prompt("Dashboard password", hide_input=True, confirmation_prompt=True)
    if len(pw) < 8:
        raise _fail(ValueError("use at least 8 characters"))
    line = f"QUANTY_UI_PASSWORD={hash_password(pw)}"
    if not write:
        typer.echo(line)
        return
    env = Path(".env")
    lines = [
        x
        for x in (env.read_text().splitlines() if env.exists() else [])
        if not x.startswith("QUANTY_UI_PASSWORD=")
    ]
    env.write_text("\n".join([*lines, line]) + "\n")
    env.chmod(0o600)
    typer.echo("saved to .env; restart `quanty ui` to use it")


@app.command()
def ui(
    data: DataOpt = Path("data"),
    host: Annotated[
        str, typer.Option(help="Interface to listen on (beyond localhost, set a password and use HTTPS)")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port")] = 8765,
    refresh: Annotated[int, typer.Option(help="Seconds between page refreshes")] = 5,
) -> None:
    """Dashboard of runs (ledgers under <data>/{live,paper,backtest}) and feed health.

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
) -> None:
    try:
        exp = experiment.load(path)
        live_names = experiment.live_brokers(exp)
        if mode == "paper" and live_names:
            raise ValueError(
                f'brokers {", ".join(live_names)} are live: use quanty live --arm, or kind = "shadow"'
            )
        if mode == "live" and not live_names:
            raise ValueError('no kind = "live" broker in this experiment: use quanty paper')
        instances = experiment.build(exp, _strategies(), armed=armed)
        feed_objs = load_feeds(feeds, None)
    except ValueError as exc:
        raise _fail(exc) from None
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))  # after any refusal
    ledger_path = ledger or data / mode / f"{path.stem}.sqlite"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    for inst in instances:
        if isinstance(inst.broker, LiveBroker):
            inst.broker.journal = ledger_path.with_name(f"{path.stem}.{inst.broker.name}.orders.jsonl")
    book = Ledger(ledger_path, durable=True)
    trader = Engine(
        instances,
        ledger=book,
        limits=exp.limits,
        clock_ms=exp.clock_ms,
        strict=False,
        signals=experiment.signals(exp),
    )
    run_name = f"{mode}/{ledger_path.stem}"
    store = ControlStore(data / "control.sqlite")
    controller = Controller(store, run_name, trader, can_arm=armed)
    session = RecordSession(
        feed_objs, data, data / "quanty.sqlite", window_s=window, trader=trader, controller=controller
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


def _demo_live(
    exp: experiment.Experiment, data: Path, spec: synthetic.Spec, speed: float, port: int, browser: bool
) -> None:
    """Synthetic markets played against the wall clock (sped up), traded as a paper session, with
    the dashboard and its controls open on it: what a running session looks like, without keys.
    The dashboard's "New day" starts over on a freshly generated day (a new seed)."""
    import uvicorn

    from quanty.ui.app import create_app

    run = "demo-live"
    (data / "paper").mkdir(parents=True)
    ledger_path = data / "paper" / f"{run}.sqlite"
    store = ControlStore(data / "control.sqlite")
    playback = Playback(speed, tuple(sorted({1.0, speed})))  # the dashboard toggles fast / real time
    server = uvicorn.Server(
        uvicorn.Config(create_app(data, refresh_s=2), host="127.0.0.1", port=port, log_level="warning")
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
    controller = Controller(store, f"paper/{run}", trader, can_arm=False, playback=playback)

    async def play(trader: Engine, day: synthetic.Spec) -> None:
        # the past (before now) at once, so the dashboard opens on a run with history; then live
        live_from = _now_window_ms()
        playback.catching_up = True
        playback.set(playback.speed)  # re-anchor for this day
        for n, ev in enumerate(synthetic.events_multi(day, ladder=True)):
            if ev.ts > live_from:
                playback.catching_up = False
                await playback.wait_for(ev.ts)
            elif n % 5000 == 0:
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
        controller.trader = trader
        controller.brokers = {i.broker.name: i.broker for i in trader.instances.values()}
        state["task"] = asyncio.create_task(play(trader, day))

    def restart() -> int:
        seed = random.randrange(1, 1_000_000)
        playback.seed = seed
        asyncio.get_running_loop().create_task(new_day(seed))
        return seed

    playback.on_new_day = restart

    async def main() -> None:
        state["task"] = asyncio.create_task(play(trader, day))
        tasks = [asyncio.create_task(controller.run_forever())]
        try:
            await server.serve()
        finally:
            for t in [*tasks, state["task"]]:
                t.cancel()
            await asyncio.gather(*tasks, state["task"], return_exceptions=True)

    url = f"http://127.0.0.1:{port}/runs/paper/{run}"
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
    engine = Engine(instances, ledger=ledger, limits=exp.limits, clock_ms=exp.clock_ms, signals=signals)
    events: Iterable[Event]
    if data is not None:
        feeds = load_feeds(None, recorded(data))
        lo, hi = recorded_range(data, list(feeds), start, end)
        events = Replay(feeds, data, lo, hi)
        typer.echo(f"replay {', '.join(feeds)} from {data}/")
    else:
        spec = synthetic.Spec(hours=hours, seed=seed)
        events = synthetic.events(spec)
        typer.echo(f"synthetic {spec.series}: {hours:g} h, seed {seed}")
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
