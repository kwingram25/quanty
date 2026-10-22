from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select

from quanty import db
from quanty.core import Action, Fill, Ledger, Liquidity, Order, OrderStatus, Side
from quanty.core.portfolio import Settlement
from quanty.db.models import Base, FillRow, LogRow, OrderRow


def test_migrations_match_models(tmp_path: Path) -> None:
    """A migrated database has exactly the schema the models declare (no migration forgotten)."""
    engine = db.connect(tmp_path / "l.sqlite")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_connect_is_idempotent_and_versioned(tmp_path: Path) -> None:
    path = tmp_path / "l.sqlite"
    db.connect(path).dispose()
    db.connect(path).dispose()
    tables = set(inspect(create_engine(f"sqlite:///{path}")).get_table_names())
    assert {"alembic_version", "orders", "fills", "settlements", "logs"} <= tables


def fill(qty: int, realized: float = 0.0) -> Fill:
    return Fill(ts=1, order_id="a-1", market_id="m", side=Side.YES, action=Action.BUY, price=0.4, qty=qty,
                fee=0.01, liquidity=Liquidity.TAKER)  # fmt: skip


def test_round_trip_and_summary(tmp_path: Path) -> None:
    path = tmp_path / "l.sqlite"
    led = Ledger(path)
    o = Order(id="a-1", instance="a", market_id="m", side=Side.YES, action=Action.BUY, price=0.4, qty=10)
    led.order(o)
    o.status, o.filled = OrderStatus.FILLED, 10  # the latest state wins
    led.order(o)
    led.fill("a", fill(10), 0.0)
    led.settlement(Settlement("a", "m", Side.YES, 10, 10.0, 5.9, 2))
    led.log(2, "a", "hello", {"n": 1, "obj": object()}, level="warning")
    led.close()

    led = Ledger(path)  # reopened from disk
    (row,) = led.execute(select(OrderRow)).scalars()
    assert (row.status, row.filled, row.side) == (OrderStatus.FILLED, 10, Side.YES)
    assert led.execute(select(FillRow.qty)).scalar_one() == 10
    log = led.execute(select(LogRow)).scalar_one()
    assert log.fields["n"] == 1 and log.fields["obj"].startswith("<object")
    (s,) = led.summary()
    assert (s.instance, s.orders, s.fills, s.contracts, s.wins, s.losses) == ("a", 1, 1, 10, 1, 0)
    assert abs(s.pnl - 5.9) < 1e-9 and abs(s.fees - 0.01) < 1e-9


def test_enums_are_stored_as_values(tmp_path: Path) -> None:
    path = tmp_path / "l.sqlite"
    led = Ledger(path, durable=True)
    led.fill("a", fill(1), 0.0)  # durable: on disk without commit()
    with create_engine(f"sqlite:///{path}").connect() as conn:
        assert conn.exec_driver_sql("SELECT side, action, liquidity FROM fills").one() == (
            "yes",
            "buy",
            "taker",
        )
