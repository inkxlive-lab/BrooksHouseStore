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


CLASSIFICATION_ORDER = {
    "A_EXACT": 1,
    "B_STRONG": 2,
    "C_CANDIDATE": 3,
    "E_AMBIGUOUS": 4,
    "D_NO_MATCH": 5,
}


@dataclass(frozen=True)
class MappingProposal:
    channel: str
    source_key: str
    marketplace_order_item_identifiers: str
    marketplace_sku: str
    marketplace_title: str
    marketplace_barcode: str
    marketplace_asin: str
    marketplace_listing_identifiers: str
    marketplace_identifiers: str
    order_line_count: int
    unit_count: int
    order_count: int
    candidate_product_id: int | None
    candidate_product_barcode: str
    candidate_product_name: str
    evidence: str
    match_classification: str
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


def _product_catalog(connection: sqlite3.Connection) -> tuple[dict[int, str], dict[str, set[int]], dict[int, list[str]]]:
    products = {int(row["product_id"]): _text(row["product_name"]) for row in connection.execute(
        "SELECT product_id,product_name FROM products WHERE COALESCE(active,1)=1"
    )}
    barcodes: dict[str, set[int]] = defaultdict(set)
    product_barcodes: dict[int, list[str]] = defaultdict(list)
    for row in connection.execute("SELECT product_id,barcode FROM product_barcodes"):
        normalized = _barcode(row["barcode"])
        if normalized and int(row["product_id"]) in products:
            barcodes[normalized].add(int(row["product_id"]))
            product_barcodes[int(row["product_id"])].append(_text(row["barcode"]))
    return products, barcodes, product_barcodes


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
    products, barcodes, product_barcodes = _product_catalog(connection)
    cross_refs = _cross_references(connection)
    listing_refs = _listing_identifier_candidates(connection, barcodes)
    all_codes = {_barcode(value) for row in lines for value in (row.get("barcode"), row.get("sku")) if _barcode(value)}
    master_evidence = _master_catalog_evidence(connection, all_codes)
    groups: dict[str, list[dict]] = defaultdict(list)
    for line in lines:
        groups[_source_key(line["channel"], line)].append(line)

    proposals = []
    scenario_candidates: dict[str, dict[tuple[str, str, str], int]] = {
        "exact_only": {}, "exact_and_strong": {},
    }
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
            for product_id in matched:
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

        classification = "D_NO_MATCH"
        selected = None
        title_options = _title_candidates(f"{title} {_text(sample.get('variant_title'))}", products)
        if len(exact_ids) == 1 and (not cross_ids or cross_ids == exact_ids):
            selected = next(iter(exact_ids))
            classification = "A_EXACT"
        elif len(exact_ids) > 1 or len(cross_ids) > 1 or (exact_ids and cross_ids and exact_ids != cross_ids):
            classification = "E_AMBIGUOUS"
        elif not exact_ids and len(cross_ids) == 1:
            selected = next(iter(cross_ids))
            classification = "B_STRONG"
        elif not exact_ids and not cross_ids and title_options:
            best_id, best_score = title_options[0]
            second_score = title_options[1][1] if len(title_options) > 1 else 0
            plausible = [item for item in title_options if item[1] >= 0.72]
            if plausible and (len(plausible) == 1 or best_score - second_score >= 0.08):
                selected = best_id
                classification = "C_CANDIDATE"
                evidence_by_product[selected].append(f"plausible normalized title similarity {best_score:.3f}; margin {best_score-second_score:.3f}; human approval required")
            elif len(plausible) > 1:
                classification = "E_AMBIGUOUS"
        alternatives = []
        candidate_pool = sorted(exact_ids | cross_ids | {item[0] for item in title_options})
        title_scores = dict(title_options)
        for product_id in candidate_pool[:8]:
            alternatives.append({"product_id": product_id, "product_name": products.get(product_id, ""), "title_score": round(title_scores.get(product_id, 0), 3), "evidence": evidence_by_product.get(product_id, [])})
        outcomes = Counter()
        if selected is not None:
            for line in grouped_lines:
                outcomes[_project_category(connection, line, selected)] += 1
                line_key = (channel, _text(line["order_id"]), _text(line["order_line_id"]))
                if classification == "A_EXACT":
                    scenario_candidates["exact_only"][line_key] = selected
                    scenario_candidates["exact_and_strong"][line_key] = selected
                elif classification == "B_STRONG":
                    scenario_candidates["exact_and_strong"][line_key] = selected
        reason = evidence_by_product.get(selected, []) if selected is not None else []
        if classification in {"D_NO_MATCH", "E_AMBIGUOUS"}:
            if classification == "E_AMBIGUOUS":
                reason.append(
                    "conflicting identifier evidence; no safe unique candidate"
                    if exact_ids or cross_ids
                    else "multiple plausible title candidates; human review cannot safely choose one"
                )
            else:
                reason.append("no exact cross-reference and no title candidate met the 0.72 review threshold")
        listing_ids = {
            "external_product_id": identifiers["external_product_id"],
            "external_variant_id": identifiers["external_variant_id"],
            "marketplace_item_id": identifiers["external_id"],
        }
        proposals.append(MappingProposal(
            channel=channel, source_key=source_key, marketplace_title=title,
            marketplace_order_item_identifiers=json.dumps([
                {"order_id": _text(line["order_id"]), "order_line_id": _text(line["order_line_id"])}
                for line in grouped_lines
            ], separators=(",", ":")),
            marketplace_sku=identifiers["sku"], marketplace_barcode=identifiers["barcode"],
            marketplace_asin=identifiers["external_id"] if channel == "amazon" else "",
            marketplace_listing_identifiers=json.dumps(listing_ids, separators=(",", ":")),
            marketplace_identifiers=json.dumps(identifiers, separators=(",", ":")),
            order_line_count=len(grouped_lines), unit_count=sum(max(int(line["units"] or 0), 0) for line in grouped_lines),
            order_count=len({_text(line["order_id"]) for line in grouped_lines}),
            candidate_product_id=selected,
            candidate_product_barcode=product_barcodes.get(selected, [""])[0] if selected else "",
            candidate_product_name=products.get(selected, "") if selected else "",
            evidence="; ".join(reason), match_classification=classification,
            candidate_alternatives=json.dumps(alternatives, separators=(",", ":")),
            projected_line_outcomes=json.dumps(dict(outcomes), separators=(",", ":")),
        ))

    proposals.sort(key=lambda item: (CLASSIFICATION_ORDER[item.match_classification], -item.order_line_count, -item.unit_count, item.channel, item.source_key))
    current = reconcile(connection, cutoff)
    current_by_key = {(row.channel, row.order_id, row.order_line_id): row for row in current}
    line_by_key = {(item["channel"], _text(item["order_id"]), _text(item["order_line_id"])): item for item in lines}
    projections = {}
    for scenario, candidates in scenario_candidates.items():
        projected = Counter(row.fulfillment_category for row in current)
        for key, product_id in candidates.items():
            old = current_by_key.get(key)
            line = line_by_key.get(key)
            if old and line:
                projected[old.fulfillment_category] -= 1
                projected[_project_category(connection, line, product_id)] += 1
        projections[scenario] = {key: value for key, value in projected.items() if value}
    group_counts = Counter(item.match_classification for item in proposals)
    line_counts = Counter()
    unit_counts = Counter()
    for item in proposals:
        line_counts[item.match_classification] += item.order_line_count
        unit_counts[item.match_classification] += item.unit_count
    summary = {
        "unmatched_lines": len(lines), "group_count": len(proposals),
        "by_channel": dict(Counter(line["channel"] for line in lines)),
        "groups_by_classification": dict(group_counts),
        "lines_by_classification": dict(line_counts),
        "units_by_classification": dict(unit_counts),
        "lines_matched_if_exact_approved": len(scenario_candidates["exact_only"]),
        "additional_lines_if_strong_approved": len(scenario_candidates["exact_and_strong"]) - len(scenario_candidates["exact_only"]),
        "remaining_manual_review_lines_after_exact_and_strong": len(lines) - len(scenario_candidates["exact_and_strong"]),
        "remaining_manual_review_groups_after_exact_and_strong": sum(group_counts[key] for key in ("C_CANDIDATE", "D_NO_MATCH", "E_AMBIGUOUS")),
        "projected_reconciliation_exact_only": projections["exact_only"],
        "projected_reconciliation_exact_and_strong": projections["exact_and_strong"],
    }
    return proposals, summary
