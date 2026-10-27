# Changelog

quanty is `quanty-py` on PyPI. Every release is a new minor version (0.2, 0.3, ...). While it's at
0.x, a release may change what your project relies on, and says so here with what to do: the
`Strategy` class and `ctx`, experiment and feeds files, the ledger's schema (migrated for you when a
file is opened) and the command line. Projects from `quanty init` depend on one minor version
(`quanty-py>=0.4.0,<0.5`), so moving to the next is your choice: raise the bound in
`pyproject.toml`, then `quanty upgrade`.

## Unreleased

- The dashboard's open markets drop a market as its countdown ends, rather than at the next update
  (which a slow connection, or a sped-up demo, made seconds or a demo minute late).

## 0.4.0 (2026-10-27)

Upgrading from 0.3: change the bound to `quanty-py>=0.4.0,<0.5`, then `quanty upgrade` (it brings the
new Dockerfile). Recordings made before it have no data files, so a warm-up is fully quick once the
recorder has run on 0.4 for as long as the warm-up reaches back (4 hours, say).

- Restarts are quicker. The recorder also writes each hour's data messages (Kalshi's index values,
  among its books) to a small file beside the hour's (`<HH>.data.jsonl.gz`, about 2% of it), and a
  data-only warm-up reads those: about 13 times faster, once hours have them (older ones are read
  whole). It skips messages that can't hold data before decoding them, and doesn't read feeds that
  hold none (Polymarket US). A session
  connects its feeds while it warms up: what arrives meanwhile builds its books and markets (orders
  off), and it trades from the next event, where it waited for the warm-up before.
- The generated Dockerfile installs from `pyproject.toml`, `uv.lock` and `vendor/` before copying
  the project, so a deploy that changes neither rebuilds in seconds (`quanty upgrade` brings it).
- Feeds can declare `data_marker` and `holds_data`, for data-only replays (docs/extending.md).

## 0.3.0 (2026-10-27)

Upgrading from 0.2: change the bound to `quanty-py>=0.3.0,<0.4`, then `quanty upgrade`. Nothing
else changes for a project.

- Reload (`quanty reload`, or the dashboard) applies simulated brokers' settings (latency, queue
  model) to orders from then on, and adds new simulated or shadow brokers, so a strategy can be
  given its own fill model without a restart. A new broker starts with each open market's book.
- Fix: `quanty backtest --data` and `quanty replay` parse recordings with the project's
  `feeds.toml` (or `--feeds`/`--config`), not built-in defaults, which dropped every market outside
  the default series (Polymarket US 5m, Kalshi series other than KXBTC15M). The dashboard's
  Backtest variants runs through it, so it's fixed there too.

## 0.2.0 (2026-10-27)

Upgrading from 0.1: in your project's `pyproject.toml`, change `quanty-py>=0.1.0,<0.2` to
`quanty-py>=0.2.0,<0.3`, then `quanty upgrade`. Experiment, feeds and strategy files need no
changes. Only code that imported quanty's internals is affected: `quanty.signals.KINDS` is now
`quanty.signals.kinds()`, and `AnySignalConfig` is gone (an experiment's `signals` are validated
by their `kind`, extensions' kinds included).

- Extensions: your own data feeds, signals and venues, from a project's `extensions/` folder or an
  installed package's `quanty.extensions` entry point; recorded, replayed, health-checked and
  checked by `quanty doctor` like the built-in ones. See docs/extending.md.
- `quanty doctor` asks each venue for its own key and rate-limit checks (`OrderAPI.key_warning`,
  `limits`, `key_env`).
- `quanty demo` trades 100 contracts a time (`--param size=` still sets it).

## 0.1.0 (2026-10-27)

The first release.

- Recording of Kalshi, Polymarket US, Coinbase and Binance feeds, exactly as received, with
  per-minute feed health, and replay of recordings in order.
- Backtests on recordings or synthetic markets, with a fill model for latency, book depth and queue
  position.
- Paper, shadow and live trading of the same strategy file, switched per strategy while a session
  runs, with paper twins to compare live fills against simulated ones.
- Risk limits in the core; a live broker that needs caps and `--arm`, and stops itself when an
  order's outcome is unknown; reconciliation with the venues' positions on start.
- A dashboard (P&L, trades, orders, open markets, the variant ranking and its backtests, health,
  controls), with an optional login, and `quanty mcp` for AI agents.
- `quanty init` projects with Docker, compose, Fly.io and DigitalOcean (OpenTofu) files;
  `quanty server`, `keys`, `deploy`, `pull` and `upgrade` to run them on a server.
