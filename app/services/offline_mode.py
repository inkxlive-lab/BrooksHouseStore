"""BrooksHouse offline snapshot, queue synchronization, and conflict review."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select, text

from app.database.connection import SessionLocal, engine
from app.database.models import (
    Inventory, InventoryLocation, InventoryTransaction, Product, ProductBarcode,
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _container(value) -> str:
    return str(value or "").strip().upper()


def _tables() -> None:
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS offline_sync_receipts (
                client_transaction_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL DEFAULT '', action_type TEXT NOT NULL,
                status TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL, applied_at TEXT NOT NULL
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS offline_sync_conflicts (
                conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_transaction_id TEXT NOT NULL UNIQUE,
                device_id TEXT NOT NULL DEFAULT '', action_type TEXT NOT NULL,
                payload_json TEXT NOT NULL, reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
                resolved_at TEXT, resolution_notes TEXT NOT NULL DEFAULT ''
            )
        """))


def _receipt(database, transaction_id: str):
    return database.execute(text("""
        SELECT status, result_json FROM offline_sync_receipts
        WHERE client_transaction_id = :transaction_id
    """), {"transaction_id": transaction_id}).mappings().first()


def _save_receipt(database, transaction_id, device_id, action_type, status, result):
    database.execute(text("""
        INSERT INTO offline_sync_receipts
        (client_transaction_id, device_id, action_type, status, result_json, created_at, applied_at)
        VALUES (:id, :device, :action, :status, :result, :created, :applied)
    """), {"id": transaction_id, "device": device_id, "action": action_type,
           "status": status, "result": json.dumps(result), "created": _now(), "applied": _now()})


def _conflict(database, transaction_id, device_id, action_type, payload, reason):
    database.execute(text("""
        INSERT OR IGNORE INTO offline_sync_conflicts
        (client_transaction_id, device_id, action_type, payload_json, reason, status, created_at)
        VALUES (:id, :device, :action, :payload, :reason, 'pending', :created)
    """), {"id": transaction_id, "device": device_id, "action": action_type,
           "payload": json.dumps(payload), "reason": reason, "created": _now()})
    result = {"client_transaction_id": transaction_id, "status": "conflict", "message": reason}
    _save_receipt(database, transaction_id, device_id, action_type, "conflict", result)
    return result


def _current_tote(database, location_id: int, container_id: str) -> dict[int, int]:
    rows = database.scalars(select(Inventory).where(Inventory.location_id == location_id)).all()
    return {int(row.product_id): int(row.quantity_on_hand or 0)
            for row in rows if _container(row.container_id) == container_id}


def _apply_batch(database, transaction_id: str, payload: dict) -> dict:
    location_id = int(payload.get("location_id") or 0)
    location = database.get(InventoryLocation, location_id)
    if not location or not location.active:
        raise ValueError("The selected inventory location is unavailable.")
    container_id = _container(payload.get("container_id"))
    if location_id == 2 and not container_id:
        raise ValueError("Back Stock requires a Tote / Container ID.")
    items = payload.get("items") or []
    if not items:
        raise ValueError("The offline batch has no items.")
    combined: dict[int, int] = {}
    for item in items:
        product_id, quantity = int(item.get("product_id") or 0), int(item.get("quantity") or 0)
        if product_id > 0 and quantity > 0:
            combined[product_id] = combined.get(product_id, 0) + quantity
    changed = 0
    for product_id, quantity in combined.items():
        product = database.get(Product, product_id)
        if not product:
            raise ValueError(f"Product {product_id} no longer exists.")
        record = database.scalar(select(Inventory).where(
            Inventory.product_id == product_id, Inventory.location_id == location_id,
            Inventory.container_id == container_id))
        if record is None:
            record = Inventory(product=product, location=location, container_id=container_id,
                               quantity_on_hand=0, quantity_reserved=0, reorder_level=0)
            database.add(record)
        record.quantity_on_hand = int(record.quantity_on_hand or 0) + quantity
        database.add(InventoryTransaction(
            product=product, location=location, container_id=container_id,
            transaction_type="offline_batch_add", quantity_change=quantity,
            unit_cost=product.average_cost, reference_number=f"OFFLINE-{transaction_id[:24]}",
            notes="Queued offline batch adjustment. Reason: " + str(payload.get("reason") or "other")))
        changed += 1
    return {"client_transaction_id": transaction_id, "status": "synced",
            "message": f"{changed} batch line(s) added.", "changed_count": changed}


