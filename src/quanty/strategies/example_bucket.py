"""Example strategy for range markets: buy the temperature bucket a model prices above its ask.

Not an up/down market: each event is one question ("what will the temperature be at 14:00?") with
several range outcomes, one of which wins. The model treats the reading as a random walk from now
to the close. It trades the synthetic temperature ladder (`quanty demo --live`). This shows the
shape of such a strategy, not a tuned edge.
"""

from pydantic import Field

from quanty.core import Book, Context, Observation, Params, Side, Strategy, Tif
from quanty.synthetic import TEMP_KEY, TEMP_SIGMA_PER_S, p_range


class BucketParams(Params):
    min_edge: float = Field(0.05, gt=0, lt=1, description="Buy when the model beats the ask by this much")
    enter_last_s: int = Field(1200, gt=0, description="Only buy in the last N seconds before the close")
    size: int = Field(10, gt=0, description="Contracts per bucket")
    sigma_per_s: float = Field(TEMP_SIGMA_PER_S, gt=0, description="Model input: °F of drift per √second")


class BucketEdge(Strategy[BucketParams]):
    params_type = BucketParams
    series = ("synthetic:temp_1h",)
    description = "Buy a range bucket when a random-walk model prices it above the ask (example)"

    def on_book(self, ctx: Context, book: Book) -> None:
        p = self.params
        mid = book.market_id
        left = ctx.seconds_left(mid)
        if not 0 < left <= p.enter_last_s or ctx.orders(mid) or ctx.position(mid, Side.YES):
            return
        reading = ctx.latest(TEMP_KEY)
        terms = ctx.market(mid).terms
        if not isinstance(reading, Observation):
            return
        lo, hi = terms.get("lo"), terms.get("hi")
        prob = p_range(
            reading.value,
            lo if isinstance(lo, float) else None,
            hi if isinstance(hi, float) else None,
            left,
            p.sigma_per_s,
        )
        ask = book.ask(Side.YES)
        if ask is not None and prob - ask >= p.min_edge:
            ctx.buy(mid, Side.YES, ask, p.size, tif=Tif.IOC, tag="bucket")
