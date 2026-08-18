from pathlib import Path


main_path = Path(r"C:\BrooksHouseStore\app\main.py")
text = main_path.read_text(encoding="utf-8-sig")


text = text.replace(
    "from io import StringIO, InvalidOperation",
    "from io import StringIO",
)

if "from decimal import Decimal, InvalidOperation" not in text:
    text = text.replace(
        "from decimal import Decimal",
        "from decimal import Decimal, InvalidOperation",
        1,
    )


main_path.write_text(
    text,
    encoding="utf-8",
)

print("CSV imports repaired successfully.")
