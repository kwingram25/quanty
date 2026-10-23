"""quanty: record, backtest, paper and live trade repeated prediction markets."""

import asyncio
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any

import structlog
import typer

from quanty import config, experiment, plugins, synthetic
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
Seed = Annotated[int, typer.Option(help="Random seed for the synthetic markets")]
LedgerPath = Annotated[Path | None, typer.Option("--ledger", help="Write the ledger to this new SQLite file")]


def _strategies() -> dict[str, ModuleType]:
    cfg = config.load()
    return plugins.discover(cfg.strategy_paths, python_paths=cfg.python_paths)


def _fail(exc: Exception) -> typer.Exit:
    typer.echo(f"quanty: {exc}", err=True)
    return typer.Exit(2)


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
    hours: Hours = 6.0,
    seed: Seed = 0,
    ledger: LedgerPath = None,
) -> None:
    """Backtest one strategy on synthetic up/down markets."""
    try:
        raw = {
            "brokers": {"sim": {"kind": "sim", "latency_ms": latency_ms}},
            "instances": [{"id": strategy, "strategy": strategy, "params": _params(param or [])}],
        }
        _backtest(experiment.parse(raw, source="demo"), hours, seed, ledger)
    except ValueError as exc:
        raise _fail(exc) from None


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


@app.command()
def paper(
    path: ExpArg,
    feeds: FeedsOpt,
    data: DataOpt = Path("data"),
    ledger: LedgerOpt = None,
    duration: DurationOpt = None,
    window: WindowOpt = 60,
) -> None:
    """Paper trade: run an experiment's strategies on live feeds with simulated fills (no orders).

    Brokers with kind = "shadow" also check every order with the venue (nothing is placed).
    """
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
) -> None:
    """Trade for real: kind = "live" brokers place orders on the venues. Needs --arm."""
    if not arm:
        raise _fail(ValueError("quanty live places real orders: pass --arm to confirm"))
    _session("live", path, feeds, data, ledger, duration, window, armed=True)


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
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))
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
    ledger_path = ledger or data / mode / f"{path.stem}.sqlite"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    book = Ledger(ledger_path, durable=True)
    trader = Engine(instances, ledger=book, limits=exp.limits, clock_ms=exp.clock_ms, strict=False,
                    signals=experiment.signals(exp))  # fmt: skip
    session = RecordSession(feed_objs, data, data / "quanty.sqlite", window_s=window, trader=trader)
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


def _backtest(
    exp: experiment.Experiment, hours: float, seed: int, ledger_path: Path | None,
    data: Path | None = None, start: str | None = None, end: str | None = None,
) -> None:  # fmt: skip
    if ledger_path and ledger_path.exists():
        raise ValueError(f"{ledger_path} already exists")
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
    typer.echo(f"{'instance':24} {'orders':>7} {'rej':>5} {'fills':>6} {'contracts':>10} {'fees':>8} "
               f"{'won':>5} {'lost':>5} {'P&L':>10}")  # fmt: skip
    for iid in ids:
        r = by_id.get(iid, Summary(iid, 0, 0, 0, 0, 0.0, 0.0, 0, 0))
        typer.echo(f"{iid:24} {r.orders:7} {r.rejected:5} {r.fills:6} {r.contracts:10g} {r.fees:8.2f} "
                   f"{r.wins:5} {r.losses:5} {r.pnl:10.2f}")  # fmt: skip


if __name__ == "__main__":
    app()
