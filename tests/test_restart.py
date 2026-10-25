from pathlib import Path

from helpers import Hooks, M, book, make, opened
from sqlalchemy import select

from quanty import synthetic
from quanty.brokers.sim import SimBroker
from quanty.core import Engine, Instance, Ledger, Side, Tif
from quanty.db.models import OrderRow
from quanty.experiment import strategy_class
from quanty.plugins import discover

CLS = strategy_class(discover([], local=None)["example_favorite"])


def engine(path: Path) -> Engine:
    return Engine(
        [Instance("fav", CLS(CLS.params_type()), SimBroker(fees=synthetic.FEES))], ledger=Ledger(path)
    )


def test_a_restarted_session_knows_what_it_holds(tmp_path: Path) -> None:
    events = list(synthetic.events(synthetic.Spec(hours=4)))
    first = engine(tmp_path / "run.sqlite")
    cut = None  # stop past the middle of the run, at a moment it holds positions
    for i, ev in enumerate(events):
        first.advance(ev.ts)
        first.process(ev)
        if i > len(events) // 2 and any(p.qty for *_, p in first.portfolio.open_positions()):
            cut = i
            break
    assert cut is not None
    held = {(m, s): p.qty for _, m, s, p in first.portfolio.open_positions()}
    realized = first.portfolio.realized["fav"]
    first.ledger.close()

    second = engine(tmp_path / "run.sqlite")  # a new process, the same run
    got = second.restore()
    assert got["open"] == len(held) and got["skipped"] == 0
    assert {(m, s): p.qty for _, m, s, p in second.portfolio.open_positions()} == held
    assert second.portfolio.realized["fav"] == realized
    for ev in events[cut + 1 :]:  # carries on: settles what it held, doesn't buy into it again
        second.advance(ev.ts)
        second.process(ev)
    second.ledger.commit()
    held_markets = {m for m, _ in held}
    rebought = second.ledger.execute(
        select(OrderRow.id).where(
            OrderRow.action == "buy",
            OrderRow.market_id.in_(held_markets),
            OrderRow.created_ts > events[cut].ts,
        )
    ).all()
    assert rebought == []  # it knew it already held those
    assert all(p.qty == 0 for *_, p in second.portfolio.open_positions())  # all settled by the end


def test_warming_rebuilds_state_without_trading(tmp_path: Path) -> None:
    seen: list[str] = []
    s = Hooks(book=lambda ctx, b: seen.append(ctx.buy(M.id, Side.YES, 0.6, 10, tif=Tif.IOC).reason))
    e = make(s)
    e.ledger = Ledger(tmp_path / "run.sqlite")
    with e.warming():
        e.run([*opened(), book(1, [(0.55, 50)], [(0.60, 10)])])
    assert seen == ["warming up"] and M.id in e.open_markets and e.books[M.id].asks[0][0] == 0.60
    assert not e.live and not list(e.portfolio.open_positions())  # no order reached the broker
    assert e.ledger.execute(select(OrderRow)).all() == []  # nothing written to the run
