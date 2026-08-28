"""Additive SQLAlchemy registration for Marketplace Publish Center tables."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import Base


class MarketplacePublishQueue(Base):
    __tablename__ = "marketplace_publish_queue"
    __table_args__ = (
        UniqueConstraint("channel", "product_id", name="uq_marketplace_publish_channel_product"),
        UniqueConstraint("idempotency_key", name="uq_marketplace_publish_idempotency"),
    )

    publish_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.product_id"), nullable=False, index=True)
    seller_sku: Mapped[str] = mapped_column(String(150), nullable=False)
    gtin: Mapped[Optional[str]] = mapped_column(String(50))
    external_catalog_id: Mapped[Optional[str]] = mapped_column(String(150))
    catalog_status: Mapped[str] = mapped_column(String(40), nullable=False, default="UNKNOWN")
    submission_type: Mapped[str] = mapped_column(String(40), nullable=False)
    proposed_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    proposed_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fulfillment_type: Mapped[str] = mapped_column(String(30), nullable=False, default="merchant")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="DRAFT", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    external_submission_id: Mapped[Optional[str]] = mapped_column(String(200))
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    validation_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    request_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    response_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class MarketplacePublishEvent(Base):
    __tablename__ = "marketplace_publish_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publish_id: Mapped[int] = mapped_column(ForeignKey("marketplace_publish_queue.publish_id"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    status_before: Mapped[Optional[str]] = mapped_column(String(30))
    status_after: Mapped[Optional[str]] = mapped_column(String(30))
    event_data_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


Index("ix_marketplace_publish_status", MarketplacePublishQueue.status, MarketplacePublishQueue.channel, MarketplacePublishQueue.product_id)
