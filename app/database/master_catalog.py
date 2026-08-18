from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import Base


class MasterCatalog(Base):
    __tablename__ = "master_catalog"

    catalog_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
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

    description: Mapped[Optional[str]] = mapped_column(
        String(300),
        nullable=True,
        index=True,
    )

    unit_cost: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4),
        nullable=True,
    )

    master_case_quantity: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )

    unit_weight: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4),
        nullable=True,
    )

    source_row_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    source_file: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    import_status: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="imported",
        index=True,
    )

    review_notes: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    imported_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now,
    )


Index(
    "ix_master_catalog_barcode_search",
    MasterCatalog.barcode_lookup,
    MasterCatalog.barcode_exact,
)
