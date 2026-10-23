"""Database tables. The schema comes from these models; migrations (quanty/db/migrations) evolve it.

After changing a model, generate a migration and review it:
    uv run alembic revision --autogenerate -m "what changed"
"""

from enum import StrEnum

from sqlalchemy import JSON, Enum, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from quanty.enums import Action, Liquidity, OrderStatus, Side, Tif


def _enum(cls: type[StrEnum]) -> Enum:
    """Store an enum as its string value ("yes"), not its name, so rows read naturally."""
    return Enum(cls, native_enum=False, length=16, values_callable=lambda e: [m.value for m in e])


class Base(DeclarativeBase):
    type_annotation_map = {cls: _enum(cls) for cls in (Side, Action, Tif, OrderStatus, Liquidity)}  # noqa: RUF012


class MarketRow(Base):
    """Markets the run's strategies trade, so a ledger can name what it traded."""

    __tablename__ = "markets"

    id: Mapped[str] = mapped_column(primary_key=True)
    venue: Mapped[str]
    series: Mapped[str]
    open_ts: Mapped[int]
    close_ts: Mapped[int]
    title: Mapped[str]
    subtitle: Mapped[str]
    event: Mapped[str] = mapped_column(index=True)


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(primary_key=True)
    instance: Mapped[str] = mapped_column(index=True)
    market_id: Mapped[str]
    side: Mapped[Side]
    action: Mapped[Action]
    price: Mapped[float]
    qty: Mapped[float]
    tif: Mapped[Tif]
    post_only: Mapped[bool]
    tag: Mapped[str]
    created_ts: Mapped[int]
    status: Mapped[OrderStatus]
    filled: Mapped[float]
    reason: Mapped[str]


class FillRow(Base):
    __tablename__ = "fills"
    __table_args__ = (Index("ix_fills_instance_ts", "instance", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[int]
    instance: Mapped[str]
    order_id: Mapped[str]
    market_id: Mapped[str]
    side: Mapped[Side]
    action: Mapped[Action]
    price: Mapped[float]
    qty: Mapped[float]
    fee: Mapped[float]
    liquidity: Mapped[Liquidity]
    realized: Mapped[float]


class SettlementRow(Base):
    __tablename__ = "settlements"
    __table_args__ = (Index("ix_settlements_instance_ts", "instance", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[int]
    instance: Mapped[str]
    market_id: Mapped[str]
    side: Mapped[Side]
    qty: Mapped[float]
    payout: Mapped[float]
    pnl: Mapped[float]


class LogRow(Base):
    __tablename__ = "logs"
    __table_args__ = (Index("ix_logs_instance_ts", "instance", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[int]
    instance: Mapped[str]
    level: Mapped[str] = mapped_column(index=True)
    msg: Mapped[str]
    fields: Mapped[dict[str, object]] = mapped_column(JSON)


class FeedMetricRow(Base):
    """One feed's health and receive-lag stats over a window (normally a minute)."""

    __tablename__ = "feed_metrics"
    __table_args__ = (Index("ix_feed_metrics_feed_start", "feed", "start"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    feed: Mapped[str]
    start: Mapped[int]
    end: Mapped[int]
    messages: Mapped[int]
    events: Mapped[int]
    parse_errors: Mapped[int]
    lag_count: Mapped[int]
    lag_p50: Mapped[float | None]
    lag_p90: Mapped[float | None]
    lag_p99: Mapped[float | None]
    lag_max: Mapped[float | None]
    negative_lags: Mapped[int]
    gaps: Mapped[int]
    max_gap_ms: Mapped[int]
    reconnects: Mapped[int]
