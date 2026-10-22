# quanty

A self-hosted framework for recording, backtesting, paper trading and live trading **repeated
prediction markets** on Polymarket US and Kalshi: crypto up/down windows, daily weather highs,
economic releases, or any market that recurs on a schedule and settles against a known source.

> **Status: early development.** Nothing here trades yet.

## Design in one paragraph

Venue and data-feed adapters turn every input into one normalised event stream. A strategy sees
only those events and returns intents (place, cancel, exit). A broker decides what an intent
means: a simulated fill in backtest and paper, an order preview in shadow, a real order in live.
The same strategy file runs unchanged in all four modes. Risk limits, the ledger, reconciliation
and latency metrics live in the core, and a web UI shows and manages running strategies.

## Your strategies stay private

Strategies are plain Python files loaded from paths in your own config, outside this repo:

```toml
# ~/.config/quanty/config.toml
strategy_paths = ["~/code/my-strategies/strategies"]
```

The bundled `src/quanty/strategies/` holds examples only.

## Development

```sh
uv sync
uv run pytest && uv run ruff check && uv run mypy
pre-commit install   # gitleaks, ruff, private-name guard
```

`scripts/check_private.py` blocks commits that match regexes in a denylist kept outside the repo
(`$QUANTY_DENYLIST`, default `~/.config/quanty/denylist.txt`).

## License

MIT
