from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.connection import Base
from app.services.transaction_actor import (
    transaction_user_id,
    transaction_user_name,
    transaction_user_role,
)


class Product(Base):
    __tablename__ = "products"

    product_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    product_name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        index=True,
    )

    brand: Mapped[Optional[str]] = mapped_column(
        String(120),
        nullable=True,
        index=True,
    )

    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    category: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )

    size_value: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 2),
        nullable=True,
    )

    size_unit: Mapped[Optional[str]] = mapped_column(
        String(30),
        nullable=True,
    )

    pack_quantity: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )

    suggested_retail_price: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 2),
        nullable=True,
    )

    store_price: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 2),
        nullable=True,
    )

    average_cost: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 2),
        nullable=True,
    )

    taxable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )

    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now,
        onupdate=datetime.now,
    )

    barcodes: Mapped[list["ProductBarcode"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
    )

    images: Mapped[list["ProductImage"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
    )

    inventory_records: Mapped[list["Inventory"]] = relationship(
        back_populates="product",
    )

    inventory_transactions: Mapped[list["InventoryTransaction"]] = relationship(
        back_populates="product",
    )

    price_history: Mapped[list["PriceHistory"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
    )


class ProductBarcode(Base):
    __tablename__ = "product_barcodes"
    __table_args__ = (
        UniqueConstraint(
            "barcode",
            name="uq_product_barcodes_barcode",
        ),
    )

    barcode_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.product_id"),
        nullable=False,
        index=True,
    )

    barcode: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        unique=True,
        index=True,
    )

    barcode_type: Mapped[Optional[str]] = mapped_column(
        String(30),
        nullable=True,
    )

    is_primary: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )

    quantity_per_scan: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now,
    )

    product: Mapped["Product"] = relationship(
        back_populates="barcodes",
    )


class ProductImage(Base):
    __tablename__ = "product_images"

    image_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.product_id"),
        nullable=False,
        index=True,
    )

    image_path: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
    )

    image_url: Mapped[Optional[str]] = mapped_column(
        String(1000),
        nullable=True,
    )

    image_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="front",
    )

    is_primary: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now,
    )

    product: Mapped["Product"] = relationship(
        back_populates="images",
    )


class InventoryLocation(Base):
    __tablename__ = "inventory_locations"

    location_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    location_name: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        unique=True,
    )

    location_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="store",
    )

    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now,
    )

    inventory_records: Mapped[list["Inventory"]] = relationship(
        back_populates="location",
    )

    inventory_transactions: Mapped[list["InventoryTransaction"]] = relationship(
        back_populates="location",
    )

    storage_photos: Mapped[list["StoragePhoto"]] = relationship(
        back_populates="location",
        cascade="all, delete-orphan",
    )



class StoragePhoto(Base):
    __tablename__ = "storage_photos"

    photo_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    location_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_locations.location_id"),
        nullable=False,
        index=True,
    )

    container_id: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        default="",
        index=True,
    )

    image_path: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )

    caption: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now,
    )

    location: Mapped["InventoryLocation"] = relationship(
        back_populates="storage_photos",
    )


class Inventory(Base):
    __tablename__ = "inventory"
    __table_args__ = (
        UniqueConstraint(
            "product_id",
            "location_id",
            "container_id",
            name=(
                "uq_inventory_product_"
                "location_container"
            ),
        ),
    )

    inventory_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.product_id"),
        nullable=False,
        index=True,
    )

    location_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_locations.location_id"),
        nullable=False,
        index=True,
    )

    container_id: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        default="",
        index=True,
    )

    quantity_on_hand: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    quantity_reserved: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    reorder_level: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now,
        onupdate=datetime.now,
    )

    product: Mapped["Product"] = relationship(
        back_populates="inventory_records",
    )

    location: Mapped["InventoryLocation"] = relationship(
        back_populates="inventory_records",
    )


class InventoryTransaction(Base):
    __tablename__ = "inventory_transactions"

    transaction_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.product_id"),
        nullable=False,
        index=True,
    )

    location_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_locations.location_id"),
        nullable=False,
        index=True,
    )

    container_id: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        default="",
        index=True,
    )

    transaction_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        index=True,
    )

    quantity_change: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    unit_cost: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 2),
        nullable=True,
    )

    reference_number: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )

    notes: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    performed_by_user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        index=True,
        default=transaction_user_id,
    )

    performed_by_name: Mapped[Optional[str]] = mapped_column(
        String(120),
        nullable=True,
        default=transaction_user_name,
    )

    performed_by_role: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        default=transaction_user_role,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now,
        index=True,
    )

    product: Mapped["Product"] = relationship(
        back_populates="inventory_transactions",
    )

    location: Mapped["InventoryLocation"] = relationship(
        back_populates="inventory_transactions",
    )


class PriceHistory(Base):
    __tablename__ = "price_history"

    price_history_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.product_id"),
        nullable=False,
        index=True,
    )

    old_price: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 2),
        nullable=True,
    )

    new_price: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    price_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="store_price",
    )

    reason: Mapped[Optional[str]] = mapped_column(
        String(250),
        nullable=True,
    )

    changed_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now,
    )

    product: Mapped["Product"] = relationship(
        back_populates="price_history",
    )
