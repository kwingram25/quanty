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

This backtests four variants of the example strategy on 6 hours of synthetic 5-minute up/down
markets (`fav_90_stop` is `fav_90` with a stop loss, to compare), prints a summary, and opens the dashboard on the result. The synthetic markets are
deterministic per `--seed`, so the same command gives the same numbers anywhere.

`quanty demo --live` does the same as a running session instead: it opens on a day of history
(`--history`), then plays the markets at 10x real time (`--speed`; a toggle on the page switches to real
time and back) while the strategies paper trade, so
the dashboard updates as you watch and its controls (halt, resume, kill switch, editing a strategy's
params) work. The markets
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
kill switch), and a strategy's params, such as its size, can be edited from its details without a
restart.

## 6. Shadow trade

Change a broker to `kind = "shadow"`. Fills are still simulated, but every order is also turned
into the exact request the venue would receive and checked: Polymarket US validates it
server-side without placing it; Kalshi requests are built, signed and checked locally. This
catches tick, size and permission problems, and measures real round trips, with no risk.

## 7. Trade live

Read [Live trading](live-trading.md) first.

Paper or live is a property of each instance's broker (`kind = "sim"`, `"shadow"` or `"live"`), so
the same experiment file goes from paper to live one instance at a time. `quanty promote` makes the
edit for you, keeping the file's comments and carrying any dashboard edits to the instance's params:

```sh
uv run quanty promote my_strategy --to shadow     # orders checked by the venue, still simulated fills
uv run quanty promote my_strategy --to live       # asks for max_qty and max_notional, runs doctor
uv run quanty live experiments/main.toml --feeds feeds.toml --data data --arm
```

That adds a broker like this (written by hand, it works the same):

```toml
[brokers.live]
kind = "live"
max_qty = 10          # required: largest single order, contracts
max_notional = 10     # required: largest single order, $
```

Instances left on `sim` keep paper trading in the same live session. `--to sim` steps back; the live
broker block stays in the file, unused, for next time.

**Runs.** Each experiment records into one run per mode: `quanty paper experiments/main.toml` into
*paper/main*, `quanty live …` into *live/main*, so real and simulated results never mix. Their
pages link to each other, and the **Compare** tab puts the same strategies side by side over the
time both ran: fill rate, misses, fills against the limit asked, price paid and P&L per trade. A
live fill rate well below paper's means paper's simulated fills are optimistic for that strategy.
