# Working on quanty

quanty records, backtests, paper trades, shadow trades and live trades repeated binary prediction
markets (Kalshi, Polymarket US). One strategy file runs unchanged in every mode: the engine is
event-sourced, and the broker behind `ctx` decides what an order means. Read `docs/architecture.md`
before changing the engine, brokers or ledger.

## Commands

```sh
uv run pytest                 # tests (no network, no keys)
uv run ruff check && uv run ruff format --check
uv run mypy                   # strict, over src, tests and scripts
cd web && npm run build       # after any change in web/: type-checks, then builds into src/quanty/ui/dist
uv run quanty demo --live     # the dashboard on synthetic markets, at http://127.0.0.1:8764
```

CI runs all of these. The dashboard build in `src/quanty/ui/dist` is committed (so installing
quanty never needs Node), and CI fails if it doesn't match `web/`: rebuild and commit it with any
change to `web/`.

## Layout

| Path | What |
|---|---|
| `src/quanty/core/` | engine, events, orders, portfolio, risk, ledger, the `Strategy` base |
| `src/quanty/brokers/` | `sim` (simulated fills), `shadow` (orders checked by the venue, not placed), `live` |
| `src/quanty/venues/` | Kalshi and Polymarket US: market data, order APIs, signing; `fake.py` is a fake exchange |
| `src/quanty/feeds/` | recording, replay, feed health |
| `src/quanty/control.py` | dashboard commands to running sessions (halt, arm, edit params, switch live, reload, …) |
| `src/quanty/reconcile.py` | a live session's start: what it holds for real against the venues' positions |
| `src/quanty/sweep.py` | backtests the dashboard starts: one strategy over a grid of its params, on recent recordings |
| `src/quanty/promote.py`, `pull.py` | experiment-file edits: moving an instance between brokers (with its paper twin); bringing a server's dashboard changes (and, with `--recordings`, its recent recordings) home |
| `src/quanty/version.py` | the code a run ran (git commit, or what `quanty deploy` sent; a hash per strategy file) |
| `src/quanty/mcp.py` | `quanty mcp`: the dashboard's API as MCP tools for an AI agent |
| `src/quanty/ui/` | FastAPI back end (`api.py`, `queries.py`, `auth.py`); `web/` is its React front end |
| `src/quanty/cli.py` | the `quanty` command; `init.py`, `deploy.py`, `doctor.py` behind its subcommands; `engine_tools.py` (paper twins in a running session) |
| `src/quanty/strategies/` | example strategies; `strategies/_template.py` is the template |
| `src/quanty/db/migrations/` | Alembic migrations for the ledger |

## Rules

- **No real orders, ever, from tests or while developing.** Use `venues/fake.py` (an httpx
  MockTransport speaking both venues' order routes) or `SimBroker`. Never run `quanty live --arm`
  or call a venue's order endpoints unless the user explicitly asks for it.
- **Never read, print or commit `.env`.** gitleaks runs in pre-commit and CI.
- The live broker refuses to place without being armed, caps every order (`max_qty`,
  `max_notional`), and stops itself when an order's outcome is unknown. Keep those properties.
- Strategies only see events and act through `ctx`; nothing in a strategy's path may read the wall
  clock, the network or randomness, so replays are exact.
- Ledger schema changes need an Alembic migration in `src/quanty/db/migrations/versions/`. The
  dashboard opens databases read-only; it writes only control commands (`data/control.sqlite`)
  and the backtests it starts (`data/backtest/`, via `sweep.py`).
- Venue reply fields follow the published API docs (Kalshi's OpenAPI spec, Polymarket US's
  docs); check them there before parsing a new field.
- `scripts/check_private.py` (pre-commit) refuses names from a denylist kept outside the repo.
  If it flags something, rename it; don't bypass the check.
- American spelling in code and prose (favorite, canceled, normalized).
- Match the surrounding code: comment density, naming, and the existing test style (plain
  pytest functions; `tests/helpers.py` builds markets, books and engines).
