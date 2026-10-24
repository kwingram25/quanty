# Writing strategies

A strategy is one Python file with one `Strategy` subclass. It only sees events and only acts
through a context, so the same file runs unchanged in backtest, paper, shadow and live: the broker
behind the context decides what an order means.

## The shape

```python
from pydantic import Field

from quanty.core import Book, Context, Market, Params, Side, Strategy, Tif


class EdgeParams(Params):
    min_edge: float = Field(0.02, ge=0, description="Buy when the model beats the price by this much")
    size: int = Field(10, gt=0, description="Contracts per order")


class Edge(Strategy[EdgeParams]):
    params_type = EdgeParams
    series = ("kalshi:KXHIGHNY",)  # markets to trade; empty = everything
    description = "Buy whichever side the model prices above the ask"

    def __init__(self, params: EdgeParams) -> None:
        super().__init__(params)
        self.tried: set[str] = set()  # markets already bought in, filled or not

    def on_book(self, ctx: Context, book: Book) -> None:
        mid = book.market_id
        if mid in self.tried:  # once per market
            return
        fair = self.model(ctx, mid)  # P(YES), your code
        for side, prob in ((Side.YES, fair), (Side.NO, 1 - fair)):
            ask = book.ask(side)
            if ask is not None and prob - ask >= self.params.min_edge:
                self.tried.add(mid)
                ctx.buy(mid, side, ask, self.params.size, tif=Tif.IOC)
                return

    def on_market_close(self, ctx: Context, market: Market) -> None:
        self.tried.discard(market.id)
```

State on `self` is fine: each run builds a fresh instance, and nothing on `self` survives a restart.
Track "already tried" yourself, as above, when it matters: `ctx.orders()` lists open orders only,
so an IOC that missed or an order that was rejected leaves nothing there.

`Params` are Pydantic models: typed, with defaults and descriptions, validated strictly when an
experiment overrides them. `quanty params <strategy>` lists them.

## Hooks

Override only what you need. Every hook gets the context first.

| Hook | When |
|---|---|
| `on_start(ctx)` | once, before any event |
| `on_market_open(ctx, market)` | a market in your series is announced (terms, strike, close time) |
| `on_book(ctx, book)` | the top of a book changed |
| `on_trade(ctx, trade)` | a trade printed on the venue |
| `on_data(ctx, event)` | external data: index values, prices, observations, forecasts, releases |
| `on_clock(ctx, clock)` | every `clock_ms` of event time, if the experiment sets it |
| `on_fill(ctx, fill)` | one of your orders filled (partly or fully) |
| `on_order(ctx, order)` | one of your orders changed status (open, filled, canceled, rejected, expired) |
| `on_market_close(ctx, market)` | trading in a market ended |
| `on_settled(ctx, market, outcome)` | the market resolved; positions are paid out |
| `on_params(ctx, old)` | `self.params` was just changed from the dashboard; `old` are the previous ones |

Params can be changed while a session runs (the dashboard's Edit, on a strategy's details), and
take effect from the next event. An edit is kept for that run (a restart keeps it) but doesn't touch
your files until you press **Save to file**, which writes it into the instance's
`[instances.params]` in the experiment file, comments kept, so backtests use it too. A session in a
container image offers **Copy for the file** instead: paste the lines into the file on your
machine and deploy, since a deploy replaces the image's copy. Read `self.params` when deciding, as above, rather than copying
values at startup; if you do derive something from them, recompute it in `on_params`.

Events arrive in time order. In a backtest that order is the recording's; live it is arrival
order, with receive timestamps.

## The context

| | |
|---|---|
| `ctx.now` | current event time, ms |
| `ctx.markets()`, `ctx.market(id)` | open markets in your series |
| `ctx.seconds_left(id)` | until the market closes |
| `ctx.book(id)` | latest book: `bids`, `asks` as `(yes_price, size)` levels; `book.ask(side)` is the price to buy that side now (NO: 1 − the best YES bid), `book.bid(side)` to sell it; top of book, None when empty |
| `ctx.latest(key)` | latest data event for a key, e.g. `"cfbenchmarks:BRTI"` |
| `ctx.signal(name)` | a derived value declared in the experiment (`last`, `ewma`, `realized_vol`) |
| `ctx.position(id, side)` | contracts held |
| `ctx.entry_price(id, side)` | average cost per contract held, buy fees included (None when not holding) |
| `ctx.outcomes(id)` | the open markets of the same question, when at most one can win |
| `ctx.orders(id=None)` | your open orders (filled, canceled and rejected ones are gone) |
| `ctx.buy(id, side, price, qty, tif=, post_only=, tag=)` | place a buy; returns the `Order` (`.status`, `.reason`) |
| `ctx.sell(...)` | sell contracts you hold (no shorting: to bet against YES, buy NO) |
| `ctx.cancel(order)`, `ctx.cancel_all(id=None)` | |
| `ctx.log(msg, **fields)` | into the ledger, shown in the dashboard |
| `ctx.estimate(id, yes)` | your model's probability of YES, shown on the market's card beside the market's own chance (display only) |

Prices are always in the terms of the side you name: buying NO at 0.97 means paying 97¢ for a
contract that pays $1 if NO wins. Venues that quote everything in YES terms are converted for you.

An order that fails a check (risk limit, tick size, quantity step, market closed, halted) comes
back already `REJECTED` with a reason; it never reaches the venue. A strategy that raises is
halted and logged; in backtests the error is raised so you see it.

## Any binary market

