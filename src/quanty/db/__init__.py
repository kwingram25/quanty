"""Database access: SQLAlchemy models and Alembic migrations."""

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, StaticPool, create_engine, event

from quanty.db.models import Base

MIGRATIONS = Path(__file__).parent / "migrations"


def connect(path: str | Path = ":memory:") -> Engine:
    """An engine for a SQLite file brought up to the latest schema, or a fresh in-memory database."""
    if str(path) == ":memory:":
        engine = create_engine("sqlite://", poolclass=StaticPool)  # one shared in-memory database
        Base.metadata.create_all(engine)
        return engine
    engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 10})
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")  # readers (quanty ui) don't block the writer
        upgrade(conn)
    return engine


def connect_readonly(path: str | Path) -> Engine:
    """Read a database another process may be writing; never migrates or writes. A ledger from
    before a column was added reads as if it had it, with the default: see _fill_in."""
    engine = create_engine(
        f"sqlite:///file:{Path(path).resolve()}?mode=ro&uri=true", connect_args={"timeout": 10}
    )
    event.listen(engine, "connect", _fill_in)
    return engine


# columns added after ledgers already existed: table -> (column, value for older rows)
ADDED = {"orders": ("kind", "'sim'"), "fills": ("kind", "'sim'"), "settlements": ("kind", "'sim'")}


def _fill_in(dbapi_conn: Any, _record: Any) -> None:
    """On a read-only connection, give a table missing an added column a TEMP view of the same name
    that adds it (temp objects shadow the file's, and live in memory: the file is never touched)."""
    cur = dbapi_conn.cursor()
    try:
        for table, (column, value) in ADDED.items():
            cols = [r[1] for r in cur.execute(f"PRAGMA main.table_info({table})")]
            if cols and column not in cols:
                cur.execute(f"CREATE TEMP VIEW {table} AS SELECT *, {value} AS {column} FROM main.{table}")
    finally:
        cur.close()


def alembic_config(conn: Connection | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.attributes["connection"] = conn
    return cfg


def upgrade(conn: Connection, revision: str = "head") -> None:
    command.upgrade(alembic_config(conn), revision)
