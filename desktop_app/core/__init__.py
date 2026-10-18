"""
Core backend package for the Cross-Platform iPhone Backup & Media Offload Suite v2.
Supports macOS (Intel/ARM64), Windows (x64/ARM64), and Linux (x64/ARM64).
"""

from .exceptions import (
    BackupAppError,
    ExceptionRouter,
    ErrorCategory,
    RemediationAction,
    setup_logging,
)
from .database import BackupLedgerDB, MediaRecord, DeviceRecord
from .device_manager import DeviceManager, ConnectedDeviceInfo
from .afc_offloader import (
    AfcMediaOffloader,
    FolderStructureMode,
    OffloadOptions,
    OffloadProgress,
    OffloadSummary,
)
from .backup2_engine import MobileBackup2Engine, BackupProgress
from .house_arrest_sync import CompanionManifestSync

__all__ = [
    "BackupAppError",
    "ExceptionRouter",
    "ErrorCategory",
    "RemediationAction",
    "setup_logging",
    "BackupLedgerDB",
    "MediaRecord",
    "DeviceRecord",
    "DeviceManager",
    "ConnectedDeviceInfo",
    "AfcMediaOffloader",
    "FolderStructureMode",
    "OffloadOptions",
    "OffloadProgress",
    "OffloadSummary",
    "MobileBackup2Engine",
    "BackupProgress",
    "CompanionManifestSync",
]