def _apply_audit(database, transaction_id: str, payload: dict) -> dict:
    location_id = int(payload.get("location_id") or 0)
    container_id = _container(payload.get("container_id"))
    location = database.get(InventoryLocation, location_id)
    if not location or not container_id:
        raise ValueError("The audit location or tote is unavailable.")
    baseline = {int(x.get("product_id")): int(x.get("quantity") or 0)
                for x in (payload.get("baseline_items") or []) if int(x.get("product_id") or 0) > 0}
    current = _current_tote(database, location_id, container_id)
    if current != baseline:
        raise RuntimeError("This tote changed on the server after the offline audit began. Review is required.")
    counted = {int(x.get("product_id")): max(0, int(x.get("quantity") or 0))
               for x in (payload.get("counted_items") or []) if int(x.get("product_id") or 0) > 0}
    changed = 0
    for product_id in set(current) | set(counted):
        before, after = current.get(product_id, 0), counted.get(product_id, 0)
        if before == after:
            continue
        product = database.get(Product, product_id)
        if not product:
            raise ValueError(f"Product {product_id} no longer exists.")
        record = database.scalar(select(Inventory).where(
            Inventory.product_id == product_id, Inventory.location_id == location_id,
            Inventory.container_id == container_id))
        if record is None:
            record = Inventory(product=product, location=location, container_id=container_id,
                               quantity_on_hand=after, quantity_reserved=0, reorder_level=0)
            database.add(record)
        else:
            record.quantity_on_hand = after
        database.add(InventoryTransaction(
            product=product, location=location, container_id=container_id,
            transaction_type="offline_tote_audit", quantity_change=after - before,
            unit_cost=product.average_cost, reference_number=f"OFFLINE-{transaction_id[:24]}",
            notes=f"Queued offline tote audit. System {before}; physical {after}."))
        changed += 1
    return {"client_transaction_id": transaction_id, "status": "synced",
            "message": f"Tote audit synced; {changed} line(s) corrected.", "changed_count": changed}


