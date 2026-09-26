from decimal import Decimal

from app.services.smart_scan_engine import (
    IDENTIFIED_EXTERNAL,
    MATCHED_CATALOG,
    PARTIAL_IDENTIFICATION,
    UNKNOWN,
    build_scan_result,
    calculate_deal_economics,
    resolve_acquisition_cost,
    variants_match,
)


def test_known_catalog_upc_preserves_product_id():
    result = build_scan_result("012345678905", catalog={"product_id": 42, "product_name": "Known", "match_method": "exact"})
    assert result["status"] == MATCHED_CATALOG
    assert result["catalog_product_id"] == 42


def test_unknown_upc_does_not_become_inventory():
    result = build_scan_result("012345678905")
    assert result["status"] == UNKNOWN
    assert result["catalog_product_id"] is None


def test_external_identification_is_distinct_from_catalog():
    result = build_scan_result("012345678905", external={"title": "External item", "brand": "Example"})
    assert result["status"] == IDENTIFIED_EXTERNAL
    assert result["catalog_match"] is False


def test_partial_ocr_preserves_evidence():
    result = build_scan_result("", ocr_text="MODEL ZX-42 RETURN", identifiers={"model_number": "ZX-42"})
    assert result["status"] == PARTIAL_IDENTIFICATION
    assert result["ocr_text"] == "MODEL ZX-42 RETURN"
    assert "ocr" in result["identification_sources"]


def test_completely_unknown_item_has_no_fake_product():
    result = build_scan_result("")
    assert result["status"] == UNKNOWN
    assert result["product_name"] is None


def test_bin_price_is_reused_as_acquisition_cost():
    assert resolve_acquisition_cost("bin", "8", "99") == Decimal("8")
    economics = calculate_deal_economics(mode="bin", bin_price="8", candidate_resale_price="20", quantity=3)
    assert economics["acquisition_cost"] == "8"
    assert economics["potential_total_profit"] == "36"


def test_size_and_count_conflicts_are_not_silent_matches():
    assert variants_match({"size": "12 oz", "quantity_or_pack_count": "2"}, {"size": "12 oz", "quantity_or_pack_count": "2"})
    assert not variants_match({"size": "12 oz"}, {"size": "24 oz"})
    assert not variants_match({"quantity_or_pack_count": "2"}, {"quantity_or_pack_count": "4"})
