# quanty

A self-hosted framework for recording, backtesting, paper trading and live trading **repeated
prediction markets** on Polymarket US and Kalshi: crypto up/down windows, daily weather highs,
economic releases, or any market that recurs on a schedule and settles against a known source.

> **Status: early development.** The engine, simulated broker, feed and venue recording/replay
> (Kalshi, Polymarket US), backtests, paper trading, shadow mode and a live broker (tested against
> a fake exchange) and a read-only web dashboard work.

## Quickstart

```sh
git clone https://github.com/kwingram25/quanty && cd quanty
uv sync
uv run quanty demo                                    # example strategy, 6 synthetic hours
uv run quanty demo --param min_conf=0.97 --seed 3     # override a parameter
uv run quanty backtest experiments/example.toml --hours 24 --ledger run.sqlite
uv run quanty list                                    # strategies quanty can find
```

## Recording data

Feeds record every message exactly as received, so recordings can be re-parsed when a parser
improves and replayed through the engine with the same results as live.

```sh
uv run quanty feeds list                                 # feed kinds
uv run quanty record --feed coinbase --feed binance      # public data, no keys; Ctrl-C to stop
uv run quanty record --config feeds.toml                 # see feeds.example.toml
uv run quanty feeds health --data data                   # gaps, reconnects, receive lag p50/p99
uv run quanty replay --data data --show 5                # parse recordings back into events
uv run quanty backtest experiment.toml --data data       # run strategies and signals on recordings
```

Venues record the same way. Kalshi discovers markets for any series ticker (crypto windows,
weather, economics), rebuilds books from deltas and streams CF Benchmarks indexes; Polymarket US
finds repeated markets by slug template. Market discovery responses are recorded in the same
stream as books and trades, so a replay sees markets open, strikes appear and results arrive in
the order they did live. WebSocket connections need keys in `.env` (see `.env.example`);
replaying never does.

```sh
uv run quanty record --config feeds.toml --data data     # with [feeds.kalshi] / [feeds.polymarket_us]
```

Recordings live in `data/<feed>/<date>/<hour>.jsonl.gz` (gitignored); per-minute health and
latency metrics go to `data/quanty.sqlite`. Signals (`last`, `ewma`, `realized_vol`) are declared
in experiments and read by strategies with `ctx.signal(name)`.

## Design in one paragraph

Venue and data-feed adapters turn every input into one normalized event stream. A strategy sees
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

A strategy is a class with typed params (Pydantic, strictly validated, overridable from
experiment configs) and event hooks:

```python
class MyParams(Params):
    min_edge: float = Field(0.02, ge=0, description="Buy when the model beats the price by this much")
    size: int = Field(1, gt=0)


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

## Paper and shadow trading

```sh
uv run quanty paper experiment.toml --feeds feeds.toml --data data
```

runs an experiment's strategies in real time on live feeds. With `kind = "sim"` brokers fills are
simulated; with `kind = "shadow"` they are simulated too, and every order is also turned into the
venue's real request and checked: Polymarket US validates it server-side without placing it
(`/v1/order/preview`), Kalshi requests are built, signed and checked locally. Results and round
trips are in the ledger (`data/paper/<experiment>.sqlite`).

## Live trading

Live brokers place real orders, so they are fenced off:

```toml
[brokers.real]
kind = "live"
max_qty = 100        # largest single order, contracts (required)
max_notional = 100   # largest single order, $ (required)
```

`quanty paper` and `quanty backtest` refuse live brokers; `quanty live experiment.toml --feeds
feeds.toml --arm` runs them. A live broker disables itself after `error_limit` consecutive
rejections, and at once if an order's reply never arrives (the order stays pending: check the
venue before restarting). Each live broker journals its orders next to the ledger
(`<experiment>.<broker>.orders.jsonl`); on start it cancels any it left resting in a previous run,
and nothing else, so other programs on the same account are safe. `quanty.venues.fake.FakeExchange` speaks both venues' order APIs with
injectable faults, for testing brokers without real orders.

## Dashboard

```sh
uv run quanty ui --data data    # http://127.0.0.1:8765
```

Every run under `data/{live,paper,backtest}/`: realized P&L over time per strategy, open positions,
fills, orders, and the events worth seeing (warnings, errors, shadow previews, live broker
events), plus feed health. Read-only and safe beside running sessions; it refreshes itself.
There is no login, so it listens on localhost only by default.

Runs that are running (`quanty paper`, `quanty live`) also get controls: halt or resume a
strategy (no new buys, resting buys canceled, exits still allowed), disarm or re-arm a live
broker (re-arming only if the session was started with `--arm`), and a kill switch that halts
everything. Sessions pick commands up from `data/control.sqlite` within a second and record the
outcome; actions on a live run need its name typed to confirm, and every form carries a
per-server token so another web page can't post to it.

## Pieces

| | |
|---|---|
| `core/events.py` | normalized events: books, trades, external data, lifecycle, fills |
| `core/engine.py` | the loop: time-ordered events, strategy dispatch, order validation |
| `core/risk.py` | per-instance and account limits: order size, position, open risk, group risk, daily loss, kill switch |
| `core/portfolio.py`, `core/ledger.py` | positions and P&L from fills and settlements; SQLite record |
| `brokers/sim.py` | simulated venue: latency, taker fills, queue position (configurable `QueueModel`), post-only |
| `brokers/shadow.py` | simulated fills plus a real venue preview of every order |
| `brokers/live.py` | real orders: placement, fill polling, cancel races, safety stops |
| `ui/` | the dashboard (FastAPI, server-rendered pages, HTMX refresh, SVG charts) |
| `venues/orders.py`, `venues/fake.py` | venue order APIs, and a fake exchange that speaks them |
| `feeds/` | WebSocket and polling transports, Coinbase and Binance adapters, raw recording, replay, health and latency |
| `venues/` | Kalshi and Polymarket US: discovery, books, trades, settlement, fees, request signing |
| `signals.py` | derived values (last, EWMA, realized vol) rebuilt identically in replay |
| `db/` | SQLAlchemy models and Alembic migrations (ledger, feed metrics) |
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
