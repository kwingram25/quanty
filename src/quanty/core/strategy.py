from typing import TYPE_CHECKING, ClassVar

from quanty.core.events import Book, Clock, DataEvent, Fill, MarketTrade
from quanty.core.orders import Order
from quanty.core.types import Market, Side
from quanty.core.validate import ConfigModel

if TYPE_CHECKING:
    from quanty.core.engine import Context


class Params(ConfigModel):
    """Base for strategy parameters. Subclass with typed fields and defaults; experiment configs
    override them by name, strictly validated. Field(description=...) shows up in the web UI.
    """


class Strategy[P: Params]:
    """Base class for strategies. Override the hooks you need and act through `ctx`.

    The same strategy runs unchanged in backtest, paper, shadow and live: it only sees events
    and only acts through ctx, and the broker behind ctx decides what an order means.
    """

    params_type: ClassVar[type[Params]] = Params
    series: ClassVar[tuple[str, ...]] = ()  # series to trade ("<venue>:<series>"); empty = all
    description: ClassVar[str] = ""

    def __init__(self, params: P) -> None:
        self.params = params

    def wants(self, market: Market) -> bool:
        return not self.series or market.series in self.series

    def on_start(self, ctx: "Context") -> None: ...
    def on_market_open(self, ctx: "Context", market: Market) -> None: ...
    def on_book(self, ctx: "Context", book: Book) -> None: ...
    def on_trade(self, ctx: "Context", trade: MarketTrade) -> None: ...
    def on_data(self, ctx: "Context", event: DataEvent) -> None: ...
    def on_clock(self, ctx: "Context", clock: Clock) -> None: ...
    def on_fill(self, ctx: "Context", fill: Fill) -> None: ...
    def on_order(self, ctx: "Context", order: Order) -> None: ...
    def on_market_close(self, ctx: "Context", market: Market) -> None: ...
    def on_settled(self, ctx: "Context", market: Market, outcome: Side) -> None: ...
    def on_params(self, ctx: "Context", old: P) -> None:
        """self.params was just changed while running (the dashboard's Edit); old are the previous
        ones. Override if the strategy derived anything from its params when it started."""
