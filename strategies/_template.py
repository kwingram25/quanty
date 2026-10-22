"""Strategy template: copy to strategies/<name>.py and fill in the TODOs.

A strategy module defines one Strategy subclass. The same file runs in backtest, paper, shadow
and live; it only sees events and only acts through ctx. Hooks you can override: on_start,
on_market_open, on_book, on_trade, on_data, on_clock, on_fill, on_order, on_market_close,
on_settled. See src/quanty/strategies/example_favourite.py for a working example.
"""

from dataclasses import dataclass

from quanty.core import Book, Context, Params, Side, Strategy, Tif


@dataclass(frozen=True)
class MyParams(Params):
    """Knobs an experiment config can override. TODO: replace with your own."""

    min_edge: float = 0.02  # buy when the model beats the price by at least this much
    max_price: float = 0.95  # never pay more than this
    size: int = 1  # contracts per order
    enter_last_s: int = 300  # only trade in the last N seconds of a window


class MyStrategy(Strategy[MyParams]):
    params_type = MyParams
    series = ("TODO_venue:TODO_series",)  # the repeated markets to trade
    description = "TODO: one line on what this strategy does"

    def fair_yes(self, ctx: Context, market_id: str) -> float | None:
        """TODO: your model. Probability that YES wins, or None to skip."""
        return None

    def on_book(self, ctx: Context, book: Book) -> None:
        p = self.params
        mid = book.market_id
        if ctx.seconds_left(mid) > p.enter_last_s or ctx.orders(mid):
            return
        fair = self.fair_yes(ctx, mid)
        if fair is None:
            return
        for side, prob in ((Side.YES, fair), (Side.NO, 1 - fair)):
            price = book.ask(side)
            if price is not None and price <= p.max_price and prob - price >= p.min_edge:
                ctx.buy(mid, side, price, p.size, tif=Tif.IOC)
                return
