# Extending quanty

quanty ships with Kalshi and Polymarket US (venues), Coinbase and Binance (data feeds), and three
signals. You can add your own of each without changing quanty:

- a **data feed**: anything that streams or serves numbers a strategy wants, such as an exchange's
  prices, a weather station or an economic calendar;
- a **signal**: a value derived from data events, which strategies read by name;
- a **venue**: somewhere to trade, which is its market data (a feed) plus its order API.

Whatever you add gets what the built-in ones have. Every message is recorded exactly as received,
replays come back in order, feed health is reported per minute, the dashboard shows the feed, and
`quanty doctor` checks the venue.

## Where extensions live

An extension is a Python module that defines subclasses of quanty's base classes. quanty finds
them in two places:

- **Your project's `extensions/` folder.** Every `*.py` file in it whose name doesn't start with
  `_` is loaded. It deploys with the project, as `strategies/` does, and it can import helper
  packages kept in the project.
- **An installed package** that declares an entry point in the `quanty.extensions` group, naming a
  module. To share an extension between projects, or publish it:

  ```toml
  # the extension package's pyproject.toml
  [project.entry-points."quanty.extensions"]
  my_venue = "my_venue.quanty"   # a module that defines the classes
  ```

  Then add the package to your project's dependencies (`uv add my-venue`).

A feed kind, venue or signal kind can be defined only once. An extension can't replace a built-in
one, and two extensions can't share a name; quanty stops and names both. An extension that fails
to import also stops quanty, with the file and the error. `quanty feeds list` shows every feed
kind it knows.

## A data feed

A feed has two jobs, kept apart on purpose:

- **`run()` receives and timestamps raw text.** That text is recorded as is.
- **`parse()` turns raw text into events.** It runs live and again on every replay, so a fixed or
  extended parser can be rerun over old recordings.

You rarely write `run()` yourself. `PollingFeed` fetches a URL on a schedule, and `WebSocketFeed`
connects, subscribes and reconnects with backoff. You subclass one and write `parse()`.

This feed polls a JSON endpoint for a weather station's temperature. The URL and reply here are
made up for the example; any source works the same way.

```python
# extensions/station.py
from typing import ClassVar, Literal

import orjson

from quanty.core.events import Event, Observation
from quanty.feeds.base import FeedConfig, PollingConfig, PollingFeed, Raw
from quanty.feeds.parse import iso_ms


class StationConfig(PollingConfig):
    kind: Literal["station"] = "station"
    station: str = "KNYC"
    interval_s: float = 60
    expect_every_s: float | None = 120  # health: longer without a message is a gap


class Station(PollingFeed):
    """Events keyed "station:<id>", e.g. "station:KNYC", in degrees F."""

    kind = "station"
    config_type: ClassVar[type[FeedConfig]] = StationConfig
    config: StationConfig

    def request(self) -> tuple[str, dict[str, str]]:
        return "https://weather.example.com/latest", {"station": self.config.station}

    def parse(self, raw: Raw) -> list[Event]:
        # a reply: {"station": "KNYC", "temp_f": 61.5, "observed": "2026-10-27T14:51:00Z"}
        msg = orjson.loads(raw.text)
        return [
            Observation(
                ts=raw.t,
                src_ts=iso_ms(msg["observed"]),
                key=f"station:{msg['station']}",
                value=float(msg["temp_f"]),
                unit="F",
            )
        ]
```

Then name it in `feeds.toml`, with whatever its config takes:

```toml
[feeds.nyc_temp]
kind = "station"
station = "KNYC"
```

`quanty record --config feeds.toml` records it, `quanty paper` runs strategies on it, and a
strategy reads it with `ctx.latest("station:KNYC")`.

What a parser must do:

- **Set `ts=raw.t` on every event.** That's the receive time, and replays depend on it. Set
  `src_ts` to the source's own timestamp, if it has one: feed health measures lag from it.
