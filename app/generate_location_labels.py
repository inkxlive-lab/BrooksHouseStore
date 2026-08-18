import argparse
import csv
from datetime import datetime
from pathlib import Path

from reportlab.graphics.barcode import code128
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


PROJECT_ROOT = Path(__file__).resolve().parents[1]

OUTPUT_DIRECTORY = (
    PROJECT_ROOT
    / "labels"
)

REGISTRY_PATH = (
    OUTPUT_DIRECTORY
    / "brookshouse-label-registry.csv"
)


def build_sequential_labels(
    prefix: str,
    count: int,
    label_type: str,
    location_name: str,
    description_prefix: str,
) -> list[dict[str, str]]:
    """Create sequential BrooksHouse label records."""

    records = []

    for number in range(1, count + 1):
        label_id = f"{prefix}-{number:03d}"

        records.append(
            {
                "label_id": label_id,
                "label_type": label_type,
                "location_name": location_name,
                "description": (
                    f"{description_prefix} {number}"
                ),
                "active": "Yes",
                "created_at": datetime.now().isoformat(
                    timespec="seconds"
                ),
            }
        )

    return records


def write_registry(
    records: list[dict[str, str]],
) -> None:
    """Create or update the label registry CSV."""

    OUTPUT_DIRECTORY.mkdir(
        parents=True,
        exist_ok=True,
    )

    existing_records = {}

    if REGISTRY_PATH.exists():
        with REGISTRY_PATH.open(
            "r",
            newline="",
            encoding="utf-8-sig",
        ) as registry_file:
            for row in csv.DictReader(
                registry_file
            ):
                existing_records[
                    row["label_id"]
                ] = row

    for record in records:
        existing_records[
            record["label_id"]
        ] = record

    sorted_records = sorted(
        existing_records.values(),
        key=lambda row: row["label_id"],
    )

    with REGISTRY_PATH.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as registry_file:
        writer = csv.DictWriter(
            registry_file,
            fieldnames=[
                "label_id",
                "label_type",
                "location_name",
                "description",
                "active",
                "created_at",
            ],
        )

        writer.writeheader()
        writer.writerows(sorted_records)


def fit_text(
    pdf: canvas.Canvas,
    text: str,
    maximum_width: float,
    starting_size: int,
    minimum_size: int = 7,
) -> int:
    """Choose a font size that fits the label."""

    font_size = starting_size

    while font_size > minimum_size:
        width = pdf.stringWidth(
            text,
            "Helvetica-Bold",
            font_size,
        )

        if width <= maximum_width:
            break

        font_size -= 1

    return font_size


def draw_label(
    pdf: canvas.Canvas,
    record: dict[str, str],
    x: float,
    y: float,
    width: float,
    height: float,
    large_format: bool,
) -> None:
    """Draw one Code 128 label."""

    label_id = record["label_id"]
    label_type = record["label_type"]
    location_name = record["location_name"]

    padding = 10 if large_format else 5

    pdf.setLineWidth(
        0.5
    )

    pdf.rect(
        x,
        y,
        width,
        height,
        stroke=1,
        fill=0,
    )

    if large_format:
        heading_size = 12
        location_size = 10
        id_size = 15
        barcode_height = 48
        barcode_width = 1.05
    else:
        heading_size = 7
        location_size = 6
        id_size = 9
        barcode_height = 25
        barcode_width = 0.65

    pdf.setFont(
        "Helvetica-Bold",
        heading_size,
    )

    pdf.drawCentredString(
        x + (width / 2),
        y + height - padding - heading_size,
        "BROOKSHOUSE STORE",
    )

    location_font_size = fit_text(
        pdf,
        location_name.upper(),
        width - (padding * 2),
        location_size,
        minimum_size=5,
    )

    pdf.setFont(
        "Helvetica-Bold",
        location_font_size,
    )

    pdf.drawCentredString(
        x + (width / 2),
        y + height - padding - heading_size - 14,
        location_name.upper(),
    )

    barcode = code128.Code128(
        label_id,
        barHeight=barcode_height,
        barWidth=barcode_width,
        humanReadable=False,
    )

    available_width = (
        width
        - (padding * 2)
    )

    if barcode.width > available_width:
        scale_factor = (
            available_width
            / barcode.width
        )

        barcode.barWidth *= scale_factor
        barcode._calculate()

    barcode_x = (
        x
        + (
            width
            - barcode.width
        )
        / 2
    )

    barcode_y = (
        y
        + (
            35
            if large_format
            else 17
        )
    )

    barcode.drawOn(
        pdf,
        barcode_x,
        barcode_y,
    )

    id_font_size = fit_text(
        pdf,
        label_id,
        width - (padding * 2),
        id_size,
        minimum_size=6,
    )

    pdf.setFont(
        "Helvetica-Bold",
        id_font_size,
    )

    pdf.drawCentredString(
        x + (width / 2),
        y + (
            14
            if large_format
            else 6
        ),
        label_id,
    )

    if large_format:
        pdf.setFont(
            "Helvetica",
            7,
        )

        pdf.drawString(
            x + padding,
            y + height - padding - 42,
            f"Type: {label_type}",
        )


