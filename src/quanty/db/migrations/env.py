"""Alembic environment. Runs against the connection quanty passes in, or alembic.ini's URL (dev CLI)."""

from alembic import context
from sqlalchemy import Connection, create_engine

from quanty.db.models import Base

config = context.config


def run(conn: Connection) -> None:
    # render_as_batch: SQLite can't ALTER most things, so migrations rebuild tables instead
    context.configure(connection=conn, target_metadata=Base.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


conn = config.attributes.get("connection")
if conn is not None:
    run(conn)
else:
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("set sqlalchemy.url in alembic.ini or pass a connection")
    with create_engine(url).begin() as c:
        run(c)
