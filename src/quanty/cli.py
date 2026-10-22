"""quanty: record, backtest, paper and live trade repeated prediction markets."""

import argparse
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from quanty import config, experiment, plugins, synthetic
from quanty.core import Engine, Ledger, Summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quanty", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list the strategies quanty can find")

    demo = sub.add_parser("demo", help="backtest one strategy on synthetic up/down markets")
    demo.add_argument("--strategy", default="example_favourite")
    demo.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    demo.add_argument("--latency-ms", type=int, default=50)
    _synthetic_args(demo)

    bt = sub.add_parser("backtest", help="run an experiment config on synthetic markets")
    bt.add_argument("experiment", type=Path)
    _synthetic_args(bt)

    args = parser.parse_args(argv)
    try:
        strategies = plugins.discover(config.load().strategy_paths)
        if args.cmd == "list":
            return _list(strategies)
        if args.cmd == "demo":
            raw = {
                "brokers": {"sim": {"kind": "sim", "latency_ms": args.latency_ms}},
                "instances": [
                    {"id": args.strategy, "strategy": args.strategy, "params": _params(args.param)}
                ],
            }
            exp = experiment.parse(raw, source="demo")
        else:
            exp = experiment.load(args.experiment)
        return _backtest(exp, strategies, args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"quanty: {exc}", file=sys.stderr)
        return 2


def _synthetic_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--hours", type=float, default=6.0)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--ledger", type=Path, help="write the ledger to this SQLite file")


def _params(pairs: list[str]) -> dict[str, object]:
    out: dict[str, object] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise ValueError(f"--param {pair!r}: expected KEY=VALUE")
        try:
            out[key] = tomllib.loads(f"v = {value}")["v"]
        except tomllib.TOMLDecodeError:
            out[key] = value
    return out


def _list(strategies: dict[str, ModuleType]) -> int:
    for name, module in sorted(strategies.items()):
        try:
            desc = experiment.strategy_class(module).description
        except ValueError as exc:
            desc = f"(invalid: {exc})"
        print(f"{name:28} {desc}")
    return 0


def _backtest(exp: experiment.Experiment, strategies: dict[str, ModuleType], args: argparse.Namespace) -> int:
    if args.ledger and args.ledger.exists():
        raise ValueError(f"{args.ledger} already exists")
    instances = experiment.build(exp, strategies, synthetic.FEES)
    ledger = Ledger(args.ledger or ":memory:")
    engine = Engine(instances, ledger=ledger, limits=exp.limits, clock_ms=exp.clock_ms)
    spec = synthetic.Spec(hours=args.hours, seed=args.seed)
    engine.run(synthetic.events(spec))
    print(f"synthetic {spec.series}: {args.hours:g} h, seed {args.seed}")
    _print_summary(ledger.summary(), [i.id for i in instances])
    ledger.close()
    return 0


def _print_summary(rows: list[Summary], ids: list[str]) -> None:
    by_id = {r.instance: r for r in rows}
    print(f"{'instance':24} {'orders':>7} {'rej':>5} {'fills':>6} {'contracts':>10} {'fees':>8} "
          f"{'won':>5} {'lost':>5} {'P&L':>10}")  # fmt: skip
    for iid in ids:
        r = by_id.get(iid, Summary(iid, 0, 0, 0, 0, 0.0, 0.0, 0, 0))
        print(f"{iid:24} {r.orders:7} {r.rejected:5} {r.fills:6} {r.contracts:10} {r.fees:8.2f} "
              f"{r.wins:5} {r.losses:5} {r.pnl:10.2f}")  # fmt: skip


if __name__ == "__main__":
    sys.exit(main())
