"""Read-only grouped analysis of unmatched marketplace order lines."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher

from app.services.channel_inventory_reconciliation import (
    _inventory_by_location,
    _lifecycle,
    reconcile,
)


RANK_LABELS = {
    1: "exact_identifier_match",
    2: "strong_existing_cross_reference",
    3: "high_confidence_human_approval",
    4: "no_viable_candidate",
}


@dataclass(frozen=True)
class MappingProposal:
    channel: str
    source_key: str
    marketplace_title: str
    marketplace_identifiers: str
    order_line_count: int
    unit_count: int
    order_count: int
    candidate_product_id: int | None
    candidate_product_name: str
    evidence: str
    confidence_rank: int
    confidence_level: str
    candidate_alternatives: str
    projected_line_outcomes: str

    def as_dict(self) -> dict:
        return asdict(self)


def _text(value: object) -> str:
    return str(value or "").strip()


def _barcode(value: object) -> str:
    digits = re.sub(r"\D", "", _text(value))
    return (digits.lstrip("0") or "0") if 8 <= len(digits) <= 14 else ""


def _normalized_title(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", _text(value).casefold()))


def _source_key(channel: str, row: dict) -> str:
    if channel == "shopify":
        choices = (("variant", row.get("external_variant_id")), ("barcode", _barcode(row.get("barcode"))), ("sku", row.get("sku")), ("product", row.get("external_product_id")))
        fallback = _normalized_title(f"{row.get('title', '')} {row.get('variant_title', '')}")
    elif channel == "amazon":
        choices = (("sku", row.get("sku")), ("asin", row.get("external_id")))
        fallback = _normalized_title(row.get("title"))
    else:
        choices = (("barcode", _barcode(row.get("barcode"))), ("sku", row.get("sku")))
        fallback = _normalized_title(row.get("title"))
    for kind, value in choices:
        if _text(value):
            return f"{channel}:{kind}:{_text(value).casefold()}"
    return f"{channel}:title:{fallback or 'unknown'}"


def _unmatched_lines(connection: sqlite3.Connection, cutoff: str) -> list[dict]:
    rows: list[dict] = []
    for row in connection.execute(
        """SELECT 'shopify' channel,o.shopify_order_id order_id,
                  l.shopify_line_id order_line_id,l.title,l.variant_title,l.sku,
                  l.barcode,l.shopify_product_id external_product_id,
                  l.shopify_variant_id external_variant_id,'' external_id,
                  l.quantity units,l.current_quantity,o.cancelled_at,
                  o.fulfillment_status,'' line_status,'' fulfilled_by,
                  o.refund_amount,o.processed_at source_updated_at
             FROM shopify_sales_lines l JOIN shopify_sales_orders o
               ON o.shopify_order_id=l.shopify_order_id
            WHERE l.product_id IS NULL AND o.test_order=0 AND o.processed_at>=?""",
        (cutoff,),
    ):
        rows.append(dict(row))
    for row in connection.execute(
        """SELECT 'amazon' channel,o.amazon_order_id order_id,
                  i.order_item_id order_line_id,i.title,'' variant_title,
                  i.seller_sku sku,'' barcode,'' external_product_id,
                  '' external_variant_id,i.asin external_id,i.quantity_ordered units,
                  i.quantity_ordered current_quantity,'' cancelled_at,
                  o.fulfillment_status,'' line_status,o.fulfilled_by,
                  0 refund_amount,o.last_updated_time source_updated_at
             FROM amazon_order_item_history i JOIN amazon_order_history o
               ON o.amazon_order_id=i.amazon_order_id
            WHERE i.product_id IS NULL AND o.created_time>=?""",
        (cutoff,),
    ):
        rows.append(dict(row))
    for row in connection.execute(
        """SELECT 'walmart' channel,o.purchase_order_id order_id,
                  CAST(l.order_line_id AS TEXT) order_line_id,l.item_name title,
                  '' variant_title,l.sku,l.upc barcode,'' external_product_id,
                  '' external_variant_id,l.line_number external_id,l.quantity units,
                  l.quantity current_quantity,'' cancelled_at,o.walmart_status fulfillment_status,
                  l.line_status,'' fulfilled_by,0 refund_amount,o.synced_at source_updated_at
             FROM walmart_order_lines l JOIN walmart_orders o
               ON o.purchase_order_id=l.purchase_order_id
            WHERE l.product_id IS NULL
              AND (CASE WHEN trim(COALESCE(o.order_date,'')) GLOB '[0-9]*'
                        AND length(trim(COALESCE(o.order_date,'')))>=13
                        THEN datetime(CAST(o.order_date AS INTEGER)/1000,'unixepoch')
                        ELSE datetime(o.order_date) END)>=datetime(?)""",
        (cutoff,),
    ):
        rows.append(dict(row))
    return rows


def _product_catalog(connection: sqlite3.Connection) -> tuple[dict[int, str], dict[str, set[int]]]:
    products = {int(row["product_id"]): _text(row["product_name"]) for row in connection.execute(
        "SELECT product_id,product_name FROM products WHERE COALESCE(active,1)=1"
    )}
    barcodes: dict[str, set[int]] = defaultdict(set)
    for row in connection.execute("SELECT product_id,barcode FROM product_barcodes"):
        normalized = _barcode(row["barcode"])
        if normalized and int(row["product_id"]) in products:
            barcodes[normalized].add(int(row["product_id"]))
    return products, barcodes


def _cross_references(connection: sqlite3.Connection) -> dict[tuple[str, str, str], list[tuple[int, str]]]:
    refs: dict[tuple[str, str, str], list[tuple[int, str]]] = defaultdict(list)
    for row in connection.execute(
        """SELECT channel_name,source_key,source_sku,source_barcode,product_id
             FROM channel_sales_product_rules
            WHERE product_id IS NOT NULL AND lower(COALESCE(rule_status,'active'))='active'"""
    ):
        for kind, value in (("source_key", row["source_key"]), ("sku", row["source_sku"]), ("barcode", _barcode(row["source_barcode"]))):
            if _text(value):
                refs[(_text(row["channel_name"]).casefold(), kind, _text(value).casefold())].append((int(row["product_id"]), "channel_sales_product_rules"))
    for row in connection.execute(
        """SELECT al.seller_sku,al.asin,apl.product_id
             FROM amazon_listings al JOIN amazon_product_links apl
               ON apl.amazon_listing_id=al.amazon_listing_id
            WHERE apl.product_id IS NOT NULL
              AND lower(COALESCE(apl.match_status,'')) IN ('linked','matched','manual')"""
    ):
        for kind in ("seller_sku", "asin"):
            value = _text(row[kind])
            if value:
                refs[("amazon", "sku" if kind == "seller_sku" else "external_id", value.casefold())].append((int(row["product_id"]), "amazon_product_links"))
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='walmart_product_links'").fetchone():
        for row in connection.execute(
            """SELECT wl.seller_sku,wpl.product_id FROM walmart_listings wl
                 JOIN walmart_product_links wpl ON wpl.walmart_listing_id=wl.walmart_listing_id
                WHERE wpl.product_id IS NOT NULL AND lower(COALESCE(wpl.match_status,''))='linked'"""
        ):
            refs[("walmart", "sku", _text(row["seller_sku"]).casefold())].append((int(row["product_id"]), "walmart_product_links"))
    return refs


def _listing_identifier_candidates(connection: sqlite3.Connection, barcodes: dict[str, set[int]]) -> dict[tuple[str, str, str], list[tuple[int, str]]]:
    refs: dict[tuple[str, str, str], list[tuple[int, str]]] = defaultdict(list)
    for row in connection.execute(
        """SELECT lower(sc.channel_name) channel,cl.external_product_id,
                  cl.external_variant_id,cl.sku,cl.barcode_exact,cl.barcode_lookup
             FROM channel_listings cl JOIN sales_channels sc ON sc.channel_id=cl.channel_id"""
    ):
        product_ids = set()
        for raw in (row["barcode_exact"], row["barcode_lookup"]):
            product_ids.update(barcodes.get(_barcode(raw), set()))
        if len(product_ids) != 1:
            continue
        product_id = next(iter(product_ids))
        for kind, value in (("external_product_id", row["external_product_id"]), ("external_variant_id", row["external_variant_id"]), ("sku", row["sku"])):
            if _text(value):
                refs[(_text(row["channel"]).casefold(), kind, _text(value).casefold())].append((product_id, "channel_listings→product_barcodes"))
    return refs


def _master_catalog_evidence(connection: sqlite3.Connection, identifiers: set[str]) -> dict[str, list[str]]:
    values = sorted(value for value in identifiers if value)
    if not values:
        return {}
    placeholders = ",".join("?" for _ in values)
    evidence: dict[str, list[str]] = defaultdict(list)
    for row in connection.execute(
        f"""SELECT barcode_lookup,barcode_exact,description FROM master_catalog
              WHERE ltrim(COALESCE(barcode_lookup,''),'0') IN ({placeholders})
                 OR ltrim(COALESCE(barcode_exact,''),'0') IN ({placeholders})""",
        (*values, *values),
    ):
        for raw in (row["barcode_lookup"], row["barcode_exact"]):
            code = _barcode(raw)
            if code:
                evidence[code].append(_text(row["description"]))
    return evidence


def _title_candidates(title: str, products: dict[int, str], limit: int = 3) -> list[tuple[int, float]]:
    target = _normalized_title(title)
    if not target:
        return []
    target_tokens = set(target.split())
    scored = []
    for product_id, product_name in products.items():
        candidate = _normalized_title(product_name)
        candidate_tokens = set(candidate.split())
        if not candidate:
            continue
        sequence = SequenceMatcher(None, target, candidate).ratio()
        union = target_tokens | candidate_tokens
        jaccard = len(target_tokens & candidate_tokens) / len(union) if union else 0
        score = 0.65 * sequence + 0.35 * jaccard
        scored.append((product_id, score))
    return sorted(scored, key=lambda item: (-item[1], item[0]))[:limit]


def _project_category(connection: sqlite3.Connection, line: dict, product_id: int) -> str:
    lifecycle, _ = _lifecycle({**line, "quantity": line["units"]})
    quantity = max(int(line["units"] or 0), 0)
    if lifecycle != "sale" or quantity <= 0:
        return "review_lifecycle"
    eligible, replenishment, staged = _inventory_by_location(connection, product_id)
    chosen = next((item for item in eligible if item["available"] >= quantity), None)
    if chosen:
        return "immediately_fulfillable_storefront" if chosen["priority"] == 10 else "immediately_fulfillable_back_room"
    if sum(item["available"] for item in replenishment) >= quantity:
        return "reserved_owed_replenishment_available"
    if sum(item["on_hand"] for item in staged) >= quantity:
        return "reserved_owed_staged_pool_review"
    return "reserved_owed_unavailable_companywide"


def analyze_unmatched(connection: sqlite3.Connection, cutoff: str) -> tuple[list[MappingProposal], dict]:
    lines = _unmatched_lines(connection, cutoff)
    products, barcodes = _product_catalog(connection)
    cross_refs = _cross_references(connection)
    listing_refs = _listing_identifier_candidates(connection, barcodes)
    all_codes = {_barcode(value) for row in lines for value in (row.get("barcode"), row.get("sku")) if _barcode(value)}
    master_evidence = _master_catalog_evidence(connection, all_codes)
    groups: dict[str, list[dict]] = defaultdict(list)
    for line in lines:
        groups[_source_key(line["channel"], line)].append(line)

    proposals = []
    approved_line_candidates: dict[tuple[str, str, str], int] = {}
    for source_key, grouped_lines in groups.items():
        sample = grouped_lines[0]
        channel = sample["channel"]
        title = _text(sample["title"])
        identifiers = {
            "sku": _text(sample.get("sku")), "barcode": _barcode(sample.get("barcode")),
            "external_id": _text(sample.get("external_id")),
            "external_product_id": _text(sample.get("external_product_id")),
            "external_variant_id": _text(sample.get("external_variant_id")),
        }
        exact_ids = set()
        evidence_by_product: dict[int, list[str]] = defaultdict(list)
        for raw_label in ("barcode", "sku"):
            code = _barcode(identifiers[raw_label])
            matched = barcodes.get(code, set())
            if len(matched) == 1:
                product_id = next(iter(matched))
                exact_ids.add(product_id)
                evidence_by_product[product_id].append(f"exact {raw_label} {code} in product_barcodes")
                if master_evidence.get(code):
                    evidence_by_product[product_id].append(f"master_catalog exact barcode: {master_evidence[code][0]}")
        cross_ids = set()
        keys = [(channel, "source_key", source_key.casefold())]
        keys.extend((channel, kind, value.casefold()) for kind, value in identifiers.items() if value)
        for key in keys:
            for product_id, source in cross_refs.get(key, []) + listing_refs.get(key, []):
                if product_id not in products:
                    continue
                cross_ids.add(product_id)
                evidence_by_product[product_id].append(f"exact {key[1]} cross-reference in {source}")

        rank = 4
        selected = None
        title_options = _title_candidates(f"{title} {_text(sample.get('variant_title'))}", products)
        if len(exact_ids) == 1:
            selected = next(iter(exact_ids))
            rank = 1
        elif len(cross_ids) == 1:
            selected = next(iter(cross_ids))
            rank = 2
        elif title_options:
            best_id, best_score = title_options[0]
            second_score = title_options[1][1] if len(title_options) > 1 else 0
            allowed = (not exact_ids or best_id in exact_ids) and (not cross_ids or best_id in cross_ids)
            if best_score >= 0.88 and best_score - second_score >= 0.08 and allowed:
                selected = best_id
                rank = 3
                evidence_by_product[selected].append(f"unique normalized title similarity {best_score:.3f}; margin {best_score-second_score:.3f}")
        alternatives = []
        candidate_pool = sorted(exact_ids | cross_ids | {item[0] for item in title_options})
        title_scores = dict(title_options)
        for product_id in candidate_pool[:8]:
            alternatives.append({"product_id": product_id, "product_name": products.get(product_id, ""), "title_score": round(title_scores.get(product_id, 0), 3), "evidence": evidence_by_product.get(product_id, [])})
        outcomes = Counter()
        if selected is not None:
            for line in grouped_lines:
                outcomes[_project_category(connection, line, selected)] += 1
                if rank <= 3:
                    approved_line_candidates[(channel, _text(line["order_id"]), _text(line["order_line_id"]))] = selected
        reason = evidence_by_product.get(selected, []) if selected is not None else []
        if rank == 4:
            if len(exact_ids) > 1 or len(cross_ids) > 1:
                reason.append("conflicting or ambiguous exact evidence; no safe unique candidate")
            else:
                reason.append("no exact cross-reference and title evidence below conservative threshold")
        proposals.append(MappingProposal(
            channel=channel, source_key=source_key, marketplace_title=title,
            marketplace_identifiers=json.dumps(identifiers, separators=(",", ":")),
            order_line_count=len(grouped_lines), unit_count=sum(max(int(line["units"] or 0), 0) for line in grouped_lines),
            order_count=len({_text(line["order_id"]) for line in grouped_lines}),
            candidate_product_id=selected, candidate_product_name=products.get(selected, "") if selected else "",
            evidence="; ".join(reason), confidence_rank=rank,
            confidence_level=RANK_LABELS[rank], candidate_alternatives=json.dumps(alternatives, separators=(",", ":")),
            projected_line_outcomes=json.dumps(dict(outcomes), separators=(",", ":")),
        ))

    proposals.sort(key=lambda item: (item.channel, item.confidence_rank, -item.order_line_count, item.source_key))
    current = reconcile(connection, cutoff)
    projected = Counter(row.fulfillment_category for row in current)
    current_by_key = {(row.channel, row.order_id, row.order_line_id): row for row in current}
    for key, product_id in approved_line_candidates.items():
        old = current_by_key.get(key)
        line = next((item for item in lines if (item["channel"], _text(item["order_id"]), _text(item["order_line_id"])) == key), None)
        if old and line:
            projected[old.fulfillment_category] -= 1
            projected[_project_category(connection, line, product_id)] += 1
    summary = {
        "unmatched_lines": len(lines), "group_count": len(proposals),
        "by_channel": dict(Counter(line["channel"] for line in lines)),
        "groups_by_confidence": dict(Counter(item.confidence_level for item in proposals)),
        "resolvable_lines_if_rank_1_to_3_approved": len(approved_line_candidates),
        "resolvable_units_if_rank_1_to_3_approved": sum(int(line["units"] or 0) for line in lines if (line["channel"], _text(line["order_id"]), _text(line["order_line_id"])) in approved_line_candidates),
        "projected_reconciliation_categories": {key: value for key, value in projected.items() if value},
    }
    return proposals, summary
