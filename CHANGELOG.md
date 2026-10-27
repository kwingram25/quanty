# Changelog

quanty is `quanty-py` on PyPI. While it's at 0.x, a new minor version (0.2, 0.3) may change what
your project relies on, and says so here with what to do: the `Strategy` class and `ctx`,
experiment and feeds files, the ledger's schema (migrated for you when a file is opened) and the
command line. A patch version (0.1.1) never does. Projects from `quanty init` depend on one minor
version (`quanty-py>=0.1.0,<0.2`), so moving to the next is your choice: raise the bound in
`pyproject.toml`, then `quanty upgrade`.

## 0.1.0 (unreleased)

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
