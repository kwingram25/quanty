"""quanty: record, backtest, paper and live trade repeated prediction markets."""

import tomllib
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any

import typer

from quanty import config, experiment, plugins, synthetic
from quanty.core import Engine, Ledger, Summary

app = typer.Typer(help=__doc__, no_args_is_help=True, add_completion=False)

Hours = Annotated[float, typer.Option(help="Synthetic hours to simulate")]
Seed = Annotated[int, typer.Option(help="Random seed for the synthetic markets")]
LedgerPath = Annotated[Path | None, typer.Option("--ledger", help="Write the ledger to this new SQLite file")]


def _strategies() -> dict[str, ModuleType]:
    return plugins.discover(config.load().strategy_paths)


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
) -> None:
    """Run an experiment config on synthetic markets."""
    try:
        _backtest(experiment.load(path), hours, seed, ledger)
    except ValueError as exc:
        raise _fail(exc) from None


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


def _backtest(exp: experiment.Experiment, hours: float, seed: int, ledger_path: Path | None) -> None:
    if ledger_path and ledger_path.exists():
        raise ValueError(f"{ledger_path} already exists")
    instances = experiment.build(exp, _strategies(), synthetic.FEES)
    ledger = Ledger(ledger_path or ":memory:")
    engine = Engine(instances, ledger=ledger, limits=exp.limits, clock_ms=exp.clock_ms)
    spec = synthetic.Spec(hours=hours, seed=seed)
    engine.run(synthetic.events(spec))
    typer.echo(f"synthetic {spec.series}: {hours:g} h, seed {seed}")
    _print_summary(ledger.summary(), [i.id for i in instances])
    ledger.close()


def _print_summary(rows: list[Summary], ids: list[str]) -> None:
    by_id = {r.instance: r for r in rows}
    typer.echo(f"{'instance':24} {'orders':>7} {'rej':>5} {'fills':>6} {'contracts':>10} {'fees':>8} "
               f"{'won':>5} {'lost':>5} {'P&L':>10}")  # fmt: skip
    for iid in ids:
        r = by_id.get(iid, Summary(iid, 0, 0, 0, 0, 0.0, 0.0, 0, 0))
        typer.echo(f"{iid:24} {r.orders:7} {r.rejected:5} {r.fills:6} {r.contracts:10} {r.fees:8.2f} "
                   f"{r.wins:5} {r.losses:5} {r.pnl:10.2f}")  # fmt: skip


if __name__ == "__main__":
    app()