quanty trades binary contracts: each market pays $1 if YES wins. That covers up/down windows,
weather and economic ranges, sports, anything a venue lists as yes/no. A question with several
outcomes (a temperature ladder, CPI ranges) is a set of binary markets sharing an event, as Kalshi
models it.

`market.describe()` says what a market is, from its `terms`. Venues fill in what they know, and
the dashboard uses it to title cards, group outcomes into events and show the value a market settles
against:

| term | meaning |
|---|---|
| `title` | the question ("Highest temperature in NYC on Oct 24?") |
| `subtitle` | this contract's outcome or range ("72° to 73°") |
| `event` | markets sharing it are outcomes of one question |
| `strike` | the threshold, where there is one |
| `reference` | the data key the market settles against (e.g. `cfbenchmarks:BRTI`) |
| `unit` | the reference's unit ("°F") |
| `exclusive` | at most one market of the event can win (a ladder, a list of candidates) |
| `exhaustive` | with `exclusive`: exactly one wins (the outcomes cover everything) |

For exclusive events, risk limits measure what could really be lost: the worst case across the
possible winners, not the sum of what each outcome cost. Holding every bucket of a ladder risks only
what was overpaid in total. `ctx.outcomes(market_id)` lists the open markets of the same question,
for strategies that price outcomes together, and the dashboard shows each strategy's worst and best
case per question. The synthetic ladder sets both terms; venue adapters set them where the venue says
so (not yet for Kalshi, whose events carry the flag but aren't fetched; until then its markets are
risk-checked one by one, which overstates rather than understates risk).

`quanty demo --live` includes an hourly temperature ladder traded by
`src/quanty/strategies/example_bucket.py`, next to the up/down example.

## Orders

- `Tif.IOC`: take what is there now, cancel the rest. The usual choice for taking liquidity.
- `Tif.GTC` (default): rest until filled, canceled, or the market closes.
- `post_only=True`: rejected rather than taking liquidity, for making markets.

## Exits and stops

There is no stop order type: a strategy exits by selling what it holds, so a stop loss is a few
lines in the strategy, and it works the same in every mode. Selling still works while a strategy is
halted (only buys stop), so a halted strategy can still get out.

**A model stop** exits when the strategy's own model stops favoring what it holds. It needs no
entry price, and it's what `example_favorite` does with `stop_conf`:

```python
def on_book(self, ctx: Context, book: Book) -> None:
    mid = book.market_id
    if mid in self.stopped:  # stopped out: don't buy back in
        return
    for side in (Side.YES, Side.NO):
        held = ctx.position(mid, side)
        fair = self.fair_yes(ctx, mid)  # your model's P(YES)
        if held and fair is not None and (fair if side is Side.YES else 1 - fair) < self.params.stop_conf:
            self.stopped.add(mid)
            ctx.sell(mid, side, 0.01, held, tif=Tif.IOC, tag="stop")  # into the bids, down to 1¢
            return
    ...  # entries
```

**A price stop** exits when the price to sell falls some way below what was paid:

```python
held, paid, bid = ctx.position(mid, side), ctx.entry_price(mid, side), book.bid(side)
if held and paid is not None and bid is not None and bid <= paid - self.params.stop:
    ctx.sell(mid, side, bid, held, tif=Tif.IOC, tag="stop")
```

Tag stop sells `tag="stop"`, as above: the dashboard then shows them as stop losses (a **Stopped**
outcome in Trades, "stopped out of" in the tape, a count on the strategy, and every one under
Orders' *Needs a look*).

Things to know:

- **No exit price is guaranteed.** An IOC sell takes the bids that are there and cancels the rest:
  a thin book fills part or none. Sell at a low limit (as the model stop does) to take whatever is
  there, or check `ctx.position` again on the next book and retry.
- **Prices jump near the close.** A contract can go from 60¢ to 5¢ in seconds, so a stop often
  fills well below its trigger, or the market closes first.
- **Remember what you stopped out of** (a set, cleared in `on_market_close`), or the entry logic
  buys straight back in.
- **Stops trade average for tail.** On the synthetic markets, `example_favorite` at `min_conf` 0.9
  with `stop_conf` 0.7 made less on an average day ($15.61 against $23.37 over 10 days) but its
  worst day was −$3.41 instead of −$48.67. `quanty demo --ui` runs both side by side
  (`fav_90` and `fav_90_stop`); decide on recordings, not on synthetic data.

## Signals and external data

Declare derived values in the experiment; they are rebuilt identically in replays:

```toml
[[signals]]
name = "btc_vol"
kind = "realized_vol"
key = "coinbase:BTC-USD"
window_s = 300
```

and read them with `ctx.signal("btc_vol")` (None until there is enough data).

## Testing a strategy

- `quanty demo --strategy my_idea` runs it on synthetic markets, in seconds and without keys. A
  strategy written for a venue's series is pointed at the synthetic up/down series for the run.
  In an experiment, `series = ["synthetic:updown_5m"]` in an `[[instances]]` entry does the same.
- `--ledger data/backtest/<name>.sqlite` keeps the run; `quanty ui --data data` then shows every
  trade with its outcome, or query its `orders`, `fills`, `settlements` and `markets` tables.
- `quanty backtest experiment.toml --data data` runs it on your recordings.
- The simulated broker models latency, taker fills against the book, queue position for resting
  orders and post-only rejects. `QueueModel` options make it more or less conservative.
- In unit tests, build events by hand and run an `Engine` directly; `tests/helpers.py` shows how.
