"""Database access: SQLAlchemy models and Alembic migrations."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, StaticPool, create_engine

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
    """Read a database another process may be writing; never migrates or writes."""
    return create_engine(
        f"sqlite:///file:{Path(path).resolve()}?mode=ro&uri=true", connect_args={"timeout": 10}
    )


def alembic_config(conn: Connection | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.attributes["connection"] = conn
    return cfg


def upgrade(conn: Connection, revision: str = "head") -> None:
    command.upgrade(alembic_config(conn), revision)
