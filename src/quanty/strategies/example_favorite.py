"""Example strategy: buy the favorite late in a window when a model says it is likely to win.

This shows the shape of a strategy, not a tuned edge. It trades the synthetic up/down series
(`quanty demo`), and its model is the same log-normal formula the synthetic markets are built on.
"""

from pydantic import Field

from quanty.core import Book, Context, IndexValue, Params, Side, Strategy, Tif
from quanty.synthetic import INDEX_KEY, p_up


class FavoriteParams(Params):
    min_conf: float = Field(0.9, gt=0.5, lt=1, description="Model probability the favorite wins")
    max_price: float = Field(0.95, gt=0, lt=1, description="Never pay more than this")
    enter_last_s: int = Field(60, gt=0, description="Only buy in the last N seconds of a window")
    size: int = Field(10, gt=0, description="Contracts per window")
    vol_per_s: float = Field(0.0005, gt=0, description="Model input: index log-return sd per second")


class FavoriteLate(Strategy[FavoriteParams]):
    params_type = FavoriteParams
    series = ("synthetic:updown_5m",)
    description = "Buy the favorite under a price cap late in the window (example)"

    def on_book(self, ctx: Context, book: Book) -> None:
        p = self.params
        mid = book.market_id
        left = ctx.seconds_left(mid)
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
        if conf >= p.min_conf and price is not None and price <= p.max_price:
            ctx.buy(mid, side, price, p.size, tif=Tif.IOC, tag="favorite")

    def fair_yes(self, ctx: Context, market_id: str, seconds_left: float) -> float | None:
        index = ctx.latest(INDEX_KEY)
        strike = ctx.market(market_id).terms.get("strike")
        if not isinstance(index, IndexValue) or not isinstance(strike, float):
            return None
        return p_up(index.value, strike, seconds_left, self.params.vol_per_s)
