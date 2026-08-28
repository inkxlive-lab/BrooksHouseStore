"""Central metadata registry for significant BrooksHouse user-facing screens.

Add new screens here when their GET route is introduced.  Parameterized
screens use ``open_route`` to point the directory button at a safe parent.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable


@dataclass(frozen=True)
class Screen:
    name: str
    route: str
    description: str
    category: str
    navigation: str
    access: str = "Staff"
    badges: tuple[str, ...] = ()
    template: str = ""
    open_route: str = ""
    review_note: str = ""


def _screen(name: str, route: str, description: str, category: str,
            navigation: str, *badges: str, access: str = "Staff",
            template: str = "", open_route: str = "",
            review_note: str = "") -> Screen:
    return Screen(name, route, description, category, navigation, access,
                  tuple(badges), template, open_route, review_note)


SCREEN_REGISTRY: tuple[Screen, ...] = (
    # Inventory & Receiving
    _screen("Smart Scan", "/smart-scan", "Identify a product, repair content, and start scan-driven workflows.", "Inventory & Receiving", "Dashboard → Smart Scan", "Dashboard", template="smart_scan.html"),
    _screen("General Scan / Check Item", "/scan", "Look up a barcode and review the resolved product and stock.", "Inventory & Receiving", "Inventory tools → Scan Item", "Sub-screen", template="scan.html"),
    _screen("Receive Inventory", "/inventory/receive", "Receive counted inventory into a confirmed location or container.", "Inventory & Receiving", "Dashboard → More → Receive", "More Menu", template="receive_inventory.html"),
    _screen("Transfer Inventory", "/inventory/transfer", "Move inventory between BrooksHouse locations and containers.", "Inventory & Receiving", "Dashboard → More → Transfer", "More Menu", template="transfer_inventory.html"),
    _screen("Batch Transfer", "/inventory/transfer/batch", "Scan and transfer multiple inventory items in one session.", "Inventory & Receiving", "Transfer Inventory → Batch Transfer", "Sub-screen", template="batch_transfer_inventory.html"),
    _screen("Adjust Inventory", "/inventory/adjust", "Make an explicit, auditable inventory correction.", "Inventory & Receiving", "Dashboard → More → Adjust", "More Menu", template="adjust_inventory.html"),
    _screen("Batch Inventory Scan", "/inventory/adjust/batch", "Scan multiple found or counted products before saving adjustments.", "Inventory & Receiving", "Adjust Inventory → Batch Scan", "Sub-screen", template="batch_adjust_inventory.html"),
    _screen("Inventory Search", "/inventory/search", "Search by product, barcode, location, tote, stock status, quantity, and value.", "Inventory & Receiving", "Dashboard → Inventory Search", "Dashboard", template="inventory_search.html"),
    _screen("Inventory Activity", "/inventory/activity", "Review completed inventory transactions without changing stock.", "Inventory & Receiving", "Dashboard → Inventory Activity", "Dashboard", template="inventory_activity.html"),
    _screen("Replenishment Queue", "/inventory/replenishment", "Find inventory that should move toward the storefront or needs attention.", "Inventory & Receiving", "Dashboard → More → Replenishment Queue", "More Menu", template="replenishment_report.html"),
    _screen("Products", "/products", "Browse and maintain BrooksHouse product records.", "Inventory & Receiving", "Dashboard → More → Products", "More Menu", template="products.html"),
    _screen("Add Product", "/products/add", "Create a new BrooksHouse product record.", "Inventory & Receiving", "Products → Add Product", "Sub-screen", access="Manager / Owner", template="add_product.html", open_route="/products"),
    _screen("Product Detail", "/products/{product_id}", "Review product content, images, pricing, barcodes, and inventory.", "Inventory & Receiving", "Products or Inventory Search → Product", "Sub-screen", template="product_detail.html", open_route="/products"),
    _screen("Packaging Barcode Mapper", "/barcode-mapper", "Map case, pack, and unit barcodes to products and quantities.", "Inventory & Receiving", "Dashboard → Inventory tools → Packaging Mapper", "Dashboard", template="barcode_mapper.html"),

    # Storage & Locations
    _screen("Tote Manager", "/inventory/tote-manager", "Review tote contents and move or correct tote inventory.", "Storage & Locations", "Dashboard → More → Tote Manager", "More Menu", template="tote_manager.html"),
    _screen("Tote Repair", "/inventory/tote-repair", "Repair inventory rows missing a valid tote or container assignment.", "Storage & Locations", "Dashboard → More → Tote Repair", "More Menu", template="tote_repair.html"),
    _screen("Rapid Tote Audit", "/inventory/tote-audit", "Lock onto a tote and verify its physical contents by scanning.", "Storage & Locations", "Dashboard → More → Tote Audit", "More Menu", template="tote_audit.html"),
    _screen("Container to Shelf", "/inventory/container-to-shelf", "Move confirmed container inventory into shelf locations.", "Storage & Locations", "Dashboard → More → Container → Shelf Transfer", "More Menu", template="container_to_shelf.html"),
    _screen("Storage Gallery", "/storage-gallery", "Browse visual storage areas and their inventory.", "Storage & Locations", "Dashboard → More → Storage Gallery", "More Menu", template="storage_gallery.html"),
    _screen("Storage Gallery Location", "/storage-gallery/{location_id}", "Inspect one storage location, general area, or slot.", "Storage & Locations", "Storage Gallery → Location", "Sub-screen", template="storage_gallery.html", open_route="/storage-gallery"),
    _screen("Storage Gallery General Area", "/storage-gallery/{location_id}/general", "Inspect loose or general inventory within one storage location.", "Storage & Locations", "Storage Gallery → Location → General", "Sub-screen", template="storage_gallery.html", open_route="/storage-gallery"),
    _screen("Storage Gallery Slot", "/storage-gallery/{location_id}/slot/{slot_code}", "Inspect one specific storage slot and its inventory.", "Storage & Locations", "Storage Gallery → Location → Slot", "Sub-screen", template="storage_gallery.html", open_route="/storage-gallery"),
    _screen("Store Map", "/store-map", "See shared store and inventory locations on a visual map.", "Storage & Locations", "Dashboard → More → Store Map", "More Menu", template="store_map.html"),
    _screen("Location Master", "/admin/location-master", "Manage canonical BrooksHouse locations and placement metadata.", "Storage & Locations", "Dashboard → More → Location Master", "More Menu", access="Manager / Owner", template="location_master.html"),
    _screen("Location Builder", "/admin/location-builder", "Build location structures and saved label batches.", "Storage & Locations", "Location Master → Bulk Builder", "Sub-screen", access="Manager / Owner", template="location_bulk_builder.html"),
    _screen("Location Label Batch", "/admin/location-builder/batches/{batch_id}/edit", "Control and print a saved location-label batch.", "Storage & Locations", "Location Builder → Control / Print", "Sub-screen", access="Manager / Owner", template="location_label_batch_editor.html", open_route="/admin/location-builder"),
    _screen("Location Label Print", "/admin/location-builder/batches/{batch_id}/print", "Render a controlled printable location-label batch.", "Storage & Locations", "Location Label Batch → Print", "Sub-screen", access="Manager / Owner", template="pallet_labels.html", open_route="/admin/location-builder"),

    # Orders & Fulfillment
    _screen("Marketplace Orders", "/channels/orders", "Review actionable Walmart and Amazon orders together.", "Orders & Fulfillment", "Dashboard → Marketplace Orders", "Dashboard", template="marketplace_orders.html"),
    _screen("Marketplace Order History", "/channels/orders/history", "Search terminal and historical marketplace orders.", "Orders & Fulfillment", "Marketplace Orders → Order History", "Sub-screen", template="marketplace_orders.html"),
    _screen("Combined Pull List", "/channels/orders/pull-list", "Consolidate current cross-channel picking requirements.", "Orders & Fulfillment", "Dashboard → Pull List", "Dashboard", template="marketplace_pull_guide.html"),
    _screen("Walmart Orders", "/channels/walmart/orders", "Review saved Walmart orders and fulfillment state.", "Orders & Fulfillment", "Dashboard → Marketplace tools → Walmart Orders", "Dashboard", template="walmart_orders.html"),
    _screen("Walmart Pull List", "/channels/walmart/orders/pull-list", "Prepare a Walmart-only product pull guide.", "Orders & Fulfillment", "Walmart Orders → Pull List", "Sub-screen", template="walmart_pull_guide.html"),
    _screen("Walmart Order Detail", "/channels/walmart/orders/{purchase_order_id}", "Map, pick, stage, and review one Walmart order.", "Orders & Fulfillment", "Walmart Orders → Order", "Sub-screen", template="walmart_order_pull.html", open_route="/channels/walmart/orders"),
    _screen("Amazon Orders", "/channels/amazon/orders", "Review the Amazon order workspace and setup status.", "Orders & Fulfillment", "Dashboard → Marketplace tools → Amazon Orders", "Dashboard", template="amazon_orders_pending.html", review_note="Template still describes Amazon API access as pending although saved Amazon order history is active elsewhere; review for consolidation."),
    _screen("Amazon Pull List", "/channels/amazon/orders/pull-list", "Open the Amazon-only pull-list placeholder workspace.", "Orders & Fulfillment", "Combined Pull List → Amazon Pull List", "Sub-screen", template="amazon_orders_pending.html", review_note="Shares the Amazon pending/setup template rather than the newer combined fulfillment implementation."),
    _screen("Operations Work Queue", "/operations/work-queue", "Assign, complete, block, and review operational work.", "Orders & Fulfillment", "Dashboard → More → Work Queue", "More Menu", template="operations_work_queue.html"),
    _screen("Operations Reports", "/operations/reports", "Create immutable marketplace order and pull-list report snapshots.", "Orders & Fulfillment", "Dashboard → Operations Reports", "Dashboard", access="Manager / Owner", template="operations_reports.html"),
    _screen("Operations Report Job", "/operations/reports/jobs/{job_id}", "Monitor one report-generation job.", "Orders & Fulfillment", "Operations Reports → Recent jobs", "Sub-screen", access="Manager / Owner", template="operations_report_job.html", open_route="/operations/reports"),
    _screen("Operations Report Snapshot", "/operations/reports/{report_run_id}", "Review and export an immutable operations snapshot.", "Orders & Fulfillment", "Operations Reports → Report history", "Sub-screen", access="Manager / Owner", template="operations_report_snapshot.html", open_route="/operations/reports"),

    # Marketplace & Channels
    _screen("Marketplace Publish Center", "/channels/publish", "Evaluate Walmart and Amazon readiness, opportunities, and draft listings.", "Marketplace & Channels", "Product Detail or Content Lab → Publish to Marketplaces", "Sub-screen", access="Owner admin", template="marketplace_publish.html"),
    _screen("Channel Performance / Content Lab", "/reports/channel-performance", "Compare product performance, content, and channel readiness.", "Marketplace & Channels", "Dashboard → More → Channel Performance", "More Menu", template="channel_performance.html"),
    _screen("Channel Product Compare", "/reports/channel-performance/product/{product_id}", "Compare one product across BrooksHouse and marketplace data.", "Marketplace & Channels", "Channel Performance → Product", "Sub-screen", template="channel_product_compare.html", open_route="/reports/channel-performance"),
    _screen("Amazon Order Detail", "/reports/channel-performance/amazon-order/{amazon_order_id}", "Review saved Amazon order details from Content Lab.", "Marketplace & Channels", "Channel Performance → Amazon order", "Sub-screen", template="amazon_order_detail.html", open_route="/reports/channel-performance"),
    _screen("Marketplace Stats", "/channels/stats", "Review marketplace listing and channel summary statistics.", "Marketplace & Channels", "Dashboard → More → Marketplace Stats", "More Menu", template="marketplace_stats.html"),
    _screen("Product Matching", "/reports/product-matching", "Resolve unmatched marketplace sales lines to BrooksHouse products.", "Marketplace & Channels", "Dashboard → More → Product Matching", "More Menu", access="Manager / Owner", template="product_matching_queue.html"),
    _screen("Amazon Mapping", "/channels/amazon/mapping", "Link Amazon listings and ASIN/SKU data to BrooksHouse products.", "Marketplace & Channels", "Dashboard → More → Amazon Mapping", "More Menu", access="Owner admin", template="amazon_mapping.html"),
    _screen("Shopify Compare", "/channels/shopify/reconcile", "Compare Shopify inventory with BrooksHouse before approval.", "Marketplace & Channels", "Dashboard → More → Shopify Compare", "More Menu", access="Owner admin", template="shopify_reconciliation.html"),
    _screen("Shopify Settings", "/channels/shopify/settings", "Configure Shopify inventory integration behavior.", "Marketplace & Channels", "Shopify Compare → Settings", "Sub-screen", access="Owner admin", template="shopify_inventory_settings.html"),
    _screen("Shopify Push Preview", "/channels/shopify/push-preview", "Preview approved Shopify changes before any push.", "Marketplace & Channels", "Direct route only", "Unlinked", access="Owner admin", template="shopify_push_preview.html"),
    _screen("Shopify Approval Queue", "/channels/shopify/approve", "Approve selected Shopify inventory changes.", "Marketplace & Channels", "Dashboard → More → Approval Queue", "More Menu", access="Owner admin", template="shopify_approval_queue.html"),
    _screen("Shopify Storefront Import", "/channels/shopify/storefront-import", "Preview products available from the active Shopify storefront.", "Marketplace & Channels", "Products / inventory tools → Storefront Import", "Sub-screen", access="Owner admin", template="shopify_storefront_import.html"),
    _screen("Shopify Sales History", "/channels/shopify/sales", "Review Shopify checkout and payment history saved locally.", "Marketplace & Channels", "Dashboard → More → Shopify Sales History", "More Menu", access="Owner admin", template="shopify_sales_history.html"),
    _screen("Shopify Sale Detail", "/channels/shopify/sales/details", "Inspect one saved Shopify sale and its line details.", "Marketplace & Channels", "Shopify Sales History → Sale", "Sub-screen", access="Owner admin", template="shopify_sale_details.html", open_route="/channels/shopify/sales"),

    # Product Content & AI
    _screen("AI Image Studio", "/images/studio", "Generate, review, approve, and safely store product images.", "Product Content & AI", "Dashboard → More → AI Image Studio", "More Menu", access="Owner admin", template="image_studio.html"),
    _screen("Product Enrichment", "/admin/product-enrichment", "Create review-first product enrichment batches.", "Product Content & AI", "Dashboard → More → Product Enrichment", "More Menu", access="Owner admin", template="product_enrichment_batches.html"),
    _screen("Product Enrichment Batch", "/admin/product-enrichment/batches/{batch_id}", "Review progress and products in one enrichment batch.", "Product Content & AI", "Product Enrichment → Batch", "Sub-screen", access="Owner admin", template="product_enrichment_batch.html", open_route="/admin/product-enrichment"),
    _screen("Product Enrichment Review", "/admin/product-enrichment/items/{item_id}", "Review and explicitly apply suggestions for one product.", "Product Content & AI", "Product Enrichment Batch → Review", "Sub-screen", access="Owner admin", template="product_enrichment_review.html", open_route="/admin/product-enrichment"),
    _screen("Product Enrichment Audit", "/admin/product-enrichment/batches/{batch_id}/audit", "Inspect the append-only enrichment audit trail.", "Product Content & AI", "Product Enrichment Batch → Audit", "Sub-screen", access="Owner admin", template="product_enrichment_audit.html", open_route="/admin/product-enrichment"),

    # Labels
    _screen("Product Labels", "/product-labels", "Generate printable product and barcode labels.", "Labels", "Dashboard → More → Product Labels", "More Menu", template="product_labels.html"),
    _screen("Pallet & Location Labels", "/pallet-labels", "Generate pallet, rack, shelf, and location labels.", "Labels", "Dashboard → More → Pallet & Location Labels", "More Menu", template="pallet_labels.html"),

    # Kids / Directed Work
    _screen("Kids Work & Rewards", "/kids", "Run directed helper work, approvals, points, and rewards.", "Kids / Directed Work", "Dashboard → More → Kids Work & Rewards", "More Menu", access="Kids / Staff", template="kids_helper.html"),
    _screen("Store Helper Home", "/role-home", "Show role-appropriate assigned work and helper tools.", "Kids / Directed Work", "Sign in as Store Helper", "Sub-screen", access="Store helper", template="store_helper_home.html"),

    # Reports
    _screen("Sales Dashboard", "/sales", "Review saved channel sales, costs, and profit summaries.", "Reports", "Dashboard → More → Sales Dashboard", "More Menu", access="Manager / Owner", template="sales_dashboard.html"),
    _screen("Shopify Cost Rules", "/sales/shopify-cost-rules", "Manage estimated costs for Shopify quick-sale lines.", "Reports", "Sales Dashboard → Cost Rules", "Sub-screen", access="Manager / Owner", template="shopify_cost_rules.html"),

    # Admin & System
    _screen("Screen Directory", "/screen-directory", "Find every major BrooksHouse screen and identify navigation gaps.", "Admin & System", "Dashboard → More → Screen Directory", "More Menu", template="screen_directory.html"),
    _screen("System Check", "/admin/system-check", "Review database, marketplace, backup, inventory, and worker health.", "Admin & System", "Dashboard → More → System Check", "More Menu", access="Owner admin", template="admin_system_check.html"),
    _screen("Notifications", "/tools/notifications", "Manage browser push subscriptions and recap settings.", "Admin & System", "Dashboard → More → Notifications", "More Menu", access="Owner admin", template="web_push_notifications.html"),
    _screen("Install BrooksHouse App", "/install", "Install BrooksHouse as a device app and verify notifications.", "Admin & System", "Direct route only", "Unlinked", template="install_app.html"),
    _screen("Team Access", "/access", "Manage BrooksHouse users, roles, sessions, and helper links.", "Admin & System", "Dashboard → More → Team Access", "More Menu", access="Owner admin", template="access_admin.html"),
    _screen("My Profile", "/profile", "Manage the signed-in team member profile and picture.", "Admin & System", "Dashboard → Profile", "Dashboard", template="team_profile.html"),
    _screen("Offline Center", "/offline", "Prepare offline inventory tools and synchronize queued work.", "Admin & System", "Dashboard → Offline Center", "Dashboard", template="offline_mode.html"),
    _screen("Offline Inventory Search", "/offline/inventory-search", "Search the most recently downloaded inventory snapshot offline.", "Admin & System", "Offline Center → Inventory Search", "Sub-screen", template="offline_inventory_search.html"),
    _screen("Offline Sync Review", "/admin/offline-sync", "Review queued offline changes, duplicates, and conflicts.", "Admin & System", "System Check → Offline Sync", "Admin", access="Owner admin", template="admin_offline_sync.html"),
    _screen("Channel Inventory Preflight", "/admin/channel-inventory-engine", "Review the dormant copy-only channel inventory engine deployment state.", "Admin & System", "Dashboard → More → Channel Inventory Preflight", "More Menu", access="Owner admin", template="channel_inventory_engine_admin.html"),
    _screen("Channel Inventory Review", "/admin/channel-inventory-review", "Review channel mapping candidates without automatic inventory mutation.", "Admin & System", "Dashboard → More → Channel Inventory Review", "More Menu", access="Owner admin", template="channel_inventory_review.html"),
    _screen("SQL Console", "/tools/sql", "Run owner-only diagnostic SQL tools.", "Admin & System", "Dashboard → More → SQL Console", "Developer", access="Owner admin", template="sql_console.html", review_note="Developer diagnostic screen; keep owner-only."),
    _screen("Python Lab", "/tools/python", "Run owner-only Python diagnostic tools.", "Admin & System", "Dashboard → More → Python Lab", "Developer", access="Owner admin", template="python_lab.html", review_note="Developer diagnostic screen; keep owner-only."),
    _screen("Login", "/login", "Authenticate a BrooksHouse team member.", "Admin & System", "Signed-out entry point", "Sub-screen", access="Public", template="access_login.html"),
)


CATEGORY_ORDER = (
    "Inventory & Receiving", "Storage & Locations", "Orders & Fulfillment",
    "Marketplace & Channels", "Product Content & AI", "Labels",
    "Kids / Directed Work", "Reports", "Admin & System",
)


def validate_registry(route_paths: Iterable[str]) -> dict:
    """Compare registry routes with currently installed FastAPI route paths."""
    installed = {str(path) for path in route_paths}
    registered = {screen.route for screen in SCREEN_REGISTRY}
    return {
        "missing_routes": sorted(registered - installed),
        "registered_routes": sorted(registered & installed),
    }


def directory_context(route_paths: Iterable[str]) -> dict:
    validation = validate_registry(route_paths)
    installed = set(validation["registered_routes"])
    screens = []
    for screen in SCREEN_REGISTRY:
        row = asdict(screen)
        row["badges"] = list(screen.badges)
        row["route_exists"] = screen.route in installed
        row["open_route"] = screen.open_route or screen.route
        row["unlinked"] = "Unlinked" in screen.badges
        screens.append(row)
    counts = {
        "total": len(screens),
        "dashboard": sum("Dashboard" in row["badges"] or "More Menu" in row["badges"] for row in screens),
        "subscreens": sum("Sub-screen" in row["badges"] for row in screens),
        "admin_developer": sum(row["access"] == "Owner admin" or "Admin" in row["badges"] or "Developer" in row["badges"] for row in screens),
        "unlinked": sum(row["unlinked"] for row in screens),
        "missing_routes": len(validation["missing_routes"]),
    }
    return {"screens": screens, "counts": counts, "categories": CATEGORY_ORDER,
            "validation": validation}
