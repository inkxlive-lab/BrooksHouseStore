import csv
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from sqlalchemy import delete, func, select

from app.database.connection import Base, SessionLocal, engine
from app.database.master_catalog import MasterCatalog


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IMPORT_FILE = (
    PROJECT_ROOT
    / "imports"
    / "master_catalog.txt"
)

BATCH_SIZE = 1000


def clean_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None

    cleaned = value.strip()

    return cleaned or None


def parse_decimal(
    value: Optional[str],
) -> Optional[Decimal]:
    cleaned = clean_text(value)

    if cleaned is None:
        return None

    cleaned = cleaned.replace("$", "").replace(",", "")

    try:
        return Decimal(cleaned)

    except InvalidOperation:
        return None


def parse_integer(
    value: Optional[str],
) -> Optional[int]:
    number = parse_decimal(value)

    if number is None:
        return None

    try:
        return int(number)

    except (ValueError, OverflowError):
        return None


def normalize_barcode(
    raw_value: Optional[str],
) -> tuple[
    Optional[str],
    Optional[str],
    str,
    Optional[str],
]:
    raw_barcode = clean_text(raw_value)

    if raw_barcode is None:
        return (
            None,
            None,
            "needs_review",
            "Barcode is blank.",
        )

    if raw_barcode.upper() in {
        "#N/A",
        "N/A",
        "NA",
        "NONE",
        "NULL",
    }:
        return (
            None,
            None,
            "needs_review",
            "Barcode is not available.",
        )

    if "E+" in raw_barcode.upper():
        return (
            None,
            None,
            "needs_review",
            (
                "Barcode is stored in rounded scientific notation. "
                "The original digits cannot be recovered safely."
            ),
        )

    cleaned = raw_barcode.strip()

    if cleaned.endswith(".0"):
        cleaned = cleaned[:-2]

    digits_only = re.sub(r"\D", "", cleaned)

    if not digits_only:
        return (
            None,
            None,
            "needs_review",
            "Barcode does not contain usable digits.",
        )

    if digits_only == "0":
        return (
            "0",
            "0",
            "needs_review",
            "Barcode value is zero.",
        )

    lookup_value = digits_only.lstrip("0")

    if not lookup_value:
        lookup_value = "0"

    notes = None
    status = "imported"

    if len(digits_only) < 8:
        status = "needs_review"
        notes = (
            "Barcode has fewer than 8 digits. "
            "It may be incomplete or missing leading zeros."
        )

    elif len(digits_only) > 14:
        status = "needs_review"
        notes = "Barcode has more than 14 digits."

    return (
        digits_only,
        lookup_value,
        status,
        notes,
    )


