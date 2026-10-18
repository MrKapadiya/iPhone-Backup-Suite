"""
Centralized Exception Handling & OS-Aware Remediation Engine.
Translates low-level pymobiledevice3, USB multiplexer, lockdown, AFC, and OS
errors into structured, actionable recovery payloads for the UI layer.
"""

from __future__ import annotations

import logging
import platform
import traceback
from dataclasses import dataclass, field
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Optional


class ErrorCategory(str, Enum):
    DAEMON_OR_DRIVER = "DAEMON_OR_DRIVER"
    PAIRING_AND_TRUST = "PAIRING_AND_TRUST"
    DEVICE_LOCKED = "DEVICE_LOCKED"
    BACKUP_ENCRYPTION = "BACKUP_ENCRYPTION"
    USB_DISCONNECTED = "USB_DISCONNECTED"
    STORAGE_EXHAUSTED = "STORAGE_EXHAUSTED"
    COMPANION_APP_MISSING = "COMPANION_APP_MISSING"
    AFC_FILESYSTEM = "AFC_FILESYSTEM"
    UNKNOWN = "UNKNOWN"


@dataclass
class RemediationAction:
    """Structured recovery payload consumed by the PySide6 UI."""
    category: ErrorCategory
    title: str
    user_message: str
    steps: list[str] = field(default_factory=list)
    can_auto_retry: bool = False
    retry_interval_sec: float = 2.0
    technical_details: str = ""
    os_platform: str = field(default_factory=lambda: platform.system())


class BackupAppError(Exception):
    """Application-level exception wrapping a structured RemediationAction."""

    def __init__(self, remediation: RemediationAction, original_exc: Optional[BaseException] = None):
        super().__init__(remediation.user_message)
        self.remediation = remediation
        self.original_exc = original_exc


def setup_logging(log_dir: Optional[Path] = None) -> logging.Logger:
    """Configure a rotating file logger under ~/.iphone_backup/logs/app.log."""
    if log_dir is None:
        log_dir = Path.home() / ".iphone_backup" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"

    logger = logging.getLogger("iphone_backup")
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(threadName)s | %(name)s:%(lineno)d | %(message)s"
    )

    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


