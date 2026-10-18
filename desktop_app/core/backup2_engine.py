"""
Full & Incremental iOS System Backup Engine wrapping `Mobilebackup2Service`.
Includes pre-backup disk space verification, backup encryption configuration,
and real-time progress reporting.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .database import BackupLedgerDB
from .device_manager import DeviceManager, maybe_await
from .exceptions import BackupAppError, ErrorCategory, ExceptionRouter, RemediationAction


@dataclass
class BackupProgress:
    percentage: float
    status_message: str


class MobileBackup2Engine:
    """
    Wraps `pymobiledevice3.services.mobilebackup2.Mobilebackup2Service` for full and
    incremental system backups, restore operations, and backup encryption management.
    """

    def __init__(
        self,
        device_manager: Optional[DeviceManager] = None,
        db: Optional[BackupLedgerDB] = None,
        router: Optional[ExceptionRouter] = None,
    ):
        self.db = db or BackupLedgerDB()
        self.router = router or ExceptionRouter()
        self.device_manager = device_manager or DeviceManager(db=self.db, router=self.router)

    async def configure_backup_encryption(
        self,
        enable: bool,
        password: str,
        current_password: str = "",
        udid: Optional[str] = None,
    ) -> None:
        """Enables, disables, or updates local backup encryption on the connected iPhone."""
        lockdown = await self.device_manager.open_lockdown_client(udid=udid, autopair=True)
        try:
            from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service

            mb2 = Mobilebackup2Service(lockdown)
            if enable:
                await maybe_await(mb2.change_password(old=current_password, new=password))
            else:
                await maybe_await(mb2.change_password(old=password, new=""))
        except Exception as exc:
            remediation = self.router.route(exc, context="backup_encryption")
            raise BackupAppError(remediation, exc) from exc

    async def run_system_backup(
        self,
        backup_directory: Path,
        full_backup: bool = False,
         udid: Optional[str] = None,
        progress_callback: Optional[Callable[[BackupProgress], None]] = None,
    ) -> Path:
        """
        Executes a full or incremental iOS system backup using `Mobilebackup2Service`.
        Verifies host free disk space prior to starting.
        """
        backup_directory = Path(backup_directory).expanduser().resolve()
        backup_directory.mkdir(parents=True, exist_ok=True)

        lockdown = await self.device_manager.open_lockdown_client(udid=udid, autopair=True)
        telemetry = await self.device_manager.get_device_telemetry(udid=udid)

        # Verify host free disk space (require at least used_disk_bytes * 0.85 for full backup)
        host_free = shutil.disk_usage(backup_directory).free
        required_bytes = int(telemetry.used_disk_bytes * (0.85 if full_backup else 0.15))
        if required_bytes > 0 and host_free < required_bytes:
            raise BackupAppError(
                RemediationAction(
                    category=ErrorCategory.STORAGE_EXHAUSTED,
                    title="Insufficient Disk Space for System Backup",
                    user_message=(
                        f"Estimated space needed is {(required_bytes / 1e9):.1f} GB, "
                        f"but {backup_directory} only has {(host_free / 1e9):.1f} GB free."
                    ),
                    steps=[
                        "Choose a drive with more free capacity.",
                        "Or use 'Smart Photo Offload' first to move large videos/photos before running a system backup.",
                    ],
                )
            )

        backup_row_id = self.db.log_system_backup_start(
            udid=telemetry.udid,
            backup_dir=str(backup_directory),
            is_full=full_backup,
            is_encrypted=telemetry.backup_will_encrypt,
        )

        def _on_progress(pct: float) -> None:
            if progress_callback:
                progress_callback(
                    BackupProgress(
                        percentage=float(pct),
                        status_message=f"Backing up {telemetry.device_name} ({pct:.1f}%)",
                    )
                )

        try:
            from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service

            mb2 = Mobilebackup2Service(lockdown)
            async with self.device_manager.prevent_device_sleep(lockdown):
                await maybe_await(
                    mb2.backup(
                        full=full_backup,
                        backup_directory=str(backup_directory),
                        progress_callback=_on_progress,
                    )
                )

            now_iso = datetime.now(timezone.utc).isoformat()
            self.db.log_system_backup_finish(backup_row_id, status="COMPLETED")
            self.db.upsert_device(
                udid=telemetry.udid,
                device_name=telemetry.device_name,
                product_type=telemetry.product_type,
                ios_version=telemetry.ios_version,
                last_full_backup_utc=now_iso,
            )
            return backup_directory / telemetry.udid

        except Exception as exc:
            self.db.log_system_backup_finish(backup_row_id, status="FAILED", notes=str(exc))
            remediation = self.router.route(exc, context="mobilebackup2")
            raise BackupAppError(remediation, exc) from exc
