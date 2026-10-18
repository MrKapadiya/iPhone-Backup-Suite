"""
Thread-safe SQLite Ledger (`backup_ledger.db`) for tracking devices,
backup sessions, and verified photo/video/Live Photo offload records,
including optional JPEG sidecar paths.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class DeviceRecord:
    udid: str
    device_name: str
    product_type: str
    ios_version: str
    last_seen_utc: str
    last_full_backup_utc: Optional[str] = None


@dataclass
class MediaRecord:
    udid: str
    relative_dcim_path: str     # e.g. "100APPLE/IMG_4821.HEIC"
    dcim_folder: str            # e.g. "100APPLE"
    filename: str               # e.g. "IMG_4821.HEIC"
    file_size: int
    device_mtime_ts: int
    sha256_hash: str
    local_backup_path: str
    is_live_photo_paired: bool
    companion_mov_path: Optional[str]
    converted_jpeg_path: Optional[str]
    verified_on_disk: bool
    backed_up_at_utc: str


class BackupLedgerDB:
    """
    Thread-safe SQLite database with Write-Ahead Logging (WAL) enabled.
    Ensures atomic commits so interrupted transfers never corrupt state.
    """

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            base = Path.home() / ".iphone_backup"
            base.mkdir(parents=True, exist_ok=True)
            db_path = base / "backup_ledger.db"
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    udid TEXT PRIMARY KEY,
                    device_name TEXT NOT NULL,
                    product_type TEXT NOT NULL,
                    ios_version TEXT NOT NULL,
                    last_seen_utc TEXT NOT NULL,
                    last_full_backup_utc TEXT
                );

                CREATE TABLE IF NOT EXISTS media_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    udid TEXT NOT NULL,
                    relative_dcim_path TEXT NOT NULL,
                    dcim_folder TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    device_mtime_ts INTEGER NOT NULL,
                    sha256_hash TEXT NOT NULL,
                    local_backup_path TEXT NOT NULL,
                    is_live_photo_paired INTEGER NOT NULL DEFAULT 0,
                    companion_mov_path TEXT,
                    converted_jpeg_path TEXT,
                    verified_on_disk INTEGER NOT NULL DEFAULT 1,
                    backed_up_at_utc TEXT NOT NULL,
                    UNIQUE(udid, relative_dcim_path, file_size)
                );

                CREATE INDEX IF NOT EXISTS idx_media_udid_verified
                    ON media_ledger(udid, verified_on_disk);

                CREATE TABLE IF NOT EXISTS system_backups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    udid TEXT NOT NULL,
                    backup_dir TEXT NOT NULL,
                    is_full INTEGER NOT NULL,
                    is_encrypted INTEGER NOT NULL,
                    started_at_utc TEXT NOT NULL,
                    completed_at_utc TEXT,
                    status TEXT NOT NULL,
                    notes TEXT
                );
                """
            )
            # Migrate existing tables if converted_jpeg_path column is missing
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(media_ledger);").fetchall()]
            if "converted_jpeg_path" not in cols:
                conn.execute("ALTER TABLE media_ledger ADD COLUMN converted_jpeg_path TEXT;")

    def upsert_device(
        self,
        udid: str,
        device_name: str,
        product_type: str,
        ios_version: str,
        last_full_backup_utc: Optional[str] = None,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            if last_full_backup_utc:
                conn.execute(
                    """
                    INSERT INTO devices (udid, device_name, product_type, ios_version, last_seen_utc, last_full_backup_utc)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(udid) DO UPDATE SET
                        device_name=excluded.device_name,
                        product_type=excluded.product_type,
                        ios_version=excluded.ios_version,
                        last_seen_utc=excluded.last_seen_utc,
                        last_full_backup_utc=excluded.last_full_backup_utc
                    """,
                    (udid, device_name, product_type, ios_version, now_iso, last_full_backup_utc),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO devices (udid, device_name, product_type, ios_version, last_seen_utc)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(udid) DO UPDATE SET
                        device_name=excluded.device_name,
                        product_type=excluded.product_type,
                        ios_version=excluded.ios_version,
                        last_seen_utc=excluded.last_seen_utc
                    """,
                    (udid, device_name, product_type, ios_version, now_iso),
                )

    def is_media_already_verified(self, udid: str, relative_dcim_path: str, file_size: int) -> bool:
        """
        Returns True only if the asset is recorded in the ledger AND the destination
        file still physically exists on the host disk with the expected byte size.
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT local_backup_path, file_size, verified_on_disk
                FROM media_ledger
                WHERE udid = ? AND relative_dcim_path = ? AND file_size = ?
                """,
                (udid, relative_dcim_path, file_size),
            ).fetchone()

            if not row or not row["verified_on_disk"]:
                return False

            local_path = Path(row["local_backup_path"])
            if local_path.is_file() and local_path.stat().st_size == row["file_size"]:
                return True

            conn.execute(
                """
                UPDATE media_ledger
                SET verified_on_disk = 0
                WHERE udid = ? AND relative_dcim_path = ? AND file_size = ?
                """,
                (udid, relative_dcim_path, file_size),
            )
            return False

    def record_verified_media(self, record: MediaRecord) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO media_ledger (
                    udid, relative_dcim_path, dcim_folder, filename, file_size,
                    device_mtime_ts, sha256_hash, local_backup_path,
                    is_live_photo_paired, companion_mov_path, converted_jpeg_path,
                    verified_on_disk, backed_up_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(udid, relative_dcim_path, file_size) DO UPDATE SET
                    sha256_hash=excluded.sha256_hash,
                    local_backup_path=excluded.local_backup_path,
                    is_live_photo_paired=excluded.is_live_photo_paired,
                    companion_mov_path=excluded.companion_mov_path,
                    converted_jpeg_path=excluded.converted_jpeg_path,
                    verified_on_disk=1,
                    backed_up_at_utc=excluded.backed_up_at_utc
                """,
                (
                    record.udid,
                    record.relative_dcim_path,
                    record.dcim_folder,
                    record.filename,
                    record.file_size,
                    record.device_mtime_ts,
                    record.sha256_hash,
                    record.local_backup_path,
                    1 if record.is_live_photo_paired else 0,
                    record.companion_mov_path,
                    record.converted_jpeg_path,
                    1 if record.verified_on_disk else 0,
                    record.backed_up_at_utc,
                ),
            )

    def get_verified_media_for_device(self, udid: str, reverify_disk: bool = True) -> list[MediaRecord]:
        records: list[MediaRecord] = []
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM media_ledger
                WHERE udid = ? AND verified_on_disk = 1
                ORDER BY device_mtime_ts ASC
                """,
                (udid,),
            ).fetchall()

            for r in rows:
                local_path = Path(r["local_backup_path"])
                if reverify_disk and (not local_path.is_file() or local_path.stat().st_size != r["file_size"]):
                    conn.execute("UPDATE media_ledger SET verified_on_disk = 0 WHERE id = ?", (r["id"],))
                    continue

                records.append(
                    MediaRecord(
                        udid=r["udid"],
                        relative_dcim_path=r["relative_dcim_path"],
                        dcim_folder=r["dcim_folder"],
                        filename=r["filename"],
                        file_size=r["file_size"],
                        device_mtime_ts=r["device_mtime_ts"],
                        sha256_hash=r["sha256_hash"],
                        local_backup_path=r["local_backup_path"],
                        is_live_photo_paired=bool(r["is_live_photo_paired"]),
                        companion_mov_path=r["companion_mov_path"],
                        converted_jpeg_path=r["converted_jpeg_path"],
                        verified_on_disk=bool(r["verified_on_disk"]),
                        backed_up_at_utc=r["backed_up_at_utc"],
                    )
                )
        return records

    def log_system_backup_start(self, udid: str, backup_dir: str, is_full: bool, is_encrypted: bool) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO system_backups (udid, backup_dir, is_full, is_encrypted, started_at_utc, status)
                VALUES (?, ?, ?, ?, ?, 'IN_PROGRESS')
                """,
                (udid, backup_dir, int(is_full), int(is_encrypted), now_iso),
            )
            return int(cur.lastrowid)

    def log_system_backup_finish(self, backup_id: int, status: str, notes: str = "") -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE system_backups
                SET completed_at_utc = ?, status = ?, notes = ?
                WHERE id = ?
                """,
                (now_iso, status, notes, backup_id),
            )
