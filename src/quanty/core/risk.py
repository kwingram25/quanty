"""Risk limits, checked by the engine on every order before it reaches a broker.

Limits apply per strategy instance and, separately, account-wide. Strategies cannot bypass them.
Sells only ever reduce a position, so they skip the exposure limits and are checked only against
the position they sell.

Open risk is what could still be lost. For a lone binary market that is what was paid. For the
outcomes of one question where at most one can win (an event whose markets have the term
`exclusive`, like a temperature ladder), it is the worst case across the possible winners: holding
every bucket of a ladder risks far less than the sum of their costs. Resting buys always count at
their full notional.
"""

from collections.abc import Iterable, Mapping

from quanty.core.orders import Order
from quanty.core.portfolio import Portfolio, Position
from quanty.core.types import EPS, Action, Market, Side
from quanty.core.validate import ConfigModel


class Limits(ConfigModel):
    max_order_qty: float | None = None
    max_position: float | None = None  # contracts per market: held + resting buys, both sides
    max_open_risk: float | None = None  # dollars: worst-case loss of open positions + resting buy notional
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
        if limits.max_order_qty is not None and order.qty > limits.max_order_qty + EPS:
            return f"{who}: qty {order.qty} > max_order_qty {limits.max_order_qty}"
        buys = [
            o for o in self._live.values()
            if o.action is Action.BUY and o.id != order.id and scope in (None, o.instance)
        ]  # fmt: skip
        held = [(m, side, p) for _, m, side, p in self._portfolio.open_positions(scope)]
        if limits.max_position is not None:
            n = sum(p.qty for m, _, p in held if m == order.market_id)
            n += sum(o.remaining for o in buys if o.market_id == order.market_id)
            if n + order.qty > limits.max_position + EPS:
                return f"{who}: position {n} + {order.qty} > max_position {limits.max_position}"
        notional = order.price * order.qty
        if limits.max_open_risk is not None:
            at_risk = self.at_risk(held, buys)
            if at_risk + notional > limits.max_open_risk + EPS:
                return (
                    f"{who}: open risk {at_risk:.2f} + {notional:.2f} > max_open_risk {limits.max_open_risk}"
                )
        if limits.max_group_risk is not None:
            group = self._group(order.market_id)
            at_risk = self.at_risk(
                [(m, side, p) for m, side, p in held if self._group(m) == group],
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
        if order.qty > held - selling + EPS:
            return f"sell {order.qty} {order.side} exceeds free position {held - selling}"
        return None

    def at_risk(self, held: Iterable[tuple[str, Side, Position]], buys: Iterable[Order]) -> float:
        """Worst-case loss of these positions (netted within exclusive events) + resting buy notional."""
        lone = 0.0
        events: dict[str, list[tuple[str, Side, Position]]] = {}
        for m, side, p in held:
            ev = self.exclusive_event(m)
            if ev is None:
                lone += p.cost
            else:
                events.setdefault(ev, []).append((m, side, p))
        netted = 0.0
        for ev, holdings in events.items():
            outcomes = [mid for mid, mk in self._markets.items() if self.exclusive_event(mid) == ev]
            exhaustive = all(self._markets[mid].terms.get("exhaustive") is True for mid in outcomes)
            netted += event_exposure(holdings, outcomes, exhaustive)[0]
        return lone + netted + sum(o.price * o.remaining for o in buys)

    def exclusive_event(self, market_id: str) -> str | None:
        """The event this market is one outcome of, if at most one of its markets can win."""
        m = self._markets.get(market_id)
        if m is None or m.terms.get("exclusive") is not True:
            return None
        return str(m.describe()["event"])

    def _group(self, market_id: str) -> str:
        m = self._markets.get(market_id)
        return m.risk_group if m else market_id


def event_exposure(
    holdings: Iterable[tuple[str, Side, Position]], outcomes: Iterable[str], exhaustive: bool
) -> tuple[float, float]:
    """(worst-case loss, best-case gain) of positions in the markets of one question where at most
    one market wins. outcomes: every market of the event; unless exhaustive, "none of them wins" is
    a possible result too."""
    holdings = list(holdings)
    cost = sum(p.cost for _, _, p in holdings)
    winners: list[str | None] = [*outcomes]
    if not exhaustive:
        winners.append(None)
    payouts = [sum(p.qty for m, side, p in holdings if (m == w) == (side is Side.YES)) for w in winners]
    return max(0.0, cost - min(payouts)), max(payouts) - cost
