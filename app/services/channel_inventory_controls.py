"""Read controls and configure them only on explicit database copies."""

from __future__ import annotations

import sqlite3
import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from app.services.channel_inventory_engine import assert_copy_database


def effective_control(connection: sqlite3.Connection, channel: str) -> dict:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_inventory_engine_control'"
    ).fetchone()
    if not exists:
        return {"mode": "disabled", "paused": True, "reason": "control schema not installed",
                "allocation_policy": "single_location_only", "eligible_locations": []}
    rows = {str(row[0]): row for row in connection.execute(
        "SELECT scope,mode,paused,cutover_at,source_checkpoint,reason,allocation_policy,eligible_locations_json FROM channel_inventory_engine_control WHERE scope IN ('global',?)",
        (channel.casefold(),))}
    global_row, channel_row = rows.get("global"), rows.get(channel.casefold())
    if not global_row or not channel_row:
        return {"mode": "disabled", "paused": True, "reason": "missing global or channel control",
                "allocation_policy": "single_location_only", "eligible_locations": []}
    rank = {"disabled": 0, "dry_run": 1, "enabled": 2}
    selected = global_row if rank[str(global_row[1])] <= rank[str(channel_row[1])] else channel_row
    return {"mode": str(selected[1]), "paused": bool(global_row[2] or channel_row[2]),
            "cutover_at": channel_row[3], "source_checkpoint": channel_row[4],
            "reason": str(selected[5] or ""), "allocation_policy": str(channel_row[6]),
            "eligible_locations": json.loads(str(channel_row[7]))}


def set_copy_control(database: str | Path, scope: str, *, mode: str, paused: bool,
                     cutover_at: str | None, source_checkpoint: str | None,
                     reason: str, actor: str = "copy-simulation",
                     allocation_policy: str = "single_location_only",
                     eligible_locations: tuple[str, ...] = ("BrooksHouse Storefront", "Store Back Room")) -> None:
    path = assert_copy_database(database)
    if scope not in {"global", "shopify", "walmart", "amazon"} or mode not in {"disabled", "dry_run", "enabled"}:
        raise ValueError("Invalid control scope or mode")
    if allocation_policy not in {"single_location_only", "ordered_multi_location"} or not eligible_locations:
        raise ValueError("Invalid allocation policy")
    if any(location not in {"BrooksHouse Storefront", "Store Back Room"} for location in eligible_locations):
        raise ValueError("Control includes an unapproved marketplace fulfillment location")
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_inventory_engine_control'").fetchone() is None:
            raise RuntimeError("Control schema is not installed")
        connection.execute(
            """INSERT INTO channel_inventory_engine_control(scope,mode,paused,cutover_at,source_checkpoint,reason,updated_by,updated_at,allocation_policy,eligible_locations_json)
               VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope) DO UPDATE SET mode=excluded.mode,paused=excluded.paused,
               cutover_at=excluded.cutover_at,source_checkpoint=excluded.source_checkpoint,reason=excluded.reason,
               updated_by=excluded.updated_by,updated_at=excluded.updated_at,allocation_policy=excluded.allocation_policy,
               eligible_locations_json=excluded.eligible_locations_json""",
            (scope, mode, int(paused), cutover_at, source_checkpoint, reason, actor,
             datetime.now(timezone.utc).isoformat(), allocation_policy, json.dumps(eligible_locations)))
        connection.commit()
