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
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        upgrade(conn)
    return engine


def alembic_config(conn: Connection | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.attributes["connection"] = conn
    return cfg


def upgrade(conn: Connection, revision: str = "head") -> None:
    command.upgrade(alembic_config(conn), revision)
