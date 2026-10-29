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


def test_a_restart_closes_the_orders_a_stopped_session_left_open(tmp_path: Path) -> None:
    def session() -> Engine:
        s = Hooks(book=lambda ctx, b: ctx.orders(M.id) or ctx.buy(M.id, Side.YES, 0.40, 10, tif=Tif.GTC))
        return Engine([Instance("bid", s, SimBroker())], ledger=Ledger(tmp_path / "run.sqlite"))

    def statuses() -> list[tuple[str, str]]:
        ledger = Ledger(tmp_path / "run.sqlite")
        rows = [(r.status, r.reason) for r in ledger.execute(select(OrderRow)).scalars()]
        ledger.close()
        return rows

    first = session()
    for ev in [
        *opened(M),
        *(book(t, [(0.39, 100)], [(0.45, 100)]) for t in (1, 2)),
    ]:  # a bid under the ask: it rests
        first.advance(ev.ts)
        first.process(ev)
    first.ledger.close()  # the session stops with it resting
    assert statuses() == [("open", "")]
    second = session()  # a new process, the same run
    assert second.restore()["orders_closed"] == 1
    second.ledger.close()
    assert statuses() == [("canceled", "left open when its session stopped")]


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


class LooksLive(SimBroker):  # simulated fills, recorded as real money
    kind = "live"


def test_a_switched_instance_keeps_what_it_holds_on_the_old_broker(tmp_path: Path) -> None:
    from helpers import M2

    from quanty.core import Settled
    from quanty.db.models import FillRow, SettlementRow

    paper, real = SimBroker("paper"), LooksLive("real")
    s = Hooks()
    e = Engine(
        [Instance("s", s, paper)], ledger=Ledger(tmp_path / "run.sqlite"), spare_brokers=[real], clock_ms=None
    )
    e.run([*opened(M, M2), book(1, [(0.55, 50)], [(0.60, 50)])])
    ctx = e._ctx["s"]
    ctx.buy(M.id, Side.YES, 0.60, 10, tif=Tif.IOC)  # a simulated position in M
    e.run([book(2, [(0.55, 50)], [(0.60, 50)])])
    assert e.switch("s", "real") == [M.id]  # M stays on paper
    e.run([book(3, [(0.65, 50)], [(0.70, 50)]), book(3, [(0.55, 50)], [(0.60, 50)], market=M2)])
    ctx.sell(M.id, Side.YES, 0.65, 4, tif=Tif.IOC)  # an exit from the simulated position: simulated
    ctx.buy(M2.id, Side.YES, 0.60, 5, tif=Tif.IOC)  # a new market: real money
    e.run([book(4, [(0.65, 50)], [(0.70, 50)]), book(4, [(0.55, 50)], [(0.60, 50)], market=M2)])
    e.run(
        [
            Settled(ts=700_000, market_id=M.id, outcome=Side.YES),
            Settled(ts=700_000, market_id=M2.id, outcome=Side.NO),
        ]
    )
    e.ledger.commit()
    kinds = {(r.market_id, r.action.value): r.kind for r in e.ledger.execute(select(OrderRow)).scalars()}
    assert kinds == {(M.id, "buy"): "sim", (M.id, "sell"): "sim", (M2.id, "buy"): "live"}
    assert {
        r.market_id: r.kind for r in e.ledger.execute(select(FillRow)).scalars() if r.action.value == "sell"
    } == {M.id: "sim"}
    assert {r.market_id: r.kind for r in e.ledger.execute(select(SettlementRow)).scalars()} == {
        M.id: "sim",
        M2.id: "live",
    }


def test_a_restored_live_position_is_never_sold_in_simulation(tmp_path: Path) -> None:
    path = tmp_path / "run.sqlite"
    first = Engine([Instance("s", Hooks(), LooksLive("real"))], ledger=Ledger(path), clock_ms=None)
    first.run([*opened(), book(1, [(0.55, 50)], [(0.60, 50)])])
    first._ctx["s"].buy(M.id, Side.YES, 0.60, 10, tif=Tif.IOC)
    first.run([book(2, [(0.55, 50)], [(0.60, 50)])])
    first.ledger.close()
    second = Engine(
        [Instance("s", Hooks(), SimBroker("paper"))], ledger=Ledger(path), clock_ms=None
    )  # demoted
    got = second.restore()
    assert got["open"] == 1 and got["live_without_broker"] == 1
    second.run([*opened(), book(3, [(0.65, 50)], [(0.70, 50)])])
    o = second._ctx["s"].sell(M.id, Side.YES, 0.65, 10, tif=Tif.IOC)
    assert o.status == "rejected" and "live broker" in o.reason  # not "sold" by the simulator


def test_an_added_instance_sees_the_open_markets(tmp_path: Path) -> None:
    paper = SimBroker("paper")
    e = Engine([Instance("a", Hooks(), paper)], ledger=Ledger(tmp_path / "run.sqlite"), clock_ms=None)
    e.run([*opened(M), book(1, [(0.55, 50)], [(0.60, 50)])])
    twin = Hooks()
    e.add_instance(Instance("a_paper", twin, paper, twin_of="a"))
    e.run([book(2, [(0.56, 50)], [(0.61, 50)])])
    assert [n for n, _ in twin.seen] == ["book"] and e._ctx["a_paper"].book(M.id) is not None
