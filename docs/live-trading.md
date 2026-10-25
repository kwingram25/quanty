# Live trading

Live brokers place real orders with real money. quanty is designed so the expensive mistakes
need several things to go wrong at once. This page is what to know before `--arm`.

## Before the first live run

1. **Paper trade the experiment** until its fills and P&L look like the backtest's. Move an instance
   on with `quanty promote <id> --to shadow`, then `--to live`.
2. **Shadow trade it** (`kind = "shadow"`) so every order is checked by the venue, or built and
   signed locally for Kalshi: tick sizes, quantity steps and permissions are confirmed before
   money is involved.
3. **Place one tiny order by hand through `quanty live`** with small caps, early in a market where
   nothing else of yours trades, and watch the dashboard: placement, fill, cancel, and the
   numbers in the ledger against the venue's own.
4. **Run `quanty doctor`** and fix anything it marks ✗: keys, clock drift, rate limits and orders
   a previous live run left unresolved.
5. **Start small**: `max_qty` and `max_notional` at the size you would not mind getting wrong.

## Layers of protection

| Layer | What it stops |
|---|---|
| `quanty live --arm` | Nothing places orders without it. `paper` and `backtest` refuse live brokers outright. |
| `max_qty`, `max_notional` (required) | A single oversized order, whatever the strategy computes. |
| Risk limits | Positions, open risk, risk per correlation group and daily loss past your limits. |
| `error_limit` | A broker disables itself after this many consecutive venue rejections. |
| Unknown outcomes | If an order's reply never arrives (timeout, dropped connection), the broker disables itself at once and the order stays pending. It is never assumed rejected: the venue may have filled it. |
| Order journal | Each order is written down before it is sent. On start, orders a previous run left resting are canceled; on Kalshi, orders whose reply never came are found by client id (`quanty-…`). Only journaled orders are touched, so other programs trading the same account are safe. |
| Dashboard controls | Halt a strategy (no new buys, exits still allowed), disarm a broker (no orders at all, resting ones canceled), or halt everything; change a strategy's params (validated by the strategy, saved for restarts; limits and broker caps can't be raised from the dashboard, so they still bound a bad value). Actions on a live run need its name typed. |

A disabled broker cannot be re-armed from the dashboard: something unexpected happened, so check
the venue and restart the session.

## Restarts

A session can be stopped and started again (a deploy, a crash, a change to the experiment) and
carry on its run:

- **Positions are restored.** On start, each strategy's open positions and realized P&L (the
  daily loss limit included) are rebuilt from the run's recorded fills and settlements, so a
  strategy that held 100 contracts before the restart knows it still does and doesn't buy again.
- **Order history continues.** Order numbers carry on from the run's last, so nothing earlier is
  overwritten.
- **Resting orders are resolved.** Live brokers read their order journal and cancel what a previous
  session left resting on the venue.
- **Warm-up, if the experiment asks.** With `warmup_s = 14400` at the top of the experiment, a
  session first replays the last four hours of its own recordings with orders off, so models and
  signals that need history start warm. It takes a while for long warm-ups (the replay runs
  through every book update), and it rebuilds what strategies learn from the markets, not what
  they learned from their own fills.

Not yet: comparing restored live positions with what the venue holds. Markets that settled while
no session ran are settled when the venue next reports their result, within its results window.

## What quanty does not do

- **It does not sell your positions in an emergency.** The kill switch halts buying; positions
  stay, and strategies' own exits keep working. Dumping into a thin book can cost more than
  holding to settlement.
- **It does not reconcile positions with the venue on start.** A restart sees its own journal
  and ledger, not orders or positions placed elsewhere.
- **The dashboard isn't public by default.** It listens on localhost: reach it through an SSH
  tunnel, or set a password (`quanty password --write`) and put it behind HTTPS (see
  [Deploying](deploying.md)).

## Venue notes

Checked against the venues' published API specs and with real orders:

- **Kalshi** quotes in YES terms (`bid` / `ask`), counts in hundredths of a contract, and has no
  order preview. A new 15-minute window can take ~30 s to be listed after it opens.
- **Polymarket US** quotes in YES terms too (buying NO at 99¢ is `BUY_SHORT` at 0.01). Rejections
  can arrive inside an HTTP 200 reply as a `REJECTED` execution; an order read right after
  placing can briefly return 404; IOC orders can fill at a better price than their limit. There
  are no client order ids, which is why the journal matters.

Testing brokers without real orders: `quanty.venues.fake.FakeExchange` speaks both venues' order
APIs, matches against books you set, and injects faults (rejects, server errors, timeouts after
placing, lost reads).
