from collections.abc import Callable
from typing import Any

from quanty.brokers.sim import SimBroker
from quanty.core import (
    Book,
    Clock,
    Context,
    DataEvent,
    Engine,
    FeeModel,
    Fill,
    Instance,
    Limits,
    Market,
    MarketOpen,
    MarketTrade,
    Order,
    Params,
    Side,
    Strategy,
)

M = Market(id="t:M1", venue="t", series="t:s", open_ts=0, close_ts=600_000, group="g")
M2 = Market(id="t:M2", venue="t", series="t:s", open_ts=0, close_ts=600_000, group="g")

Hook = Callable[..., None]


class Hooks(Strategy[Params]):
    """A strategy whose hooks are functions set by the test; it records what it saw."""

    def __init__(self, **hooks: Hook) -> None:
        super().__init__(Params())
        self.hooks = hooks
        self.seen: list[tuple[str, Any]] = []

    def _call(self, name: str, ctx: Context, arg: Any) -> None:
        self.seen.append((name, arg))
        if name in self.hooks:
            self.hooks[name](ctx, arg)

    def on_book(self, ctx: Context, book: Book) -> None:
        self._call("book", ctx, book)

    def on_trade(self, ctx: Context, trade: MarketTrade) -> None:
        self._call("trade", ctx, trade)

    def on_data(self, ctx: Context, event: DataEvent) -> None:
        self._call("data", ctx, event)

    def on_clock(self, ctx: Context, clock: Clock) -> None:
        self._call("clock", ctx, clock)

    def on_fill(self, ctx: Context, fill: Fill) -> None:
        self._call("fill", ctx, fill)

    def on_order(self, ctx: Context, order: Order) -> None:
        self._call("order", ctx, order.status)

    def on_settled(self, ctx: Context, market: Market, outcome: Side) -> None:
        self._call("settled", ctx, outcome)

    def fills(self) -> list[Fill]:
        return [a for n, a in self.seen if n == "fill"]

    def statuses(self) -> list[str]:
        return [str(a) for n, a in self.seen if n == "order"]


def make(
    *strategies: Hooks,
    latency_ms: int = 0,
    fees: dict[str, FeeModel] | None = None,
    limits: Limits | None = None,
    account: Limits | None = None,
    clock_ms: int | None = None,
    strict: bool = True,
) -> Engine:
    broker = SimBroker(latency_ms=latency_ms, fees=fees)
    instances = [Instance(f"s{i}", s, broker, limits or Limits()) for i, s in enumerate(strategies)]
    return Engine(instances, limits=account, clock_ms=clock_ms, strict=strict)


def opened(*markets: Market, ts: int = 0) -> list[MarketOpen]:
    return [MarketOpen(ts=ts, market=m) for m in markets or (M,)]


def book(ts: int, bids: list[tuple[float, int]], asks: list[tuple[float, int]], market: Market = M) -> Book:
    return Book(ts=ts, market_id=market.id, bids=tuple(bids), asks=tuple(asks))


def once(action: Hook) -> Hook:
    """Wrap a hook so it only acts the first time it is called."""
    done = False

    def hook(ctx: Context, arg: Any) -> None:
        nonlocal done
        if not done:
            done = True
            action(ctx, arg)

    return hook
