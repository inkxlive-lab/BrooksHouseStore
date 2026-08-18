#!/usr/bin/env python3
"""BrooksHouse free Railway backup agent.

Creates consistent SQLite snapshots and media archives on the Railway volume,
downloads them in small verified chunks over Railway SSH, validates them
locally, and applies rolling retention.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from datetime import datetime
from pathlib import Path


CHUNK_BYTES = 3 * 1024 * 1024
CHUNK_RETRIES = 4


class BackupError(RuntimeError):
    pass


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def log(message: str, log_file: Path) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} | {message}"
    print(line, flush=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as handle:
        config = json.load(handle)
    required = ["railway_command", "railway_workdir", "service", "backup_root"]
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise BackupError(f"Missing configuration values: {', '.join(missing)}")
    return config


def railway_ssh(config: dict, remote_args: list[str], timeout: int = 180) -> str:
    command = [config["railway_command"], "ssh", "--service", config["service"], *remote_args]
    result = subprocess.run(
        command,
        cwd=config["railway_workdir"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    stdout = result.stdout.decode("utf-8", errors="replace").strip()
    stderr = result.stderr.decode("utf-8", errors="replace").strip()
    if result.returncode != 0:
        raise BackupError(
            f"Railway SSH failed ({result.returncode}).\nSTDOUT: {stdout}\nSTDERR: {stderr}"
        )
    return stdout


def remote_file_info(config: dict, remote_path: str) -> dict:
    code = (
        "import hashlib,json,pathlib,sys;"
        "p=pathlib.Path(sys.argv[1]);"
        "h=hashlib.sha256();f=p.open('rb');"
        "[h.update(b) for b in iter(lambda:f.read(1048576),b'')];f.close();"
        "print(json.dumps({'size':p.stat().st_size,'sha256':h.hexdigest()}))"
    )
    output = railway_ssh(config, ["python", "-c", code, remote_path], timeout=600)
    try:
        return json.loads(output.splitlines()[-1])
    except Exception as exc:
        raise BackupError(f"Could not parse remote file information: {output}") from exc


def download_remote_file(config: dict, remote_path: str, local_path: Path, log_file: Path) -> dict:
    info = remote_file_info(config, remote_path)
    expected_size = int(info["size"])
    expected_hash = str(info["sha256"]).lower()
    partial = local_path.with_suffix(local_path.suffix + ".partial")
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.unlink(missing_ok=True)

    code = (
        "import base64,pathlib,sys;"
        "p=pathlib.Path(sys.argv[1]);o=int(sys.argv[2]);n=int(sys.argv[3]);"
        "f=p.open('rb');f.seek(o);b=f.read(n);f.close();"
        "print(base64.b64encode(b).decode('ascii'))"
    )

    downloaded = 0
    with partial.open("wb") as output:
        while downloaded < expected_size:
            request_size = min(CHUNK_BYTES, expected_size - downloaded)
            last_error = None
            for attempt in range(1, CHUNK_RETRIES + 1):
                try:
                    encoded = railway_ssh(
                        config,
                        ["python", "-c", code, remote_path, str(downloaded), str(request_size)],
                        timeout=180,
                    )
                    chunk = base64.b64decode(encoded, validate=True)
                    if len(chunk) != request_size:
                        raise BackupError(
                            f"Chunk at {downloaded} returned {len(chunk)} bytes; expected {request_size}."
                        )
                    output.write(chunk)
                    output.flush()
                    downloaded += len(chunk)
                    percent = downloaded * 100 / expected_size if expected_size else 100
                    log(f"Downloaded {downloaded}/{expected_size} bytes ({percent:.1f}%)", log_file)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    log(f"Chunk retry {attempt}/{CHUNK_RETRIES} at byte {downloaded}: {exc}", log_file)
                    time.sleep(min(attempt * 3, 10))
            if last_error is not None:
                raise BackupError(f"Could not download chunk at byte {downloaded}: {last_error}")

    actual_hash = sha256_file(partial)
    actual_size = partial.stat().st_size
    if actual_size != expected_size or actual_hash.lower() != expected_hash:
        raise BackupError(
            f"Downloaded file verification failed. Size {actual_size}/{expected_size}; "
            f"SHA256 {actual_hash}/{expected_hash}."
        )
    os.replace(partial, local_path)
    return {"size": actual_size, "sha256": actual_hash}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_sqlite(path: Path) -> dict:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise BackupError(f"SQLite quick_check failed: {quick_check}")
        inventory_rows = connection.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
        inventory_units = connection.execute(
            "SELECT COALESCE(SUM(quantity_on_hand),0) FROM inventory"
        ).fetchone()[0]
        transactions = connection.execute("SELECT COUNT(*) FROM inventory_transactions").fetchone()[0]
        latest = connection.execute("SELECT MAX(created_at) FROM inventory_transactions").fetchone()[0]
        return {
            "quick_check": quick_check,
            "inventory_rows": inventory_rows,
            "inventory_units": inventory_units,
            "transactions": transactions,
            "latest_transaction": latest,
        }
    finally:
        connection.close()


def validate_tar(path: Path) -> dict:
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        files = [member for member in members if member.isfile()]
        if not files:
            raise BackupError("Media archive contains no files.")
        unsafe = [m.name for m in members if m.name.startswith("/") or ".." in Path(m.name).parts]
        if unsafe:
            raise BackupError(f"Media archive contains unsafe paths: {unsafe[:3]}")
        return {"members": len(members), "files": len(files)}


def prune_local(directory: Path, pattern: str, keep: int, log_file: Path) -> None:
    files = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        old.unlink(missing_ok=True)
        sidecar = old.with_suffix(old.suffix + ".json")
        sidecar.unlink(missing_ok=True)
        log(f"Removed expired local backup: {old.name}", log_file)


def write_metadata(path: Path, payload: dict) -> None:
    metadata_path = path.with_suffix(path.suffix + ".json")
    with metadata_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)


def create_remote_database_snapshot(config: dict, stamp: str) -> str:
    remote_path = f"/data/backups/automatic/brookshouse-{stamp}.db"
    keep = int(config.get("remote_database_retention", 8))
    code = (
        "import pathlib,sqlite3,sys;"
        "src=pathlib.Path('/data/app-data/brookshouse_store.db');"
        "dst=pathlib.Path(sys.argv[1]);keep=int(sys.argv[2]);"
        "dst.parent.mkdir(parents=True,exist_ok=True);"
        "s=sqlite3.connect(str(src));d=sqlite3.connect(str(dst));"
        "s.backup(d);d.close();s.close();"
        "c=sqlite3.connect(str(dst));q=c.execute('PRAGMA quick_check').fetchone()[0];c.close();"
        "assert q=='ok',q;"
        "files=sorted(dst.parent.glob('brookshouse-*.db'),key=lambda p:p.stat().st_mtime,reverse=True);"
        "[p.unlink() for p in files[keep:]];"
        "print(dst)"
    )
    railway_ssh(config, ["python", "-c", code, remote_path, str(keep)], timeout=600)
    return remote_path


def create_remote_media_snapshot(config: dict, stamp: str) -> str:
    remote_path = f"/data/backups/automatic-media/brookshouse-media-{stamp}.tar.gz"
    keep = int(config.get("remote_media_retention", 2))
    code = (
        "import pathlib,tarfile,sys;"
        "dst=pathlib.Path(sys.argv[1]);keep=int(sys.argv[2]);"
        "dst.parent.mkdir(parents=True,exist_ok=True);"
        "roots=[pathlib.Path('/data/storage-gallery'),pathlib.Path('/data/product-images'),"
        "pathlib.Path('/data/app-data/kids-profile-photos'),pathlib.Path('/data/app-data/kids-task-proofs'),"
        "pathlib.Path('/data/app-data/team-profile-photos')];"
        "t=tarfile.open(dst,'w:gz');"
        "[(t.add(str(p),arcname=str(p.relative_to('/data')))) for p in roots if p.exists()];"
        "t.close();"
        "files=sorted(dst.parent.glob('brookshouse-media-*.tar.gz'),key=lambda p:p.stat().st_mtime,reverse=True);"
        "[p.unlink() for p in files[keep:]];"
        "print(dst)"
    )
    railway_ssh(config, ["python", "-c", code, remote_path, str(keep)], timeout=3600)
    return remote_path


def run_database(config: dict, backup_root: Path, log_file: Path) -> None:
    stamp = now_stamp()
    local_dir = backup_root / "database"
    local_dir.mkdir(parents=True, exist_ok=True)
    remote_path = create_remote_database_snapshot(config, stamp)
    local_path = local_dir / f"brookshouse-railway-{stamp}.db"
    log(f"Remote SQLite snapshot created: {remote_path}", log_file)
    transfer = download_remote_file(config, remote_path, local_path, log_file)
    validation = validate_sqlite(local_path)
    metadata = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "database",
        "remote_path": remote_path,
        **transfer,
        **validation,
    }
    write_metadata(local_path, metadata)
    prune_local(local_dir, "brookshouse-railway-*.db", int(config.get("local_database_retention", 30)), log_file)
    log(f"DATABASE BACKUP SUCCESS: {local_path} | {json.dumps(metadata, default=str)}", log_file)


def run_media(config: dict, backup_root: Path, log_file: Path) -> None:
    stamp = now_stamp()
    local_dir = backup_root / "media"
    local_dir.mkdir(parents=True, exist_ok=True)
    remote_path = create_remote_media_snapshot(config, stamp)
    local_path = local_dir / f"brookshouse-media-{stamp}.tar.gz"
    log(f"Remote media archive created: {remote_path}", log_file)
    transfer = download_remote_file(config, remote_path, local_path, log_file)
    validation = validate_tar(local_path)
    metadata = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "media",
        "remote_path": remote_path,
        **transfer,
        **validation,
    }
    write_metadata(local_path, metadata)
    prune_local(local_dir, "brookshouse-media-*.tar.gz", int(config.get("local_media_retention", 4)), log_file)
    log(f"MEDIA BACKUP SUCCESS: {local_path} | {json.dumps(metadata, default=str)}", log_file)


def acquire_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        age_seconds = time.time() - lock_path.stat().st_mtime
        if age_seconds > 12 * 60 * 60:
            lock_path.unlink(missing_ok=True)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        return descriptor
    except FileExistsError as exc:
        raise BackupError(f"Another BrooksHouse backup appears to be running: {lock_path}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--mode", choices=["database", "media"], required=True)
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    backup_root = Path(config["backup_root"]).resolve()
    log_file = backup_root / "logs" / "backup.log"
    lock_path = backup_root / "backup.lock"
    lock_descriptor = None

    try:
        lock_descriptor = acquire_lock(lock_path)
        log(f"Starting {args.mode} backup", log_file)
        if args.mode == "database":
            run_database(config, backup_root, log_file)
        else:
            run_media(config, backup_root, log_file)
        return 0
    except Exception as exc:
        log(f"BACKUP FAILED ({args.mode}): {exc}", log_file)
        return 1
    finally:
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
