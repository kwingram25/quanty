# Getting started

From a fresh clone to a strategy trading live, one step at a time. Each step is useful on its own;
stop wherever you like.

**Requirements:** Python 3.12+ and [uv](https://docs.astral.sh/uv/). Recording and trading on
Kalshi or Polymarket US need API keys; everything up to step 4 doesn't.

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

## 2. Start your project

Your strategies, configs and keys live in a project of your own, not in this clone:

```sh
uv run quanty init ~/code/my-quanty     # a few questions (strategy name, venues, keys); --yes for defaults
cd ~/code/my-quanty
git init && git add -A && git commit -m "my quanty project"   # .env and data/ are already ignored
```

It holds `strategies/<name>.py` (a skeleton aimed at your first series), `experiments/main.toml`,
`feeds.toml`, `.env` (your keys, readable only by you) and the files to deploy it. `uv run quanty`
works in it with nothing installed first, and every command below runs there. Keep the repo
private and push it somewhere backed up: your strategies are your edge. quanty itself is just a
dependency of it.

## 3. Run an experiment

An experiment is a TOML file: which strategies, with which parameters, on which brokers, under
which risk limits.

```sh
uv run quanty backtest experiments/main.toml --ledger data/backtest/first.sqlite
uv run quanty ui --data data           # the run is now on the dashboard
```

Without `--data`, a backtest runs on built-in synthetic markets: 5-minute up/down windows. A
strategy written for a venue's series doesn't trade there (the backtest says so);
`uv run quanty demo --strategy <name>` points it at the synthetic markets to try its logic, and
step 5 backtests on real recordings.

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

## 4. Write your own strategy

Your strategy is `strategies/<name>.py` in the project (fill in the TODOs; more strategies are more
files there). See [Writing strategies](strategies.md).

```sh
uv run quanty list                      # it's found (an import error shows here)
uv run quanty params <name>             # and its params
uv run quanty demo --strategy <name>    # a quick run on synthetic markets
```

To share one private strategy library between several projects, point quanty at its folder in your
user config. That works on your machine; a deployed project only carries its own `strategies/`, so
copy in what a server should run.

```toml
# ~/.config/quanty/config.toml
strategy_paths = ["~/code/my-strategies/strategies"]
```

## 5. Record real data

Public crypto feeds need no keys:

```sh
uv run quanty record --feed coinbase --feed binance --data data      # Ctrl-C to stop
uv run quanty feeds health --data data
```

For Kalshi and Polymarket US, your keys are in the project's `.env` (`quanty init` asked for them;
`uv run quanty doctor` checks them) and its `feeds.toml` chooses the series:

```sh
uv run quanty record --config feeds.toml --data data
```

Every message is stored exactly as received (`data/<feed>/<date>/<hour>.jsonl.gz`), with receive
timestamps. Replays parse the same bytes in the same order, so a backtest on recordings sees what
a live run would have seen:

```sh
uv run quanty replay --data data --show 5
uv run quanty backtest experiments/main.toml --data data --start "2026-10-21 00:00" --end "2026-10-22 00:00"
```

## 6. Paper trade

```sh
uv run quanty paper experiments/main.toml --feeds feeds.toml --data data --ui
```

Strategies run in real time on live feeds; fills are simulated by the same broker model as
backtests. Recording continues alongside, and the ledger is written as it goes
(`data/session/<experiment>.sqlite`). The dashboard shows it live and adds controls (halt, resume,
kill switch), and a strategy's params, such as its size, can be edited from its details without a
restart.

## 7. Shadow trade

Change a broker to `kind = "shadow"`. Fills are still simulated, but every order is also turned
into the exact request the venue would receive and checked: Polymarket US validates it
server-side without placing it; Kalshi requests are built, signed and checked locally. This
catches tick, size and permission problems, and measures real round trips, with no risk.

## 8. Trade live

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

**Without a restart.** Start the session armed, with the live broker defined even if nothing uses it
yet (`quanty promote` writes one, or add it by hand): `quanty live experiments/main.toml --feeds
feeds.toml --data data --arm`. Then switch a paper strategy live while it runs, from its details on
the dashboard (**Go live on …**, with the run's name typed to confirm) or from a terminal on the
same machine:

```sh
uv run quanty promote my_strategy --to live --now
```

It takes real orders from the next market it enters; markets it's already in finish on paper, so a
simulated position is never sold for real (or the other way). Its paper twin starts beside it, and
the experiment file is updated (in a container, where the image's copy is replaced on deploy, the
switch is kept with the run instead and reapplied when the session restarts). **Back to paper**
works the same way.

**Many variants.** A `grid` in an `[[instances]]` entry makes one instance per combination:

```toml
[[instances]]
id = "fav_{min_conf}_{enter_last_s}"     # placeholders name each one; without them, values are appended
strategy = "example_favorite"
grid = { min_conf = [0.9, 0.95, 0.97], enter_last_s = [30, 60] }   # six instances
```

Add, change or remove variants in the file while a session runs, then `uv run quanty reload` (or
**Reload experiment** on the dashboard): new instances start, removed ones retire (no new entries;
what they hold finishes), changed params and brokers apply. New brokers, limits and feeds, and
edited strategy code, take a restart. The **Variants** tab ranks every strategy for promotion by what
it likely makes per trade at worst: the win rate's lower ~95% bound times its average win, the
rest at its average loss, or a position's whole cost before any loss. A run of wins on near-certain
bets can't rule out the rare loss, so few trades rank low however well they went. **Backtest
variants** there tries values of one strategy's params before you add them: it replays the last
hours of recordings with the strategy as it runs now and a copy per combination (simulated, in
the background, one at a time), and the result opens as a backtest run, ranked the same way. Its
experiment is saved beside the result (`data/backtest/<name>.toml`), so it can be rerun by hand.

**Runs.** Each experiment records into one run, *session/main* for `experiments/main.toml`, whether
started with `quanty paper` or `quanty live`: the command only decides whether live brokers may be
armed. Every order, fill and settlement records whether it was real money, so a strategy keeps one
history from its paper days into its live ones, and the dashboard never adds the two: real-money
and simulated P&L are separate totals, strategies carry LIVE / shadow / paper badges, and the
Trades and Orders tabs filter by either.

Going live, `quanty promote` keeps a **paper twin** beside the strategy (`<id>_paper`, `twin_of =
"<id>"`): the same strategy and params on the simulated broker, in the same session. The
**Compare** tab puts the two side by side over the time both traded: fill rate, misses, fills
against the limit asked, price paid and P&L per trade. Both saw the same markets at the same
moments, so a live fill rate well below the twin's means paper's simulated fills are optimistic for
that strategy. `--no-twin` skips it; stepping back to paper removes it.
