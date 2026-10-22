"""Risk limits, checked by the engine on every order before it reaches a broker.

Limits apply per strategy instance and, separately, account-wide. Strategies cannot bypass them.
Sells only ever reduce a position, so they skip the exposure limits and are checked only against
the position they sell.
"""

from collections.abc import Iterable, Mapping

from quanty.core.orders import Order
from quanty.core.portfolio import Portfolio, Position
from quanty.core.types import EPS, Action, Market
from quanty.core.validate import ConfigModel


class Limits(ConfigModel):
    max_order_qty: int | None = None
    max_position: int | None = None  # contracts per market: held + resting buys, both sides
    max_open_risk: float | None = None  # dollars: cost of open positions + resting buy notional
    max_group_risk: float | None = None  # the same, per correlation group (Market.group)
    max_daily_loss: float | None = None  # no new buys once today's (UTC) realized P&L <= −this


class Risk:
    def __init__(self, portfolio: Portfolio, live: Mapping[str, Order], markets: Mapping[str, Market]):
        self._portfolio = portfolio
        self._live = live
        self._markets = markets

    def check(self, order: Order, limits: Limits, scope: str | None) -> str | None:
        """Why `order` breaks `limits` for one instance (scope) or the account (None); None if it doesn't."""
        if order.action is Action.SELL:
            return None
        who = scope or "account"
        if limits.max_order_qty is not None and order.qty > limits.max_order_qty:
            return f"{who}: qty {order.qty} > max_order_qty {limits.max_order_qty}"
        buys = [
            o for o in self._live.values()
            if o.action is Action.BUY and o.id != order.id and scope in (None, o.instance)
        ]  # fmt: skip
        held = [(m, p) for _, m, _, p in self._portfolio.open_positions(scope)]
        if limits.max_position is not None:
            n = sum(p.qty for m, p in held if m == order.market_id)
            n += sum(o.remaining for o in buys if o.market_id == order.market_id)
            if n + order.qty > limits.max_position:
                return f"{who}: position {n} + {order.qty} > max_position {limits.max_position}"
        notional = order.price * order.qty
        if limits.max_open_risk is not None:
            at_risk = _at_risk(held, buys)
            if at_risk + notional > limits.max_open_risk + EPS:
                return (
                    f"{who}: open risk {at_risk:.2f} + {notional:.2f} > max_open_risk {limits.max_open_risk}"
                )
        if limits.max_group_risk is not None:
            group = self._group(order.market_id)
            at_risk = _at_risk(
                [(m, p) for m, p in held if self._group(m) == group],
                [o for o in buys if self._group(o.market_id) == group],
            )
            if at_risk + notional > limits.max_group_risk + EPS:
                return f"{who}: group {group} risk {at_risk:.2f} + {notional:.2f} > max_group_risk"
        if limits.max_daily_loss is not None:
            pnl = self._portfolio.daily_pnl(scope, order.created_ts)
            if pnl <= -limits.max_daily_loss:
                return f"{who}: daily P&L {pnl:.2f} hit max_daily_loss {limits.max_daily_loss}"
        return None

    def check_sell(self, order: Order) -> str | None:
        held = self._portfolio.position(order.instance, order.market_id, order.side).qty
        selling = sum(
            o.remaining for o in self._live.values()
            if o.action is Action.SELL and o.id != order.id and o.instance == order.instance
            and o.market_id == order.market_id and o.side is order.side
        )  # fmt: skip
        if order.qty > held - selling:
            return f"sell {order.qty} {order.side} exceeds free position {held - selling}"
        return None

    def _group(self, market_id: str) -> str:
        m = self._markets.get(market_id)
        return m.risk_group if m else market_id


def _at_risk(held: Iterable[tuple[str, Position]], buys: Iterable[Order]) -> float:
    return sum(p.cost for _, p in held) + sum(o.price * o.remaining for o in buys)
