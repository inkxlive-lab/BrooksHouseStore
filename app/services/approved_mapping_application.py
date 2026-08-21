"""Preflight and apply explicitly approved channel product mappings only."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from app.services.channel_mapping_analysis import analyze_unmatched


@dataclass
class MappingPlan:
    channel: str
    source_key: str
    product_id: int
    product_name: str
    source_title: str
    source_sku: str
    source_barcode: str
    line_ids: list[dict]
    affected_lines: int
    affected_units: int
    status: str
    reason: str
    operations: list[str]

    def as_dict(self) -> dict:
        return asdict(self)


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def integrity_check(connection: sqlite3.Connection) -> str:
    return str(connection.execute("PRAGMA integrity_check").fetchone()[0])


def inventory_fingerprint(connection: sqlite3.Connection) -> dict:
    digest = hashlib.sha256()
    rows = connection.execute(
        """SELECT inventory_id,product_id,location_id,COALESCE(container_id,''),
                  quantity_on_hand,quantity_reserved,reorder_level,COALESCE(updated_at,'')
             FROM inventory ORDER BY inventory_id"""
    ).fetchall()
    for row in rows:
        digest.update(json.dumps(list(row), separators=(",", ":"), default=str).encode("utf-8"))
        digest.update(b"\n")
    transactions = connection.execute(
        "SELECT COUNT(*),COALESCE(MAX(transaction_id),0) FROM inventory_transactions"
    ).fetchone()
    totals = connection.execute(
        "SELECT COALESCE(SUM(quantity_on_hand),0),COALESCE(SUM(quantity_reserved),0) FROM inventory"
    ).fetchone()
    return {
        "row_count": len(rows), "quantity_on_hand_total": int(totals[0]),
        "quantity_reserved_total": int(totals[1]), "rows_sha256": digest.hexdigest().upper(),
        "inventory_transaction_count": int(transactions[0]),
        "inventory_transaction_max_id": int(transactions[1]),
    }


def verified_backup(source_path: str | Path, backup_path: str | Path) -> dict:
    source = Path(source_path).resolve()
    target = Path(backup_path).resolve()
    if target.exists():
        raise RuntimeError(f"Backup target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    backup_connection = sqlite3.connect(target)
    try:
        source_connection.backup(backup_connection)
        backup_connection.commit()
        check = integrity_check(backup_connection)
        if check != "ok":
            raise RuntimeError(f"Backup integrity check failed: {check}")
    finally:
        backup_connection.close()
        source_connection.close()
    return {"path": str(target), "sha256": file_sha256(target), "integrity_check": check}


def load_approved_report(path: str | Path) -> dict:
    report_path = Path(path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    strong = [row for row in payload.get("proposals", []) if row.get("match_classification") == "B_STRONG"]
    if len(strong) != 42:
        raise RuntimeError(f"Expected exactly 42 approved STRONG identities; report contains {len(strong)}")
    if sum(int(row.get("order_line_count") or 0) for row in strong) != 59:
        raise RuntimeError("Approved report no longer contains exactly 59 STRONG order lines")
    if sum(int(row.get("unit_count") or 0) for row in strong) != 92:
        raise RuntimeError("Approved report no longer contains exactly 92 STRONG units")
    return payload


def _line_state(connection: sqlite3.Connection, channel: str, item: dict) -> sqlite3.Row | None:
    if channel == "shopify":
        return connection.execute(
            "SELECT product_id,sku,barcode FROM shopify_sales_lines WHERE shopify_line_id=? AND shopify_order_id=?",
            (item["order_line_id"], item["order_id"]),
        ).fetchone()
    if channel == "amazon":
        return connection.execute(
            "SELECT product_id,seller_sku sku,'' barcode FROM amazon_order_item_history WHERE amazon_order_id=? AND order_item_id=?",
            (item["order_id"], item["order_line_id"]),
        ).fetchone()
    return connection.execute(
        "SELECT product_id,sku,upc barcode FROM walmart_order_lines WHERE order_line_id=? AND purchase_order_id=?",
        (int(item["order_line_id"]), item["order_id"]),
    ).fetchone()


def preflight(connection: sqlite3.Connection, report: dict) -> list[MappingPlan]:
    connection.row_factory = sqlite3.Row
    cutoff = str(report.get("summary", {}).get("cutoff") or "")
    current_proposals, _ = analyze_unmatched(connection, cutoff)
    current = {item.source_key: item for item in current_proposals}
    plans = []
    for row in (item for item in report["proposals"] if item["match_classification"] == "B_STRONG"):
        channel = str(row["channel"])
        source_key = str(row["source_key"])
        product_id = int(row["candidate_product_id"])
        identifiers = json.loads(row["marketplace_identifiers"])
        line_ids = json.loads(row["marketplace_order_item_identifiers"])
        operations = ["upsert channel_sales_product_rules", "insert channel_match_audit"]
        reasons = []
        product = connection.execute(
            "SELECT product_id,product_name,COALESCE(active,1) active FROM products WHERE product_id=?", (product_id,)
        ).fetchone()
        if product is None or int(product["active"] or 0) != 1:
            reasons.append("reviewed BrooksHouse product is missing or inactive")
        reviewed_barcode = str(row.get("candidate_product_barcode") or "").strip()
        if reviewed_barcode and connection.execute(
            "SELECT 1 FROM product_barcodes WHERE product_id=? AND barcode=?", (product_id, reviewed_barcode)
        ).fetchone() is None:
            reasons.append("reviewed BrooksHouse barcode no longer belongs to the product")
        fresh = current.get(source_key)
        if fresh is None or fresh.match_classification != "B_STRONG" or fresh.candidate_product_id != product_id:
            reasons.append("current read-only analysis no longer reproduces the approved STRONG mapping")
        rule = connection.execute(
            "SELECT product_id,rule_status FROM channel_sales_product_rules WHERE source_key=?", (source_key,)
        ).fetchone()
        if rule is not None and rule["product_id"] not in (None, product_id):
            reasons.append(f"existing channel rule points to product {rule['product_id']}")
        mapped = 0
        for item in line_ids:
            state = _line_state(connection, channel, item)
            if state is None:
                reasons.append(f"approved order line is missing: {item}")
            elif state["product_id"] not in (None, product_id):
                reasons.append(f"approved order line already points to product {state['product_id']}: {item}")
            elif state["product_id"] == product_id:
                mapped += 1
        if channel == "shopify":
            operations.append("map approved shopify_sales_lines")
        elif channel == "amazon":
            operations.extend(["map approved amazon_order_item_history rows", "preserve/update matching amazon_product_links"])
            links = connection.execute(
                """SELECT DISTINCT apl.product_id FROM amazon_listings al JOIN amazon_product_links apl
                     ON apl.amazon_listing_id=al.amazon_listing_id
                    WHERE (?<>'' AND al.seller_sku=?) OR (?<>'' AND al.asin=?)""",
                (identifiers.get("sku", ""), identifiers.get("sku", ""), identifiers.get("external_id", ""), identifiers.get("external_id", "")),
            ).fetchall()
            if not links:
                reasons.append("no Amazon listing/link exists for the approved identity")
            elif any(link[0] not in (None, product_id) for link in links):
                reasons.append("an Amazon listing link points to a different product")
        else:
            operations.extend(["map approved walmart_order_lines", "insert/update walmart_listings and walmart_product_links"])
            sku = str(identifiers.get("sku") or "").strip()
            if not sku:
                reasons.append("Walmart identity has no seller SKU")
            existing = connection.execute(
                """SELECT wpl.product_id FROM walmart_listings wl LEFT JOIN walmart_product_links wpl
                     ON wpl.walmart_listing_id=wl.walmart_listing_id
                    WHERE trim(wl.seller_sku)=? COLLATE NOCASE""", (sku,)
            ).fetchall()
            if any(link[0] not in (None, product_id) for link in existing):
                reasons.append("an existing Walmart listing link points to a different product")
        plans.append(MappingPlan(
            channel=channel, source_key=source_key, product_id=product_id,
            product_name=str(product["product_name"] if product else row.get("candidate_product_name") or ""),
            source_title=str(row.get("marketplace_title") or ""), source_sku=str(identifiers.get("sku") or ""),
            source_barcode=str(identifiers.get("barcode") or ""), line_ids=line_ids,
            affected_lines=int(row["order_line_count"]), affected_units=int(row["unit_count"]),
            status="conflict" if reasons else ("already_mapped" if mapped == len(line_ids) else "safe"),
            reason="; ".join(reasons), operations=operations,
        ))
    return plans


def _upsert_rule_and_audit(connection: sqlite3.Connection, plan: MappingPlan, now: str) -> None:
    connection.execute(
        """INSERT INTO channel_sales_product_rules
           (channel_name,source_key,source_title,source_sku,source_barcode,product_id,rule_status,created_at,updated_at)
           VALUES(?,?,?,?,?,?,'active',?,?)
           ON CONFLICT(source_key) DO UPDATE SET product_id=excluded.product_id,
             rule_status='active',updated_at=excluded.updated_at
           WHERE channel_sales_product_rules.product_id IS NULL
              OR channel_sales_product_rules.product_id=excluded.product_id""",
        (plan.channel, plan.source_key, plan.source_title, plan.source_sku, plan.source_barcode, plan.product_id, now, now),
    )
    connection.execute(
        """INSERT INTO channel_match_audit
           (channel_name,source_key,source_title,source_sku,source_barcode,product_id,
            action_name,match_method,confidence,affected_lines,affected_units,affected_sales,
            source_row_ids_json,created_at) VALUES(?,?,?,?,?,?,'approved_strong_apply',
            'martel_review_20260821',95,?,?,0,?,?)""",
        (plan.channel, plan.source_key, plan.source_title, plan.source_sku, plan.source_barcode,
         plan.product_id, plan.affected_lines, plan.affected_units, json.dumps(plan.line_ids), now),
    )


def apply_safe_plans(connection: sqlite3.Connection, plans: list[MappingPlan]) -> list[dict]:
    connection.row_factory = sqlite3.Row
    results = []
    now = datetime.now().astimezone().isoformat()
    for index, plan in enumerate(plans):
        if plan.status == "conflict":
            results.append({**plan.as_dict(), "apply_status": "conflict_skipped"})
            continue
        if plan.status == "already_mapped":
            results.append({**plan.as_dict(), "apply_status": "already_correct"})
            continue
        savepoint = f"approved_mapping_{index}"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            if plan.channel == "shopify":
                for item in plan.line_ids:
                    connection.execute(
                        """UPDATE shopify_sales_lines SET product_id=?,match_status='matched',
                                  match_method='martel_review_20260821',updated_at=?
                            WHERE shopify_line_id=? AND shopify_order_id=? AND product_id IS NULL""",
                        (plan.product_id, now, item["order_line_id"], item["order_id"]),
                    )
            elif plan.channel == "amazon":
                for item in plan.line_ids:
                    connection.execute(
                        """UPDATE amazon_order_item_history SET product_id=?
                            WHERE amazon_order_id=? AND order_item_id=? AND product_id IS NULL""",
                        (plan.product_id, item["order_id"], item["order_line_id"]),
                    )
                connection.execute(
                    """UPDATE amazon_product_links SET product_id=?,match_status='linked',
                              match_method='martel_review_20260821',match_value=?,linked_at=?
                        WHERE amazon_listing_id IN (SELECT amazon_listing_id FROM amazon_listings
                         WHERE (?<>'' AND seller_sku=?) OR (?<>'' AND asin=?))
                          AND (product_id IS NULL OR product_id=?)""",
                    (plan.product_id, plan.source_key, now, plan.source_sku, plan.source_sku,
                     plan.source_key.split(":", 2)[-1], plan.source_key.split(":", 2)[-1], plan.product_id),
                )
            else:
                for item in plan.line_ids:
                    connection.execute(
                        "UPDATE walmart_order_lines SET product_id=? WHERE order_line_id=? AND purchase_order_id=? AND product_id IS NULL",
                        (plan.product_id, int(item["order_line_id"]), item["order_id"]),
                    )
                listing = connection.execute(
                    "SELECT walmart_listing_id FROM walmart_listings WHERE trim(seller_sku)=? COLLATE NOCASE",
                    (plan.source_sku,),
                ).fetchone()
                if listing is None:
                    cursor = connection.execute(
                        "INSERT INTO walmart_listings(seller_sku,item_name,created_at,updated_at) VALUES(?,?,?,?)",
                        (plan.source_sku, plan.source_title, now, now),
                    )
                    listing_id = int(cursor.lastrowid)
                else:
                    listing_id = int(listing[0])
                link = connection.execute(
                    "SELECT walmart_product_link_id FROM walmart_product_links WHERE walmart_listing_id=?", (listing_id,)
                ).fetchone()
                if link is None:
                    connection.execute(
                        "INSERT INTO walmart_product_links(walmart_listing_id,product_id,match_status,matched_at) VALUES(?,?,'linked',?)",
                        (listing_id, plan.product_id, now),
                    )
                else:
                    connection.execute(
                        "UPDATE walmart_product_links SET product_id=?,match_status='linked',matched_at=? WHERE walmart_product_link_id=? AND (product_id IS NULL OR product_id=?)",
                        (plan.product_id, now, int(link[0]), plan.product_id),
                    )
            _upsert_rule_and_audit(connection, plan, now)
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            results.append({**plan.as_dict(), "apply_status": "applied"})
        except Exception as error:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            results.append({**plan.as_dict(), "apply_status": "failed", "failure": str(error)})
    return results
