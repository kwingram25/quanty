"""A live session's start: what it holds for real against what the venue says (the fake exchange)."""

import asyncio
import math
from pathlib import Path

import pytest
from helpers import Hooks

from quanty import reconcile
from quanty.brokers.live import LiveBroker
from quanty.core import Engine, Instance, Ledger
from quanty.core.events import Fill
from quanty.core.types import Action, Liquidity, Side
from quanty.venues.fake import FakeExchange
from quanty.venues.orders import KalshiOrders, PolymarketOrders


class FakeSigner:
    def headers(self, method: str, path: str) -> dict[str, str]:
        return {}


def fill(oid: str, market: str, side: Side, action: Action, qty: float) -> Fill:
    return Fill(ts=1000, order_id=oid, market_id=market, side=side, action=action, price=0.9, qty=qty,
                fee=0.0, liquidity=Liquidity.TAKER)  # fmt: skip


def session(tmp_path: Path, fake: FakeExchange, fills: list[Fill]) -> Engine:
    """A run that filled `fills` for real in an earlier session, restarted."""
    path = tmp_path / "main.sqlite"
    first = Ledger(path, durable=True)
    first.kinds = {"fav": "live"}
    for f in fills:
        first.fill("fav", f, 0.0)
    first.close()
    apis = {"kalshi": KalshiOrders(FakeSigner()), "polymarket_us": PolymarketOrders(FakeSigner())}
    live = LiveBroker("live", armed=True, apis=apis, transport=fake.transport(), max_qty=10, max_notional=10)
    engine = Engine([Instance("fav", Hooks(), live)], ledger=Ledger(path, durable=True), clock_ms=None,
                    strict=False)  # fmt: skip
    engine.restore()
    return engine


BOUGHT = [fill("o1", "kalshi:KX-1", Side.YES, Action.BUY, 10)]


def test_matching_positions_halt_nothing(tmp_path: Path) -> None:
    fake = FakeExchange()
    fake.held["KX-1"] = 10
    engine = session(tmp_path, fake, BOUGHT)
    assert asyncio.run(reconcile.on_start(engine)) == [] and not engine.halted


def test_a_position_the_venue_doesnt_have_halts_its_strategy(tmp_path: Path) -> None:
    fake = FakeExchange()
    fake.held["KX-1"] = 4  # sold elsewhere, or an exit that filled while nothing ran
    engine = session(tmp_path, fake, BOUGHT)
    (f,) = asyncio.run(reconcile.on_start(engine))
    assert f.halts and f.ours == 10 and f.venue == 4 and engine.halted == {"fav"}
    assert "the venue holds 4 YES, quanty's strategies 10 YES; check the venue, then resume fav" in f.text()


def test_the_other_side_halts_too(tmp_path: Path) -> None:
    fake = FakeExchange()
    fake.held["KX-1"] = -10
    engine = session(tmp_path, fake, BOUGHT)
    assert [f.halts for f in asyncio.run(reconcile.on_start(engine))] == [True]


def test_a_market_that_settled_while_down_is_not_missing(tmp_path: Path) -> None:
    fake = FakeExchange()
    fake.settled.add("KX-1")
    engine = session(tmp_path, fake, BOUGHT)
    assert asyncio.run(reconcile.on_start(engine)) == [] and not engine.halted


def test_more_on_the_venue_is_a_note(tmp_path: Path) -> None:
    fake = FakeExchange()
    fake.held |= {"pm-a": 15, "pm-b": 3}  # more than ours; and in a market it traded and closed
    engine = session(tmp_path, fake, [
        fill("o1", "polymarket_us:pm-a", Side.YES, Action.BUY, 10),
        fill("o2", "polymarket_us:pm-b", Side.NO, Action.BUY, 5),
        fill("o3", "polymarket_us:pm-b", Side.NO, Action.SELL, 5),
    ])  # fmt: skip
    found = asyncio.run(reconcile.on_start(engine))
    assert [(f.market_id, f.ours, f.venue, f.halts) for f in found] == [
        ("polymarket_us:pm-a", 10, 15, False),
        ("polymarket_us:pm-b", 0, 3, False),
    ]
    assert not engine.halted


def test_unreadable_positions_halt_what_holds_there(tmp_path: Path) -> None:
    fake = FakeExchange()
    fake.fail_next("error")
    engine = session(tmp_path, fake, BOUGHT)
    (f,) = asyncio.run(reconcile.on_start(engine))
    assert f.halts and math.isnan(f.venue) and engine.halted == {"fav"}
    assert "couldn't be read" in f.text()


def test_never_traded_for_real_asks_nothing(tmp_path: Path) -> None:
    fake = FakeExchange()
    engine = session(tmp_path, fake, [])
    assert asyncio.run(reconcile.on_start(engine)) == [] and fake.requests == []


@pytest.mark.parametrize(("venue", "want"), [(10.0, []), (0.0, [True]), (12.0, [False])])
def test_compare(venue: float, want: list[bool]) -> None:
    got = reconcile.compare({"k:a": {"x": 6, "y": 4}}, {"k:a": venue}, set(), {"k:a"})
    assert [f.halts for f in got] == want
