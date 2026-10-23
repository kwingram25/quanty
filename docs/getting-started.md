# Getting started

From a fresh clone to a strategy trading live, one step at a time. Each step is useful on its own;
stop wherever you like.

**Requirements:** Python 3.12+ and [uv](https://docs.astral.sh/uv/). Recording and trading on
Kalshi or Polymarket US need API keys; everything up to step 3 doesn't.

## 1. See it work

```sh
git clone https://github.com/kwingram25/quanty && cd quanty
uv sync
uv run quanty demo --ui
```

This backtests three variants of the example strategy on 6 hours of synthetic 5-minute up/down
markets, prints a summary, and opens the dashboard on the result. The synthetic markets are
deterministic per `--seed`, so the same command gives the same numbers anywhere.

`quanty demo --live` does the same as a running session instead: it opens on a day of history
(`--history`), then plays the markets at 10x real time (`--speed`; a toggle on the page switches to real
time and back) while the strategies paper trade, so
the dashboard updates as you watch and its controls (halt, resume, kill switch) work. The markets
are still synthetic. "New day" starts over on a freshly generated day; the header shows its seed, and
`quanty demo --live --seed N` replays it.

```sh
uv run quanty demo --hours 24 --seed 3 --param min_conf=0.97   # one variant, your parameters
uv run quanty params example_favorite                          # what you can change
```

## 2. Run an experiment

An experiment is a TOML file: which strategies, with which parameters, on which brokers, under
which risk limits.

```sh
uv run quanty backtest experiments/example.toml --hours 24 --ledger data/backtest/example.sqlite
uv run quanty ui --data data           # the run is now on the dashboard
```

```toml
clock_ms = 1000                # on_clock every second (optional)

[limits]                       # account-wide, across all instances
max_open_risk = 200

[brokers.sim]
kind = "sim"                   # sim | shadow | live
latency_ms = 50

[[instances]]
id = "fav_90"
strategy = "example_favorite"
[instances.params]
min_conf = 0.90
[instances.limits]             # this instance only
max_order_qty = 10
```

Configs are validated strictly: a misspelled parameter or limit is an error that names the
field, never a silent default. Risk limits: `max_order_qty`, `max_position`, `max_open_risk`
(worst-case loss, netted across the outcomes of one question), `max_group_risk` (per correlation
group, e.g. all markets on one index) and `max_daily_loss`.

## 3. Write your own strategy

```sh
cp strategies/_template.py strategies/my_idea.py      # gitignored
```

See [Writing strategies](strategies.md). Keep strategies you care about out of this repo
entirely by pointing quanty at your own folder:

```toml
# ~/.config/quanty/config.toml
strategy_paths = ["~/code/my-strategies/strategies"]
```

## 4. Record real data

Public crypto feeds need no keys:

```sh
uv run quanty record --feed coinbase --feed binance --data data      # Ctrl-C to stop
uv run quanty feeds health --data data
```

For Kalshi and Polymarket US, copy `.env.example` to `.env`, add your keys, and copy
`feeds.example.toml` to `feeds.toml` to choose series:

```sh
uv run quanty record --config feeds.toml --data data
```

Every message is stored exactly as received (`data/<feed>/<date>/<hour>.jsonl.gz`), with receive
timestamps. Replays parse the same bytes in the same order, so a backtest on recordings sees what
a live run would have seen:

```sh
uv run quanty replay --data data --show 5
uv run quanty backtest experiment.toml --data data --start "2026-10-21 00:00" --end "2026-10-22 00:00"
```

## 5. Paper trade

```sh
uv run quanty paper experiment.toml --feeds feeds.toml --data data
```

Strategies run in real time on live feeds; fills are simulated by the same broker model as
backtests. Recording continues alongside, and the ledger is written as it goes
(`data/paper/<experiment>.sqlite`). The dashboard shows it live and adds controls (halt, resume,
kill switch).

## 6. Shadow trade

Change a broker to `kind = "shadow"`. Fills are still simulated, but every order is also turned
into the exact request the venue would receive and checked: Polymarket US validates it
server-side without placing it; Kalshi requests are built, signed and checked locally. This
catches tick, size and permission problems, and measures real round trips, with no risk.

## 7. Trade live

Read [Live trading](live-trading.md) first. Then:

```toml
[brokers.real]
kind = "live"
max_qty = 10          # required: largest single order, contracts
max_notional = 10     # required: largest single order, $
```

```sh
uv run quanty doctor     # config, keys, clock, each venue: fix anything marked ✗ first
uv run quanty live experiment.toml --feeds feeds.toml --data data --arm
```