- **Key data events `"<feed>:<instrument>"`.** Choose from the data event types in
  `quanty.core.events`: `Quote`, `Trade`, `IndexValue`, `Observation`, `Forecast`, `Release`,
  `Status`.
- **Raise on a malformed message.** It's counted as a parse error in feed health, not dropped
  silently. Return `[]` for messages that carry nothing, such as heartbeats.
- **Never read the clock, the network or randomness.** A replay has to give the same events.
  State is fine (an order book built from deltas, say): recordings are parsed in order by one feed
  instance, and `on_link()` hears about reconnects in the same order.

Optional extras on the class:

- `parser_version` (bump it when `parse()` changes what it emits);
- `replayable = False` (for a source that can't be backfilled);
- `carries_data()` (a cheap text test, so a warm-up can skip messages that hold no data);
- for a WebSocket: `subscriptions()`, `keep()` (drop protocol noise before recording) and
  `headers()` (auth).

The built-in feeds in `src/quanty/feeds/` (`coinbase.py`, `binance.py`) are short, complete
examples.

To test a feed, parse a few saved raw lines. No network is needed:

```python
from quanty import feeds
from quanty.feeds.base import Raw


def test_station_parses_a_reply() -> None:
    feed = feeds.make("nyc_temp", {"kind": "station"})
    (ev,) = feed.parse(
        Raw("nyc_temp", 1_000, '{"station": "KNYC", "temp_f": 61.5, "observed": "2026-10-27T14:51:00Z"}')
    )
    assert (ev.key, ev.value, ev.ts) == ("station:KNYC", 61.5, 1_000)
```

## A signal

A signal updates from data events and holds a `value` that strategies read with
`ctx.signal(name)`. It must update only from event timestamps, never the wall clock, so a replay
rebuilds the same values.

```python
# extensions/signals.py
from typing import Literal

from pydantic import Field

from quanty.core.events import DataEvent
from quanty.signals import Signal, SignalConfig, price_of


class ChangeConfig(SignalConfig):
    kind: Literal["change"] = "change"
    since_s: float = Field(300, gt=0, description="Compare with the price this long ago")


class Change(Signal):
    """The price now minus the last price at least since_s ago."""

    config_type = ChangeConfig

    def __init__(self, config: ChangeConfig) -> None:
        super().__init__(config)
        self.since = int(config.since_s * 1000)
        self.prices: list[tuple[int, float]] = []

    def update(self, ev: DataEvent) -> None:
        p = price_of(ev)
        if p is None:
            return
        self.prices.append((ev.ts, p))
        while len(self.prices) > 1 and self.prices[1][0] <= ev.ts - self.since:
            self.prices.pop(0)
        start_ts, start = self.prices[0]
        self.value = p - start if start_ts <= ev.ts - self.since else None
```

An experiment adds it like a built-in one; the `kind` picks the class:

```toml
[[signals]]
kind = "change"
name = "btc_5m"
key = "coinbase:BTC-USD"
since_s = 300
```

## A venue

A venue is two classes, linked by one name, such as `"exchange"`:

- **Market data:** a `VenueFeed` whose `kind` is the venue's name. It discovers markets and streams
  their books, trades and results, recorded in one stream.
- **Orders:** an `OrderAPI` whose `venue` is that name. The shadow and live brokers use it for
  markets whose `Market.venue` is the name, and `quanty doctor` checks it.

Market ids are `"<venue>:<the venue's id>"` and series are `"<venue>:<series>"`, so a strategy
names its series like `"exchange:BTC-DAILY"`.

### Market data

Subclass `quanty.venues.base.VenueFeed`. It runs discovery over REST and keeps one WebSocket
subscribed to the markets in play, replacing the connection (with an overlap) when that set
changes. You write:

- **`discover(client, sink)`**, called every second. Fetch whatever is due (new markets, results
  of closed ones) and pass each reply to `sink(self.rest("markets", body, **params))`, so it's
  recorded in the stream. Update `self.windows` (venue id → `Window(id, open_ts, close_ts)`), which
  decides what's subscribed.
- **`subscribe_messages(ids)`**: the messages that subscribe to those markets. Optionally,
  `update_in_place()` changes subscriptions on the open connection instead of reconnecting.
- **`parse(raw)`**, for both the REST replies you recorded and the WebSocket messages. It turns
  them into:
  - `MarketOpen(market=Market(...))` when a market appears, with its `venue`, `series`, open and
    close times, `tick`, `qty_step` and `fee` (a `quanty.core.fees` model). Send it again to update
    the market's `terms`, such as a strike set at the open.
  - `Book(market_id, bids, asks)`: the whole book, best first, `(price, size)` in YES terms. If
    the venue sends deltas, rebuild the book in the parser.
  - `MarketTrade` for trades, `MarketClose` at the close, and `Settled(market_id, outcome)` when
    the result is known.

`src/quanty/venues/kalshi.py` and `polymarket_us.py` are the full examples, including series
discovery, book rebuilding with sequence checks, and result polling.

### Orders

Subclass `quanty.venues.orders.OrderAPI`:

| Member | What it does |
|---|---|
| `venue`, `label`, `base_url` | the name markets carry, a display name, the REST base URL |
| `signer` | an object with `headers(method, path)`: the auth headers for one request. Read keys from the environment or `.env` (pydantic-settings does both); raise `RuntimeError` when they're missing |
| `key_env` | the `.env` name of the key, so `quanty doctor` can say where it goes |
| `place_request(order, market)` | the exact request an order becomes: `VenueRequest(method, path, body)` |
| `preview(client, order, market)` | for the shadow broker: check that request without placing it (on the venue if it can validate, otherwise locally) and return a `Preview` |
| `place(client, req)` | send it; return a `VenueOrder`. Raise `VenueError(status, detail)` when the venue refuses it. Let an `httpx` error propagate when the reply never came: the order's outcome is unknown, and the live broker stops |
| `get(client, venue_id, market_id)`, `cancel(...)` | an order's state now (`VenueOrder`: filled, fees, done), and canceling it |
| `balance(client)` | the account's money (`AccountBalance`), for the dashboard and `doctor` |
| `holdings(client, held)` | open positions (`Holdings`), for a live session's check of what it holds |
| `resting_by_client_id(client)` | optional: lets a restarted live run find orders a crash left behind |
| `key_warning()`, `limits(client)` | optional: a warning about keys that do sign, and a line on rate limits, for `doctor` |

`_send(client, method, path, json=...)` signs and sends a request and raises `VenueError` on a
refusal, so most methods are a few lines.

You don't write the safety rules; the live broker applies them to any venue:
- it places nothing unless the session is armed;
- every order is capped by `max_qty` and `max_notional`;
- orders are journaled before they're sent;
- it stops itself on repeated refusals, or when an order's outcome is unknown.

Your part is to keep `place()`'s contract: a refusal raises `VenueError`, and an unknown outcome
raises an `httpx` error.

### Testing a venue

Never test against the real order endpoints. Give the client an `httpx.MockTransport` that answers
like the venue does. `src/quanty/venues/fake.py` is a fake exchange speaking both built-in venues'
order routes, with injected faults (refusals, errors, lost replies), and it's the pattern to copy.
`VenueFeed` has a `transport` attribute for the same purpose in discovery tests.

```python
import asyncio

import httpx

from quanty.venues.orders import default_apis


def test_balance() -> None:
    def venue(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/balance"
        return httpx.Response(200, json={"cash": 12.5})

    api = default_apis()["exchange"]

    async def go() -> float:
        async with httpx.AsyncClient(transport=httpx.MockTransport(venue)) as client:
            return (await api.balance(client)).cash

    assert asyncio.run(go()) == 12.5
```

Then run the venue the way the built-in ones went live:
1. `quanty record` for a day, and check its health;
2. a backtest on what it recorded;
3. paper, then shadow, so every order is checked by `preview()`;
4. then one small live order, watched, with low caps ([Live trading](live-trading.md)).
