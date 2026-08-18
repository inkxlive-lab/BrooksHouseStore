from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ProductCreate(BaseModel):
    barcode: str = Field(
        min_length=4,
        max_length=50,
        description="UPC, EAN, or other product barcode",
    )

    barcode_type: Optional[str] = Field(
        default="UPC-A",
        max_length=30,
    )

    product_name: str = Field(
        min_length=1,
        max_length=200,
    )

    brand: Optional[str] = Field(
        default=None,
        max_length=120,
    )

    description: Optional[str] = None

    category: Optional[str] = Field(
        default=None,
        max_length=100,
    )

    size_value: Optional[Decimal] = Field(
        default=None,
        ge=0,
    )

    size_unit: Optional[str] = Field(
        default=None,
        max_length=30,
    )

    pack_quantity: int = Field(
        default=1,
        ge=1,
    )

    quantity_per_scan: int = Field(
        default=1,
        ge=1,
    )

    suggested_retail_price: Optional[Decimal] = Field(
        default=None,
        ge=0,
    )

    store_price: Optional[Decimal] = Field(
        default=None,
        ge=0,
    )

    average_cost: Optional[Decimal] = Field(
        default=None,
        ge=0,
    )

    taxable: bool = True

    starting_quantity: int = Field(
        default=0,
        ge=0,
    )

    location_id: int = Field(
        default=1,
        ge=1,
    )

    notes: Optional[str] = None

    @field_validator("barcode")
    @classmethod
    def clean_barcode(cls, value: str) -> str:
        cleaned_value = value.strip().replace(" ", "")

        if not cleaned_value:
            raise ValueError("Barcode cannot be empty.")

        return cleaned_value

    @field_validator(
        "product_name",
        "brand",
        "category",
        "size_unit",
        "barcode_type",
    )
    @classmethod
    def clean_text_fields(cls, value):
        if value is None:
            return None

        cleaned_value = value.strip()

        return cleaned_value or None
