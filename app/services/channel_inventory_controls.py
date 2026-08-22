"""Read controls and configure them only on explicit database copies."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from app.services.channel_inventory_engine import assert_copy_database


def effective_control(connection: sqlite3.Connection, channel: str) -> dict:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_inventory_engine_control'"
    ).fetchone()
    if not exists:
        return {"mode": "disabled", "paused": True, "reason": "control schema not installed"}
    rows = {str(row[0]): row for row in connection.execute(
        "SELECT scope,mode,paused,cutover_at,source_checkpoint,reason FROM channel_inventory_engine_control WHERE scope IN ('global',?)",
        (channel.casefold(),))}
    global_row, channel_row = rows.get("global"), rows.get(channel.casefold())
    if not global_row or not channel_row:
        return {"mode": "disabled", "paused": True, "reason": "missing global or channel control"}
    rank = {"disabled": 0, "dry_run": 1, "enabled": 2}
    selected = global_row if rank[str(global_row[1])] <= rank[str(channel_row[1])] else channel_row
    return {"mode": str(selected[1]), "paused": bool(global_row[2] or channel_row[2]),
            "cutover_at": channel_row[3], "source_checkpoint": channel_row[4],
            "reason": str(selected[5] or "")}


def set_copy_control(database: str | Path, scope: str, *, mode: str, paused: bool,
                     cutover_at: str | None, source_checkpoint: str | None,
                     reason: str, actor: str = "copy-simulation") -> None:
    path = assert_copy_database(database)
    if scope not in {"global", "shopify", "walmart", "amazon"} or mode not in {"disabled", "dry_run", "enabled"}:
        raise ValueError("Invalid control scope or mode")
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_inventory_engine_control'").fetchone() is None:
            raise RuntimeError("Control schema is not installed")
        connection.execute(
            """INSERT INTO channel_inventory_engine_control(scope,mode,paused,cutover_at,source_checkpoint,reason,updated_by,updated_at)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(scope) DO UPDATE SET mode=excluded.mode,paused=excluded.paused,
               cutover_at=excluded.cutover_at,source_checkpoint=excluded.source_checkpoint,reason=excluded.reason,
               updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
            (scope, mode, int(paused), cutover_at, source_checkpoint, reason, actor,
             datetime.now(timezone.utc).isoformat()))
        connection.commit()