def install_offline_mode(app, templates) -> None:
    _tables()

    # Serve the PWA manifest from FastAPI itself.  Keeping this endpoint here
    # avoids a deployment-dependent 404 when manifest.webmanifest is not copied
    # into (or mounted from) the application's static directory.
    @app.get("/manifest.webmanifest", include_in_schema=False)
    def web_manifest():
        manifest = {
            "id": "/dashboard",
            "name": "The BrooksHouse Store",
            "short_name": "BrooksHouse",
            "description": "BrooksHouse inventory, scanning, storage, and marketplace operations.",
            "start_url": "/dashboard?source=pwa",
            "scope": "/",
            "display": "standalone",
            "display_override": ["window-controls-overlay", "standalone", "minimal-ui"],
            "orientation": "any",
            "background_color": "#f4ede5",
            "theme_color": "#8f1f24",
            "categories": ["business", "productivity", "shopping"],
            "icons": [
                {"src": "/static/pwa/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": "/static/pwa/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            ],
            "shortcuts": [
                {"name": "Smart Scan", "short_name": "Scan", "url": "/smart-scan"},
                {"name": "Marketplace Orders", "short_name": "Orders", "url": "/channels/orders"},
                {"name": "Inventory Search", "short_name": "Inventory", "url": "/inventory/search"},
                {"name": "Offline Center", "short_name": "Offline", "url": "/offline"},
                {"name": "Notifications", "short_name": "Alerts", "url": "/tools/notifications"},
            ],
        }
        return Response(
            content=json.dumps(manifest, separators=(",", ":")),
            media_type="application/manifest+json",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/offline", response_class=HTMLResponse)
    def offline_home(request: Request):
        return templates.TemplateResponse(request=request, name="offline_mode.html", context={})

    @app.get("/offline/inventory-search", response_class=HTMLResponse)
    def offline_search(request: Request):
        return templates.TemplateResponse(request=request, name="offline_inventory_search.html", context={})

    @app.get("/admin/offline-sync", response_class=HTMLResponse)
    def offline_admin(request: Request):
        user = getattr(request.state, "auth_user", None)
        if user is not None and getattr(user, "role", "") != "owner_admin":
            raise HTTPException(status_code=403, detail="Owner/admin access is required.")
        return templates.TemplateResponse(request=request, name="admin_offline_sync.html", context={})

    @app.get("/api/offline/snapshot")
    def snapshot():
        with SessionLocal() as database:
            locations = database.scalars(
                select(InventoryLocation).order_by(InventoryLocation.location_name)
            ).all()
            # Inventory Search must include every product that owns an inventory
            # record. Inactive/catalog-disabled products can still physically
            # exist in a tote, pick slot, trailer, or other location.
            products = database.scalars(select(Product)).all()
            inventory = database.scalars(select(Inventory)).all()
            barcodes = database.scalars(select(ProductBarcode)).all()
            barcode_map: dict[int, list[str]] = {}
            for row in barcodes:
                barcode_map.setdefault(int(row.product_id), []).append(str(row.barcode))
            product_map = {int(p.product_id): p for p in products}
            location_map = {int(l.location_id): l.location_name for l in locations}
            rows = []
            for row in inventory:
                product = product_map.get(int(row.product_id))
                if not product:
                    continue
                rows.append({"inventory_id": row.inventory_id, "product_id": product.product_id,
                    "product_name": product.product_name, "description": product.description or "",
                    "brand": product.brand or "", "category": product.category or "",
                    "barcodes": barcode_map.get(int(product.product_id), []),
                    "location_id": row.location_id, "location_name": location_map.get(int(row.location_id), "Unknown"),
                    "container_id": row.container_id or "", "quantity": int(row.quantity_on_hand or 0),
                    "reserved": int(row.quantity_reserved or 0),
                    "store_price": str(product.store_price) if product.store_price is not None else ""})
            return {"version": 1, "generated_at": _now(),
                    "locations": [{"location_id": l.location_id, "location_name": l.location_name} for l in locations],
                    "rows": rows}

    @app.post("/api/offline/sync")
    async def sync(request: Request):
        body = await request.json()
        device_id = str(body.get("device_id") or "unknown")[:120]
        results = []
        with SessionLocal() as database:
            for queued in (body.get("transactions") or [])[:250]:
                transaction_id = str(queued.get("id") or "").strip()[:160]
                action = str(queued.get("action_type") or "").strip()
                payload = queued.get("payload") or {}
                if not transaction_id:
                    continue
                prior = _receipt(database, transaction_id)
                if prior:
                    old = json.loads(prior["result_json"] or "{}")
                    old["status"] = "duplicate" if prior["status"] == "synced" else prior["status"]
                    results.append(old)
                    continue
                try:
                    if action == "batch_adjustment":
                        result = _apply_batch(database, transaction_id, payload)
                    elif action == "tote_audit":
                        result = _apply_audit(database, transaction_id, payload)
                    else:
                        raise ValueError("Unsupported offline action.")
                    _save_receipt(database, transaction_id, device_id, action, "synced", result)
                    database.commit()
                    results.append(result)
                except RuntimeError as error:
                    database.rollback()
                    result = _conflict(database, transaction_id, device_id, action, payload, str(error))
                    database.commit()
                    results.append(result)
                except Exception as error:
                    database.rollback()
                    results.append(_conflict(database, transaction_id, device_id, action, payload, str(error)))
                    database.commit()
        return {"success": True, "results": results}

    @app.get("/api/offline/admin-summary")
    def admin_summary(request: Request):
        user = getattr(request.state, "auth_user", None)
        if user is not None and getattr(user, "role", "") != "owner_admin":
            raise HTTPException(status_code=403, detail="Owner/admin access is required.")
        with engine.begin() as connection:
            pending = connection.execute(text("SELECT COUNT(*) FROM offline_sync_conflicts WHERE status='pending'")).scalar() or 0
            synced = connection.execute(text("SELECT COUNT(*) FROM offline_sync_receipts WHERE status='synced'")).scalar() or 0
            conflicts = connection.execute(text("""
                SELECT conflict_id, client_transaction_id, device_id, action_type, reason, status, created_at
                FROM offline_sync_conflicts ORDER BY conflict_id DESC LIMIT 100
            """)).mappings().all()
        return {"pending_conflicts": pending, "synced_transactions": synced,
                "conflicts": [dict(row) for row in conflicts]}

    @app.post("/api/offline/conflicts/{conflict_id}/reviewed")
    async def resolve(conflict_id: int, request: Request):
        user = getattr(request.state, "auth_user", None)
        if user is not None and getattr(user, "role", "") != "owner_admin":
            raise HTTPException(status_code=403, detail="Owner/admin access is required.")
        body = await request.json()
        with engine.begin() as connection:
            connection.execute(text("""
                UPDATE offline_sync_conflicts SET status='reviewed', resolved_at=:now,
                resolution_notes=:notes WHERE conflict_id=:id
            """), {"now": _now(), "notes": str(body.get("notes") or "")[:500], "id": conflict_id})
        return {"success": True}
