# quanty

A self-hosted framework for recording, backtesting, paper trading and live trading **repeated
prediction markets** on Polymarket US and Kalshi: crypto up/down windows, daily weather highs,
economic releases, or any market that recurs on a schedule and settles against a known source.

> **Status: early development.** The engine, simulated broker and backtests on synthetic data
> work; venue adapters, recording, live trading and the web UI are next. Nothing here trades yet.

## Quickstart

```sh
git clone https://github.com/kwingram25/quanty && cd quanty
uv sync
uv run quanty demo                                    # example strategy, 6 synthetic hours
uv run quanty demo --param min_conf=0.97 --seed 3     # override a parameter
uv run quanty backtest experiments/example.toml --hours 24 --ledger run.sqlite
uv run quanty list                                    # strategies quanty can find
```

## Design in one paragraph

Venue and data-feed adapters turn every input into one normalised event stream. A strategy sees
only those events and acts only through a context (buy, sell, cancel). A broker decides what an
order means: a simulated fill in backtest and paper, an order preview in shadow, a real order in live.
The same strategy file runs unchanged in all four modes. Risk limits, the ledger, reconciliation
and latency metrics live in the core, and a web UI shows and manages running strategies.

## Your strategies stay private

Strategies are plain Python files loaded from paths in your own config, outside this repo:

```toml
# ~/.config/quanty/config.toml
strategy_paths = ["~/code/my-strategies/strategies"]
```

The bundled `src/quanty/strategies/` holds examples only. The simplest way to start:

```sh
cp strategies/_template.py strategies/my_idea.py      # gitignored; edit the TODOs
```

A strategy is a class with typed, config-overridable params and event hooks:

```python
@dataclass(frozen=True)
class MyParams(Params):
    min_edge: float = 0.02
    size: int = 1


class MyStrategy(Strategy[MyParams]):
    params_type = MyParams
    series = ("kalshi:KXHIGHNY",)

    def on_book(self, ctx: Context, book: Book) -> None:
        fair = ...  # your model
        price = book.ask(Side.YES)
        if price is not None and fair - price >= self.params.min_edge:
            ctx.buy(book.market_id, Side.YES, price, self.params.size, tif=Tif.IOC)
```

Experiments (`experiments/*.toml`, also gitignored except the example) pick strategies, params,
brokers and risk limits; see `experiments/example.toml`.

## Pieces

| | |
|---|---|
| `core/events.py` | normalised events: books, trades, external data, lifecycle, fills |
| `core/engine.py` | the loop: time-ordered events, strategy dispatch, order validation |
| `core/risk.py` | per-instance and account limits: order size, position, open risk, group risk, daily loss, kill switch |
| `core/portfolio.py`, `core/ledger.py` | positions and P&L from fills and settlements; SQLite record |
| `brokers/sim.py` | simulated venue: latency, taker fills, queue position, post-only |
| `synthetic.py` | deterministic synthetic markets for demos and tests |

## Development

```sh
uv sync
uv run pytest && uv run ruff check && uv run mypy
pre-commit install   # gitleaks, ruff, private-name guard
```

Database tables are SQLAlchemy models in `src/quanty/db/models.py`; quanty migrates SQLite files to
the latest schema when it opens them. After changing a model:

```sh
uv run alembic revision --autogenerate -m "what changed"   # review the generated file
uv run pytest tests/test_ledger.py                          # fails if models and migrations differ
```

`scripts/check_private.py` blocks commits that match regexes in a denylist kept outside the repo
(`$QUANTY_DENYLIST`, default `~/.config/quanty/denylist.txt`).

## License

MIT