class ExceptionRouter:
    """
    Inspects exceptions from pymobiledevice3 and the host OS, logs full tracebacks,
    and returns a structured RemediationAction for the GUI.
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or setup_logging()
        self.os_name = platform.system()  # 'Darwin', 'Windows', 'Linux'

    def route(self, exc: BaseException, context: str = "") -> RemediationAction:
        if isinstance(exc, BackupAppError):
            self.logger.warning("Handled BackupAppError [%s]: %s", context, exc.remediation.title)
            return exc.remediation

        exc_name = type(exc).__name__
        exc_msg = str(exc)
        tb_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.logger.error("Exception in [%s] (%s: %s)\n%s", context, exc_name, exc_msg, tb_str)

        # 1. Host Disk Full / Permission Errors
        if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
            return RemediationAction(
                category=ErrorCategory.STORAGE_EXHAUSTED,
                title="Host Computer Disk Full",
                user_message="Your computer ran out of free disk space during the backup transfer.",
                steps=[
                    "Free up disk space on the selected destination drive or choose an external SSD/HDD.",
                    "Click 'Resume' — all previously verified photos are safely recorded and will not be re-downloaded.",
                ],
                can_auto_retry=False,
                technical_details=f"{exc_name}: {exc_msg}",
            )

        # 2. System Daemon / USB Multiplexer / Driver Missing
        if exc_name in ("MuxException", "NoDeviceConnectedError", "ConnectionFailedError") or (
            isinstance(exc, (ConnectionRefusedError, FileNotFoundError))
            and ("usbmux" in exc_msg.lower() or "lockdown" in exc_msg.lower() or context == "usbmux")
        ):
            return self._build_daemon_remediation(exc_name, exc_msg)

        # 3. Pairing & Trust Errors
        if exc_name in (
            "NotPairedError",
            "PairingError",
            "UserDeniedPairingError",
            "InvalidHostIDError",
        ) or "not paired" in exc_msg.lower() or "invalidhostid" in exc_msg.lower():
            return RemediationAction(
                category=ErrorCategory.PAIRING_AND_TRUST,
                title="Trust This Computer on Your iPhone",
                user_message="Your iPhone has not trusted this computer yet, or the pairing record expired.",
                steps=[
                    "1. Unlock your iPhone screen.",
                    "2. Tap 'Trust' on the 'Trust This Computer?' prompt.",
                    "3. Enter your iPhone passcode to confirm.",
                ],
                can_auto_retry=True,
                retry_interval_sec=2.0,
                technical_details=f"{exc_name}: {exc_msg}",
            )

        # 4. Device Locked / Passcode Required
        if exc_name in ("PasscodeRequiredError", "PasswordRequiredError", "DeviceLockedError") or (
            "passcode" in exc_msg.lower() or "password protected" in exc_msg.lower()
        ):
            return RemediationAction(
                category=ErrorCategory.DEVICE_LOCKED,
                title="iPhone is Locked",
                user_message="iOS requires the device to be unlocked before starting lockdown or backup services.",
                steps=[
                    "Unlock your iPhone using Face ID, Touch ID, or your passcode.",
                    "Keep the screen awake while pairing and backup initialization complete.",
                ],
                can_auto_retry=True,
                retry_interval_sec=2.0,
                technical_details=f"{exc_name}: {exc_msg}",
            )

        # 5. Encrypted Backup Password Errors
        if "backup" in context.lower() and (
            "encrypt" in exc_msg.lower() or "password" in exc_msg.lower() or exc_name == "BackupEncryptedError"
        ):
            return RemediationAction(
                category=ErrorCategory.BACKUP_ENCRYPTION,
                title="Encrypted Backup Password Required",
                user_message="This iPhone has encrypted backups enabled. Please provide your iOS backup password.",
                steps=[
                    "Enter your iPhone Backup Password in the Backup Encryption settings dialog.",
                    "If you want unencrypted backups, disable 'Encrypt Local Backup' using your current password.",
                ],
                can_auto_retry=False,
                technical_details=f"{exc_name}: {exc_msg}",
            )

        # 6. Companion App Missing on iPhone (HouseArrestService)
        if exc_name == "AppNotInstalledError" or "applicationnotinstalled" in exc_msg.lower() or (
            context == "house_arrest" and ("vend" in exc_msg.lower() or "installationlookupfailed" in exc_msg.lower())
        ):
            return RemediationAction(
                category=ErrorCategory.COMPANION_APP_MISSING,
                title="iOS Companion App Not Found on iPhone",
                user_message="Could not push the backup manifest because 'BackupCompanion' is not installed on this iPhone.",
                steps=[
                    "Install the BackupCompanion iOS app on your iPhone with UIFileSharingEnabled set to true.",
                    "Alternatively, use the 'Start Local Wi-Fi Manifest Server' button to sync the manifest over Wi-Fi.",
                ],
                can_auto_retry=False,
                technical_details=f"{exc_name}: {exc_msg}",
            )

        # 7. Mid-Transfer USB Disconnection / Pipe Broken
        if isinstance(exc, (BrokenPipeError, TimeoutError, ConnectionResetError, EOFError)) or exc_name in (
            "ConnectionTerminatedError",
            "MuxWriteError",
            "AfcException",
        ):
            return RemediationAction(
                category=ErrorCategory.USB_DISCONNECTED,
                title="USB Connection Interrupted",
                user_message="Communication with the iPhone dropped during transfer.",
                steps=[
                    "Check that the Lightning / USB-C cable is firmly connected.",
                    "Avoid loose USB hubs; plug directly into a host port if possible.",
                    "Click 'Resume' once reconnected — all completed photos were saved and verified.",
                ],
                can_auto_retry=True,
                retry_interval_sec=3.0,
                technical_details=f"{exc_name}: {exc_msg}",
            )

        # Fallback Unknown Error
        return RemediationAction(
            category=ErrorCategory.UNKNOWN,
            title=f"Unexpected Error ({exc_name})",
            user_message=exc_msg or "An unexpected error occurred while communicating with the iPhone.",
            steps=[
                "Ensure your iPhone is unlocked and connected via a data-capable USB cable.",
                "Check the Diagnostics tab for full traceback logs.",
            ],
            can_auto_retry=False,
            technical_details=tb_str,
        )

    def _build_daemon_remediation(self, exc_name: str, exc_msg: str) -> RemediationAction:
        if self.os_name == "Windows":
            steps = [
                "Ensure 'Apple Mobile Device Service' (AMDS) is installed and running in Windows Services (services.msc).",
                "If you don't have iTunes installed, install 'Apple Devices' from the Microsoft Store or the standalone Apple Mobile Device Support MSI.",
                "Unplug and reconnect your iPhone after starting the service.",
            ]
        elif self.os_name == "Linux":
            steps = [
                "Ensure 'usbmuxd' is installed (`sudo apt install usbmuxd libimobiledevice6`).",
                "Start or restart the daemon: `sudo systemctl restart usbmuxd`.",
                "Unplug and reconnect your iPhone so udev assigns permissions.",
            ]
        else:  # macOS (Darwin)
            steps = [
                "Connect your iPhone with a data-capable USB-C or Lightning cable.",
                "Verify macOS `usbmuxd` is running (`pgrep -lf usbmuxd`).",
                "If your iPhone does not appear in Finder, try another USB port or cable.",
            ]

        return RemediationAction(
            category=ErrorCategory.DAEMON_OR_DRIVER,
            title=f"iOS USB Multiplexer / Driver Unavailable ({self.os_name})",
            user_message="Could not connect to the Apple USB multiplexer daemon (usbmuxd) or no iPhone was detected.",
            steps=steps,
            can_auto_retry=True,
            retry_interval_sec=3.0,
            technical_details=f"{exc_name}: {exc_msg}",
        )
