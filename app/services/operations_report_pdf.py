"""PDF rendering for immutable Operations Reports snapshots."""

from __future__ import annotations

from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


DEFAULT_REPORT_DIRECTORY = Path(__file__).resolve().parents[2] / "reports" / "operations"


def _paragraph(value: Any, style: ParagraphStyle) -> Paragraph:
    text = str(value or "").replace("—", "-").replace("–", "-")
    return Paragraph(escape(text).replace("\n", "<br/>"), style)


def render_report_pdf(metadata: dict, snapshot: dict) -> bytes:
    """Render one immutable snapshot without reading or changing application data."""
    output = BytesIO()
    document = SimpleDocTemplate(output, pagesize=landscape(letter), leftMargin=.28*inch,
        rightMargin=.28*inch, topMargin=.3*inch, bottomMargin=.3*inch,
        title=str(snapshot.get("report_title") or "Operations Report"))
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Small", parent=styles["BodyText"], fontSize=6.8, leading=8.2))
    styles.add(ParagraphStyle(name="SmallBold", parent=styles["Small"], fontName="Helvetica-Bold"))
    styles.add(ParagraphStyle(name="Tiny", parent=styles["Small"], fontSize=6.1, leading=7.2))
    styles.add(ParagraphStyle(name="Metric", parent=styles["BodyText"], fontSize=8, leading=9, alignment=TA_CENTER))
    story = [Paragraph(str(snapshot.get("report_title") or "Operations Report"), styles["Title"]),
        Paragraph(f"Immutable snapshot #{metadata.get('report_run_id')} - created {metadata.get('created_at')} UTC - "
                  f"SHA-256 {metadata.get('snapshot_sha256')}", styles["Small"]), Spacer(1, 6)]
    totals = snapshot.get("totals") or {}
    metrics = [("Active orders", totals.get("active_orders", 0)), ("Units required", totals.get("units_required", 0)),
        ("Products", totals.get("unique_aggregated_products", 0)), ("Remaining units", totals.get("remaining_units_to_pull", 0)),
        ("Blocked / at risk", totals.get("blocked_or_at_risk", totals.get("exceptions", 0))),
        ("Unmatched items", totals.get("unmatched_items", 0))]
    metric_table = Table([[Paragraph(f"<b>{escape(label)}</b><br/>{escape(str(value))}", styles["Metric"]) for label,value in metrics]],
                         colWidths=[1.68*inch]*len(metrics))
    metric_table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#EAF2F8")),
        ("BOX",(0,0),(-1,-1),.8,colors.HexColor("#123B5D")),("INNERGRID",(0,0),(-1,-1),.4,colors.HexColor("#7993A6")),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)]))
    story.extend([metric_table, Spacer(1, 6)])
    for warning in snapshot.get("warnings") or []:
        box = Table([[_paragraph(f"INCOMPLETE / STALE DATA WARNING: {warning}", styles["SmallBold"])]], colWidths=[10.1*inch])
        box.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#FFF0F1")),
            ("BOX",(0,0),(-1,-1),1.2,colors.HexColor("#9D252B")),("PADDING",(0,0),(-1,-1),5)]))
        story.extend([box, Spacer(1, 4)])
    criteria = snapshot.get("filters") or {}
    if criteria:
        def criterion_value(value: Any) -> str:
            if isinstance(value, bool):
                return "Yes" if value else "No"
            if isinstance(value, (list, tuple, set)):
                return ", ".join(str(item) for item in value) or "None"
            return str(value or "Not set")
        criteria_rows = [[_paragraph("Report Criteria (read-only)", styles["SmallBold"]), "", "", ""]]
        items = [(str(key).replace("_", " ").title(), criterion_value(value)) for key, value in criteria.items()]
        for index in range(0, len(items), 4):
            cells = [_paragraph(f"{label}: {value}", styles["Tiny"]) for label, value in items[index:index + 4]]
            criteria_rows.append(cells + [""] * (4 - len(cells)))
        criteria_table = Table(criteria_rows, colWidths=[2.525*inch]*4)
        criteria_table.setStyle(TableStyle([
            ("SPAN", (0, 0), (-1, 0)), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF2F8")),
            ("BOX", (0, 0), (-1, -1), .6, colors.HexColor("#7993A6")),
            ("INNERGRID", (0, 1), (-1, -1), .3, colors.HexColor("#C9D2D8")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("PADDING", (0, 0), (-1, -1), 4),
        ]))
        story.extend([criteria_table, Spacer(1, 6)])
    headers = ["Picking checklist","Product / mapping","Qty","Barcode / SKUs","Orders / deadlines","Exact locations","Blocked / at-risk notes"]
    data = [[_paragraph(value, styles["SmallBold"]) for value in headers]]
    pull_rows = snapshot.get("pull_rows") or []
    for row in pull_rows:
        checks = "[ ] Found\n[ ] Partial\n[ ] Not found\n[ ] Damaged\n[ ] Loaded"
        mapping = f"{row.get('product','Unknown')}\n{row.get('exception') or row.get('mapping_status') or 'Mapped'}"
        quantity = f"{row.get('units_required',0)} required\n{row.get('units_picked_staged',0)} picked/staged\n{row.get('remaining_to_pull',0)} remaining"
        identifiers = f"{row.get('barcode') or 'No barcode'}\n{', '.join(row.get('skus') or []) or 'No SKU'}"
        orders = "\n".join(f"{x.get('channel')} {x.get('order_id')} - due {x.get('ship_by_central')}" for x in row.get("orders") or []) or "No contributing order"
        locations = "\n".join(f"{x.get('site')} > {x.get('location')} / {x.get('container')} - {x.get('available')} available" for x in row.get("locations") or []) or "UNKNOWN / MANUAL SEARCH"
        risk = row.get("exception") or "Mapped - ready"
        if row.get("shortage_quantity"): risk += f"\nShortage: {row['shortage_quantity']}"
        data.append([_paragraph(checks,styles["SmallBold"]),_paragraph(mapping,styles["Small"]),
            _paragraph(quantity,styles["SmallBold"]),_paragraph(identifiers,styles["Tiny"]),
            _paragraph(orders,styles["Tiny"]),_paragraph(locations,styles["Tiny"]),_paragraph(risk,styles["Small"])])
    if not pull_rows: data.append([_paragraph("No remaining units require pulling.",styles["Small"])] + [""]*6)
    table = Table(data, repeatRows=1, colWidths=[.8*inch,1.65*inch,.85*inch,1.15*inch,1.75*inch,2.25*inch,1.65*inch])
    table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#123B5D")),("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("GRID",(0,0),(-1,-1),.45,colors.HexColor("#555555")),("VALIGN",(0,0),(-1,-1),"TOP"),
        ("LEFTPADDING",(0,0),(-1,-1),3),("RIGHTPADDING",(0,0),(-1,-1),3),("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3)]))
    for index,row in enumerate(pull_rows,start=1):
        if row.get("exception_code") or row.get("shortage_quantity"):
            table.setStyle(TableStyle([("BACKGROUND",(0,index),(-1,index),colors.HexColor("#FFF1D6"))]))
    story.append(table)
    def footer(canvas, doc):
        canvas.saveState(); canvas.setFont("Helvetica",7)
        canvas.drawString(doc.leftMargin,.16*inch,"BrooksHouse Operations Reports - immutable reconciled snapshot")
        canvas.drawRightString(landscape(letter)[0]-doc.rightMargin,.16*inch,f"Page {doc.page}"); canvas.restoreState()
    document.build(story,onFirstPage=footer,onLaterPages=footer)
    return output.getvalue()


def write_report_pdf(metadata: dict, snapshot: dict, output_directory: Path | None = None) -> Path:
    directory = Path(output_directory or DEFAULT_REPORT_DIRECTORY).resolve(); directory.mkdir(parents=True,exist_ok=True)
    path = directory / f"operations-report-{int(metadata['report_run_id'])}.pdf"
    path.write_bytes(render_report_pdf(metadata,snapshot)); return path
