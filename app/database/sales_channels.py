from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.connection import Base


class SalesChannel(Base):
    __tablename__ = "sales_channels"

    channel_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    channel_name: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
        unique=True,
    )

    active: Mapped[bool] = mapped_column(
        nullable=False,
        default=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now,
    )

    listings: Mapped[list["ChannelListing"]] = relationship(
        back_populates="channel",
        cascade="all, delete-orphan",
    )


class ChannelListing(Base):
    __tablename__ = "channel_listings"

    __table_args__ = (
        UniqueConstraint(
            "channel_id",
            "external_variant_id",
            name="uq_channel_external_variant",
        ),
    )

    listing_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    channel_id: Mapped[int] = mapped_column(
        ForeignKey("sales_channels.channel_id"),
        nullable=False,
        index=True,
    )

    external_product_id: Mapped[Optional[str]] = mapped_column(
        String(150),
        nullable=True,
        index=True,
    )

    external_variant_id: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
        index=True,
    )

    listing_title: Mapped[Optional[str]] = mapped_column(
        String(300),
        nullable=True,
        index=True,
    )

    variant_title: Mapped[Optional[str]] = mapped_column(
        String(300),
        nullable=True,
    )

    sku: Mapped[Optional[str]] = mapped_column(
        String(150),
        nullable=True,
        index=True,
    )

    barcode_raw: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )

    barcode_exact: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        index=True,
    )

    barcode_lookup: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        index=True,
    )

    listing_status: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        index=True,
    )

    listed_price: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )

    quantity_available: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )

    vendor: Mapped[Optional[str]] = mapped_column(
        String(200),
        nullable=True,
    )

    source_data: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    first_imported_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now,
    )

    last_imported_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now,
        onupdate=datetime.now,
    )

    channel: Mapped["SalesChannel"] = relationship(
        back_populates="listings",
    )


Index(
    "ix_channel_listing_barcode_search",
    ChannelListing.channel_id,
    ChannelListing.barcode_lookup,
)