def import_master_catalog(
    import_file: Path,
    replace_existing: bool = True,
) -> None:
    if not import_file.exists():
        raise FileNotFoundError(
            f"Import file was not found: {import_file}"
        )

    Base.metadata.create_all(bind=engine)

    counters = {
        "rows_read": 0,
        "imported": 0,
        "needs_review": 0,
        "missing_description": 0,
        "invalid_cost": 0,
        "invalid_case_quantity": 0,
        "invalid_weight": 0,
    }

    source_name = import_file.name

    with SessionLocal() as database:
        if replace_existing:
            database.execute(
                delete(MasterCatalog).where(
                    MasterCatalog.source_file == source_name
                )
            )
            database.commit()

        batch: list[MasterCatalog] = []

        with import_file.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as file_handle:
            reader = csv.DictReader(
                file_handle,
                delimiter="\t",
            )

            required_columns = {
                "upc",
                "description",
                "cost",
                "cs",
                "weight",
            }

            actual_columns = set(
                reader.fieldnames or []
            )

            missing_columns = (
                required_columns - actual_columns
            )

            if missing_columns:
                raise ValueError(
                    "Missing required columns: "
                    + ", ".join(
                        sorted(missing_columns)
                    )
                )

            for source_row, row in enumerate(
                reader,
                start=2,
            ):
                counters["rows_read"] += 1

                (
                    barcode_exact,
                    barcode_lookup,
                    import_status,
                    review_notes,
                ) = normalize_barcode(
                    row.get("upc")
                )

                description = clean_text(
                    row.get("description")
                )

                unit_cost = parse_decimal(
                    row.get("cost")
                )

                case_quantity = parse_integer(
                    row.get("cs")
                )

                unit_weight = parse_decimal(
                    row.get("weight")
                )

                review_messages: list[str] = []

                if review_notes:
                    review_messages.append(review_notes)

                if description is None:
                    counters[
                        "missing_description"
                    ] += 1

                    import_status = "needs_review"
                    review_messages.append(
                        "Description is blank."
                    )

                raw_cost = clean_text(
                    row.get("cost")
                )

                if (
                    raw_cost is not None
                    and unit_cost is None
                ):
                    counters["invalid_cost"] += 1

                    import_status = "needs_review"
                    review_messages.append(
                        "Cost could not be read."
                    )

                raw_case_quantity = clean_text(
                    row.get("cs")
                )

                if (
                    raw_case_quantity is not None
                    and case_quantity is None
                ):
                    counters[
                        "invalid_case_quantity"
                    ] += 1

                    import_status = "needs_review"
                    review_messages.append(
                        "Master-case quantity could not be read."
                    )

                raw_weight = clean_text(
                    row.get("weight")
                )

                if (
                    raw_weight is not None
                    and unit_weight is None
                ):
                    counters[
                        "invalid_weight"
                    ] += 1

                    import_status = "needs_review"
                    review_messages.append(
                        "Weight could not be read."
                    )

                catalog_record = MasterCatalog(
                    barcode_raw=clean_text(
                        row.get("upc")
                    ),
                    barcode_exact=barcode_exact,
                    barcode_lookup=barcode_lookup,
                    description=description,
                    unit_cost=unit_cost,
                    master_case_quantity=case_quantity,
                    unit_weight=unit_weight,
                    source_row_number=source_row,
                    source_file=source_name,
                    import_status=import_status,
                    review_notes=(
                        " ".join(review_messages)
                        if review_messages
                        else None
                    ),
                )

                batch.append(catalog_record)

                counters[import_status] += 1

                if len(batch) >= BATCH_SIZE:
                    database.add_all(batch)
                    database.commit()
                    batch.clear()

                    print(
                        f"Imported "
                        f"{counters['rows_read']:,} rows..."
                    )

            if batch:
                database.add_all(batch)
                database.commit()

        total_records = database.scalar(
            select(
                func.count(
                    MasterCatalog.catalog_id
                )
            )
        )

        duplicate_lookup_values = database.execute(
            select(
                MasterCatalog.barcode_lookup,
                func.count(
                    MasterCatalog.catalog_id
                ).label("record_count"),
            )
            .where(
                MasterCatalog.barcode_lookup.is_not(
                    None
                )
            )
            .group_by(
                MasterCatalog.barcode_lookup
            )
            .having(
                func.count(
                    MasterCatalog.catalog_id
                ) > 1
            )
        ).all()

    print("")
    print("Master catalog import complete.")
    print(f"Source file: {import_file}")
    print(
        f"Rows read: "
        f"{counters['rows_read']:,}"
    )
    print(
        f"Imported normally: "
        f"{counters['imported']:,}"
    )
    print(
        f"Needs review: "
        f"{counters['needs_review']:,}"
    )
    print(
        f"Total catalog records: "
        f"{total_records:,}"
    )
    print(
        f"Repeated barcode lookup values: "
        f"{len(duplicate_lookup_values):,}"
    )
    print(
        f"Missing descriptions: "
        f"{counters['missing_description']:,}"
    )
    print(
        f"Invalid costs: "
        f"{counters['invalid_cost']:,}"
    )
    print(
        f"Invalid master-case quantities: "
        f"{counters['invalid_case_quantity']:,}"
    )
    print(
        f"Invalid weights: "
        f"{counters['invalid_weight']:,}"
    )


if __name__ == "__main__":
    import_master_catalog(
        import_file=DEFAULT_IMPORT_FILE,
        replace_existing=True,
    )
