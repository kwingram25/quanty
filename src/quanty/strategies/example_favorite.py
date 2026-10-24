"""Example strategy: buy the favorite late in a window when a model says it is likely to win.

This shows the shape of a strategy, not a tuned edge. It trades the synthetic up/down series
(`quanty demo`), and its model is the same log-normal formula the synthetic markets are built on.
"""

from pydantic import Field

from quanty.core import Book, Context, IndexValue, Market, Params, Side, Strategy, Tif
from quanty.synthetic import p_up


class FavoriteParams(Params):
    min_conf: float = Field(0.9, gt=0.5, lt=1, description="Model probability the favorite wins")
    max_price: float = Field(0.95, gt=0, lt=1, description="Never pay more than this")
    min_edge: float = Field(
        0.0, ge=0, lt=1, description="Only buy when the model's probability beats the price by this much"
    )
    enter_last_s: int = Field(60, gt=0, description="Only buy in the last N seconds of a window")
    size: int = Field(10, gt=0, description="Contracts per window")
    vol_per_s: float = Field(0.0005, gt=0, description="Model input: index log-return sd per second")
    stop_conf: float | None = Field(
        None, gt=0, lt=1, description="Sell out if the model's probability for the side held drops below this"
    )


class FavoriteLate(Strategy[FavoriteParams]):
    params_type = FavoriteParams
    series = ("synthetic:updown_5m", "synthetic:updown_15m", "synthetic:updown_1h")
    description = "Buy the favorite under a price cap late in the window (example)"

    def __init__(self, params: FavoriteParams) -> None:
        super().__init__(params)
        self.stopped: set[str] = set()  # markets stopped out of: never bought into again

    def on_book(self, ctx: Context, book: Book) -> None:
        p = self.params
        mid = book.market_id
        left = ctx.seconds_left(mid)
        if left > 0 and (fair := self.fair_yes(ctx, mid, left)) is not None:
            ctx.estimate(mid, fair)  # the model's view, shown beside the price in the dashboard
        if mid in self.stopped or self.stop(ctx, mid, left):
            return
        if not 0 < left <= p.enter_last_s or ctx.orders(mid):
            return
        if ctx.position(mid, Side.YES) or ctx.position(mid, Side.NO):
            return
        fair_yes = self.fair_yes(ctx, mid, left)
        if fair_yes is None:
            return
        side = Side.YES if fair_yes >= 0.5 else Side.NO
        conf = max(fair_yes, 1 - fair_yes)
        price = book.ask(side)
        # the edge check: never pay more than the model says the contract is worth
        if conf >= p.min_conf and price is not None and price <= p.max_price and conf - price >= p.min_edge:
            ctx.buy(mid, side, price, p.size, tif=Tif.IOC, tag="favorite")

    def stop(self, ctx: Context, mid: str, left: float) -> bool:
        """A model stop: once the model no longer favors what we hold enough, sell all of it into
        the bids (IOC down to 1¢: whatever is there). True when it sold."""
        if self.params.stop_conf is None or left <= 0:
            return False
        held = next((s for s in Side if ctx.position(mid, s) > 0), None)
        fair = self.fair_yes(ctx, mid, left) if held else None
        if held is None or fair is None or (fair if held is Side.YES else 1 - fair) >= self.params.stop_conf:
            return False
        self.stopped.add(mid)
        ctx.sell(mid, held, 0.01, ctx.position(mid, held), tif=Tif.IOC, tag="stop")
        return True

    def on_market_close(self, ctx: Context, market: Market) -> None:
        self.stopped.discard(market.id)

    def fair_yes(self, ctx: Context, market_id: str, seconds_left: float) -> float | None:
        # what the market settles on: the value it references, against its strike
        m = ctx.market(market_id).describe()
        index = ctx.latest(str(m["reference"])) if m["reference"] else None
        strike = m["strike"]
        if not isinstance(index, IndexValue) or not isinstance(strike, float):
            return None
        return p_up(index.value, strike, seconds_left, self.params.vol_per_s)
