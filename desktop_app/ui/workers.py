"""
Dedicated QThread workers that run asyncio loops for pymobiledevice3 operations.
Keeps the PySide6 main GUI thread 100% responsive during multi-gigabyte transfers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal

from desktop_app.core import (
    AfcMediaOffloader,
    BackupAppError,
    BackupLedgerDB,
    BackupProgress,
    CompanionManifestSync,
    ConnectedDeviceInfo,
    DeviceManager,
    ExceptionRouter,
    MobileBackup2Engine,
    OffloadOptions,
    OffloadProgress,
    OffloadSummary,
    RemediationAction,
)


class DeviceWatcherWorker(QThread):
    """
    Polls USB/Network connections every 2.5 seconds and emits device telemetry
    or structured remediation actions without freezing the UI.
    """

    device_connected = Signal(object)      # ConnectedDeviceInfo
    device_disconnected = Signal()
    remediation_needed = Signal(object)    # RemediationAction
    log_emitted = Signal(str, str)         # (level, message)

    def __init__(self, db: BackupLedgerDB, router: ExceptionRouter, parent=None):
        super().__init__(parent)
        self.db = db
        self.router = router
        self.device_manager = DeviceManager(db=self.db, router=self.router)
        self._running = True
        self._last_udid: Optional[str] = None

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            while self._running:
                try:
                    devices = loop.run_until_complete(self.device_manager.list_connected_udids())
                    if not devices:
                        if self._last_udid is not None:
                            self.log_emitted.emit("INFO", f"iPhone ({self._last_udid}) disconnected.")
                            self._last_udid = None
                            self.device_disconnected.emit()
                    else:
                        udid, conn_type = devices[0]
                        telemetry = loop.run_until_complete(
                            self.device_manager.get_device_telemetry(udid=udid)
                        )
                        telemetry.connection_type = conn_type
                        if self._last_udid != telemetry.udid:
                            self.log_emitted.emit(
                                "INFO",
                                f"Connected to {telemetry.device_name} (iOS {telemetry.ios_version}) via {conn_type}",
                            )
                        self._last_udid = telemetry.udid
                        self.device_connected.emit(telemetry)

                except BackupAppError as exc:
                    self.remediation_needed.emit(exc.remediation)
                except Exception as exc:
                    rem = self.router.route(exc, context="device_watcher")
                    self.remediation_needed.emit(rem)

                self.msleep(2500)
        finally:
            loop.close()


class MediaOffloadWorker(QThread):
    """
    Executes incremental SHA-256 verified `/DCIM` photo & Live Photo offload on a background thread.
    Supports configurable Date Hierarchy folders, optional HEIC-to-JPEG conversion, and live thumbnails.
    """

    progress_updated = Signal(object)      # OffloadProgress
    offload_completed = Signal(object)     # OffloadSummary
    remediation_needed = Signal(object)    # RemediationAction
    log_emitted = Signal(str, str)

    def __init__(
        self,
        destination_dir: Path,
        options: OffloadOptions,
        udid: Optional[str],
        db: BackupLedgerDB,
        router: ExceptionRouter,
        parent=None,
    ):
        super().__init__(parent)
        self.destination_dir = destination_dir
        self.options = options
        self.udid = udid
        self.db = db
        self.router = router
        self.offloader = AfcMediaOffloader(db=self.db, router=self.router)

    def cancel(self) -> None:
        self.offloader.cancel()

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            heic_mode = "Enabled (Sidecar .JPG)" if self.options.convert_heic_to_jpeg else "Disabled (Original Format)"
            self.log_emitted.emit(
                "INFO",
                f"Starting Smart Offload to {self.destination_dir} | "
                f"Layout: {self.options.folder_mode.value} | HEIC->JPEG: {heic_mode}",
            )
            summary = loop.run_until_complete(
                self.offloader.offload_photos(
                    destination_dir=self.destination_dir,
                    options=self.options,
                    udid=self.udid,
                    progress_callback=lambda p: self.progress_updated.emit(p),
                )
            )
            self.log_emitted.emit(
                "INFO",
                f"Offload finished: {summary.newly_backed_up} new, "
                f"{summary.skipped_existing} skipped, "
                f"{summary.live_photos_paired} Live Photos paired, "
                f"{summary.converted_jpegs} JPEGs exported.",
            )
            self.offload_completed.emit(summary)
        except BackupAppError as exc:
            self.log_emitted.emit("ERROR", f"Offload error: {exc.remediation.title} - {exc.remediation.user_message}")
            self.remediation_needed.emit(exc.remediation)
        except Exception as exc:
            rem = self.router.route(exc, context="afc_offload_worker")
            self.log_emitted.emit("ERROR", f"Offload exception: {rem.user_message}")
            self.remediation_needed.emit(rem)
        finally:
            loop.close()


class SystemBackupWorker(QThread):
    """
    Executes full or incremental iOS system backup (`Mobilebackup2Service`) on a background thread.
    """

    progress_updated = Signal(object)      # BackupProgress
    backup_completed = Signal(str)         # Final backup folder path
    remediation_needed = Signal(object)    # RemediationAction
    log_emitted = Signal(str, str)

    def __init__(
        self,
        backup_dir: Path,
        full_backup: bool,
        udid: Optional[str],
        db: BackupLedgerDB,
        router: ExceptionRouter,
        parent=None,
    ):
        super().__init__(parent)
        self.backup_dir = backup_dir
        self.full_backup = full_backup
        self.udid = udid
        self.db = db
        self.router = router
        self.engine = MobileBackup2Engine(db=self.db, router=self.router)

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            mode_str = "Full" if self.full_backup else "Incremental"
            self.log_emitted.emit("INFO", f"Starting {mode_str} iOS System Backup to {self.backup_dir}...")
            final_path = loop.run_until_complete(
                self.engine.run_system_backup(
                    backup_directory=self.backup_dir,
                    full_backup=self.full_backup,
                    udid=self.udid,
                    progress_callback=lambda p: self.progress_updated.emit(p),
                )
            )
            self.log_emitted.emit("INFO", f"System backup completed at {final_path}")
            self.backup_completed.emit(str(final_path))
        except BackupAppError as exc:
            self.log_emitted.emit("ERROR", f"System backup failed: {exc.remediation.user_message}")
            self.remediation_needed.emit(exc.remediation)
        except Exception as exc:
            rem = self.router.route(exc, context="system_backup_worker")
            self.log_emitted.emit("ERROR", f"System backup error: {rem.user_message}")
            self.remediation_needed.emit(rem)
        finally:
            loop.close()


class ManifestPushWorker(QThread):
    """
    Pushes `backed_up_manifest.json` over USB (`HouseArrestService`) to the iOS Companion App.
    """

    push_succeeded = Signal(dict)          # Manifest payload dict
    remediation_needed = Signal(object)    # RemediationAction
    log_emitted = Signal(str, str)

    def __init__(
        self,
        bundle_id: str,
        udid: Optional[str],
        db: BackupLedgerDB,
        router: ExceptionRouter,
        parent=None,
    ):
        super().__init__(parent)
        self.bundle_id = bundle_id
        self.udid = udid
        self.db = db
        self.router = router
        self.sync = CompanionManifestSync(bundle_id=self.bundle_id, db=self.db, router=self.router)

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.log_emitted.emit("INFO", f"Pushing verified manifest to {self.bundle_id} over USB...")
            payload = loop.run_until_complete(self.sync.push_manifest_via_usb(udid=self.udid))
            count = payload.get("total_verified_assets", 0)
            self.log_emitted.emit(
                "INFO",
                f"Successfully pushed manifest ({count} verified assets) to iPhone Companion App.",
            )
            self.push_succeeded.emit(payload)
        except BackupAppError as exc:
            self.log_emitted.emit("WARN", f"Manifest USB push warning: {exc.remediation.user_message}")
            self.remediation_needed.emit(exc.remediation)
        except Exception as exc:
            rem = self.router.route(exc, context="house_arrest")
            self.remediation_needed.emit(rem)
        finally:
            loop.close()