def create_large_label_pdf(
    records: list[dict[str, str]],
    output_path: Path,
) -> None:
    """Create 4 x 2 inch labels on letter paper."""

    page_width, page_height = letter

    label_width = 4 * 72
    label_height = 2 * 72

    columns = 2
    rows = 5

    horizontal_margin = (
        page_width
        - columns * label_width
    ) / 2

    vertical_margin = (
        page_height
        - rows * label_height
    ) / 2

    pdf = canvas.Canvas(
        str(output_path),
        pagesize=letter,
    )

    labels_per_page = columns * rows

    for index, record in enumerate(records):
        page_position = (
            index
            % labels_per_page
        )

        if (
            index > 0
            and page_position == 0
        ):
            pdf.showPage()

        row = (
            page_position
            // columns
        )

        column = (
            page_position
            % columns
        )

        x = (
            horizontal_margin
            + column * label_width
        )

        y = (
            page_height
            - vertical_margin
            - (row + 1) * label_height
        )

        draw_label(
            pdf=pdf,
            record=record,
            x=x,
            y=y,
            width=label_width,
            height=label_height,
            large_format=True,
        )

    pdf.save()


def create_shelf_label_pdf(
    records: list[dict[str, str]],
    output_path: Path,
) -> None:
    """Create Avery 5160-style shelf labels."""

    page_width, page_height = letter

    label_width = 2.625 * 72
    label_height = 1 * 72

    columns = 3
    rows = 10

    left_margin = 0.1875 * 72
    top_margin = 0.5 * 72

    horizontal_gap = 0.125 * 72

    pdf = canvas.Canvas(
        str(output_path),
        pagesize=letter,
    )

    labels_per_page = columns * rows

    for index, record in enumerate(records):
        page_position = (
            index
            % labels_per_page
        )

        if (
            index > 0
            and page_position == 0
        ):
            pdf.showPage()

        row = (
            page_position
            // columns
        )

        column = (
            page_position
            % columns
        )

        x = (
            left_margin
            + column
            * (
                label_width
                + horizontal_gap
            )
        )

        y = (
            page_height
            - top_margin
            - (row + 1) * label_height
        )

        draw_label(
            pdf=pdf,
            record=record,
            x=x,
            y=y,
            width=label_width,
            height=label_height,
            large_format=False,
        )

    pdf.save()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Generate BrooksHouse Code 128 "
            "tote and shelf labels."
        )
    )

    parser.add_argument(
        "--backroom-totes",
        type=int,
        default=50,
    )

    parser.add_argument(
        "--prob-totes",
        type=int,
        default=20,
    )

    parser.add_argument(
        "--storefront-shelves",
        type=int,
        default=0,
    )

    parser.add_argument(
        "--backroom-shelves",
        type=int,
        default=0,
    )

    arguments = parser.parse_args()

    OUTPUT_DIRECTORY.mkdir(
        parents=True,
        exist_ok=True,
    )

    tote_records = []

    tote_records.extend(
        build_sequential_labels(
            prefix="BR-TOTE",
            count=arguments.backroom_totes,
            label_type="Back Room Tote",
            location_name="Store Back Room",
            description_prefix="Back room tote",
        )
    )

    tote_records.extend(
        build_sequential_labels(
            prefix="PROB-TOTE",
            count=arguments.prob_totes,
            label_type="Inventory Review Tote",
            location_name="PROB - Inventory Review",
            description_prefix="Inventory review tote",
        )
    )

    shelf_records = []

    shelf_records.extend(
        build_sequential_labels(
            prefix="SF-SHELF",
            count=arguments.storefront_shelves,
            label_type="Storefront Shelf",
            location_name="BrooksHouse Storefront",
            description_prefix="Storefront shelf",
        )
    )

    shelf_records.extend(
        build_sequential_labels(
            prefix="BR-SHELF",
            count=arguments.backroom_shelves,
            label_type="Back Room Shelf",
            location_name="Store Back Room",
            description_prefix="Back room shelf",
        )
    )

    all_records = (
        tote_records
        + shelf_records
    )

    write_registry(
        all_records
    )

    timestamp = datetime.now().strftime(
        "%Y%m%d-%H%M%S"
    )

    if tote_records:
        tote_pdf_path = (
            OUTPUT_DIRECTORY
            / (
                "brookshouse-tote-labels-"
                f"{timestamp}.pdf"
            )
        )

        create_large_label_pdf(
            tote_records,
            tote_pdf_path,
        )

        print(
            "Tote label PDF:",
            tote_pdf_path,
        )

    if shelf_records:
        shelf_pdf_path = (
            OUTPUT_DIRECTORY
            / (
                "brookshouse-shelf-labels-"
                f"{timestamp}.pdf"
            )
        )

        create_shelf_label_pdf(
            shelf_records,
            shelf_pdf_path,
        )

        print(
            "Shelf label PDF:",
            shelf_pdf_path,
        )

    print(
        "Label registry:",
        REGISTRY_PATH,
    )

    print()
    print(
        f"Back room tote labels: "
        f"{arguments.backroom_totes}"
    )

    print(
        f"PROB tote labels: "
        f"{arguments.prob_totes}"
    )

    print(
        f"Storefront shelf labels: "
        f"{arguments.storefront_shelves}"
    )

    print(
        f"Back room shelf labels: "
        f"{arguments.backroom_shelves}"
    )


if __name__ == "__main__":
    main()
