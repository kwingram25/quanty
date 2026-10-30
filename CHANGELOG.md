# Changelog

quanty is `quanty-py` on PyPI. Every release is a new minor version (0.2, 0.3, ...). While it's at
0.x, a release may change what your project relies on, and says so here with what to do: the
`Strategy` class and `ctx`, experiment and feeds files, the ledger's schema (migrated for you when a
file is opened) and the command line. Projects from `quanty init` depend on one minor version
(`quanty-py>=0.5.0,<0.6`), so moving to the next is your choice: raise the bound in
`pyproject.toml`, then `quanty upgrade`.

## Unreleased

- Fix: Kalshi's index stopped when Kalshi listed no market (its weekly maintenance, Thursdays 3-5 AM
  Eastern): the feed closed its connection, which carries `index_ids` too, so every strategy reading
  the index stopped, including ones trading other venues. A feed with an index now keeps its
  connection with no market listed, and subscribes markets on it again when they return (no
  reconnect).
- Feed health judges a venue feed by its stream: the REST polls recorded beside it no longer keep it
  "ok" when the stream has gone quiet (`Feed.streamed`).
- Less CPU per message, for busy markets on small servers: Kalshi book updates rebuild only the side
  that moved (no side's levels are kept by the ask they make, converted once), the recorder works
  out an hour's file once instead of per message, and the engine asks each strategy whether it
  wants a market once (again after its params change) instead of on every book and trade. A
  backtest of 4.7 hours of BTC and XRP markets runs 22% faster with identical orders and fills;
  the recorder's own work per message is 44% less. A strategy's `wants()` must now answer from
  the market and its params alone (as the built-in ones do).
- Fix: orders resting when a session stopped (a deploy, a restart) stayed "open" in the run's ledger
  for good, since they lived only in that session's brokers. A session starting now marks what the
  previous one left open as canceled ("left open when its session stopped"). Live orders were
  already canceled at the venue on start, by the order journal; their ledger rows now say so too.

## 0.5.0 (2026-10-27)

Upgrading from 0.4: change the bound to `quanty-py>=0.5.0,<0.6`, then `quanty upgrade` (it
brings the recorder's `--take-over` into `compose.yaml`). Recordings made by 0.5 tag each message
with its connection, and 0.4 skips the tagged lines: read them with 0.5, which reads older
recordings as before.

- Fix: Kalshi books went wrong for whole windows after the feed replaced its connection (each time a
  window was added, hourly at :30): the old connection, kept open a few seconds so nothing is
  missed, sent the same deltas as the new one, and both were applied. 3.8 million crossed books in
  8 hours of BTC and XRP 15-minute markets, a quarter of their windows. Every message now carries
  the connection it came on (`Raw.conn`, recorded as `"c"`), and a book follows its snapshot's.
  Recordings from before read as they did.
- Fix: the simulated queue counted contracts twice when a venue sent a book update before the
  trades that caused it: the level shrinking moved a resting order up (cancels ahead), then the
  trades moved it up again. Trades at its price within `match_trades_ms` (500 ms) of a shrink now
  count as what the shrink showed. Over a day of Polymarket US 5m windows this brings simulated
  fills closest to live ones (154 of 166 windows filled-or-not as live). `match_trades_ms = 0` in a
  broker's `queue` keeps the old count, to reproduce a simulator that has it.
- `quanty deploy` replaces the recorder without a gap in the recordings: it used to stop the old
  one, and nothing was recorded for the 20 seconds or so the new container took to start (more on
  a busy server). `quanty record --take-over`, which projects' `compose.yaml` now uses, records
  beside a running recorder into the hour's `<HH>.handover.jsonl.gz` and takes over when it
  stops; reading merges that file in by time. A project gets this with `quanty upgrade` (it
  refreshes `compose.yaml`), or by adding `"--take-over"` to the recorder's command itself.
- The dashboard's open markets drop a market as its countdown ends, rather than at the next update
  (which a slow connection, or a sped-up demo, made seconds or a demo minute late).
- A hosted demo's banner no longer makes a run's page taller than the window.

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
