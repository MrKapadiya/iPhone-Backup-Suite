"""
Cross-Platform Device Discovery, Lockdown Pairing, Telemetry, and Power Assertion Manager.
Includes a transparent sync/async bridge so both synchronous (v3/v4) and asynchronous (v7-v11+)
releases of `pymobiledevice3` execute cleanly.
"""

from __future__ import annotations

import asyncio
import inspect
import platform
import shutil
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Optional

from .database import BackupLedgerDB
from .exceptions import BackupAppError, ExceptionRouter


async def maybe_await(val: Any) -> Any:
    """Transparently awaits coroutines while passing synchronous return values through."""
    if inspect.isawaitable(val):
        return await val
    return val


@dataclass
class ConnectedDeviceInfo:
    udid: str
    connection_type: str          # "USB" or "Network"
    device_name: str
    product_type: str             # e.g., "iPhone16,2"
    hardware_model: str
    ios_version: str
    build_version: str
    battery_level: int            # 0-100
    is_charging: bool
    total_disk_bytes: int
    free_disk_bytes: int
    used_disk_bytes: int
    photo_usage_bytes: int
    backup_will_encrypt: bool
    is_paired: bool


@dataclass
class HostPreflightReport:
    os_name: str
    architecture: str
    usbmuxd_available: bool
    pymobiledevice3_installed: bool
    pymobiledevice3_version: str
    details: list[str]


class DeviceManager:
    """
    Manages USB/Network iOS device discovery, Lockdown pairing, battery/disk telemetry,
    and power assertions via pymobiledevice3.
    """

    def __init__(self, db: Optional[BackupLedgerDB] = None, router: Optional[ExceptionRouter] = None):
        self.db = db or BackupLedgerDB()
        self.router = router or ExceptionRouter()

    def run_host_preflight_check(self) -> HostPreflightReport:
        """Verifies host OS architecture, usbmuxd / Apple Mobile Device Service, and pymobiledevice3."""
        os_name = platform.system()
        arch = platform.machine()
        details: list[str] = [f"Host OS: {os_name} ({arch})"]
        pmd_installed = False
        pmd_version = "Not Installed"

        try:
            import importlib.metadata
            pmd_version = importlib.metadata.version("pymobiledevice3")
            pmd_installed = True
            details.append(f"pymobiledevice3 version: {pmd_version}")
        except Exception:
            details.append("ERROR: pymobiledevice3 package is not installed.")

        usbmux_ok = False
        if os_name == "Darwin":
            usbmux_ok = Path_exists("/var/run/usbmuxd")
            details.append("macOS /var/run/usbmuxd socket: " + ("Present" if usbmux_ok else "Missing"))
        elif os_name == "Linux":
            usbmux_ok = Path_exists("/var/run/usbmuxd") or shutil.which("usbmuxd") is not None
            details.append("Linux usbmuxd daemon: " + ("Detected" if usbmux_ok else "Missing"))
        elif os_name == "Windows":
            try:
                res = subprocess.run(
                    ["sc", "query", "Apple Mobile Device Service"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                usbmux_ok = "RUNNING" in res.stdout
                details.append("Windows Apple Mobile Device Service: " + ("RUNNING" if usbmux_ok else "NOT RUNNING"))
            except Exception as exc:
                details.append(f"Could not query Windows service status: {exc}")

        return HostPreflightReport(
            os_name=os_name,
            architecture=arch,
            usbmuxd_available=usbmux_ok,
            pymobiledevice3_installed=pmd_installed,
            pymobiledevice3_version=pmd_version,
            details=details,
        )

    async def list_connected_udids(self, include_network: bool = True) -> list[tuple[str, str]]:
        """
        Returns a list of (udid, connection_type) tuples for all connected iOS devices.
        """
        try:
            from pymobiledevice3 import usbmux

            mux_devices = await maybe_await(usbmux.list_devices())
            results: list[tuple[str, str]] = []
            for dev in mux_devices:
                serial = getattr(dev, "serial", None)
                if not serial:
                    continue
                is_usb = getattr(dev, "is_usb", True)
                conn_type = "USB" if is_usb else "Network"
                if not is_usb and not include_network:
                    continue
                results.append((serial, conn_type))
            return results
        except Exception as exc:
            remediation = self.router.route(exc, context="usbmux")
            raise BackupAppError(remediation, exc) from exc

    async def open_lockdown_client(self, udid: Optional[str] = None, autopair: bool = True) -> Any:
        """
        Creates and returns an authenticated LockdownClient for the target UDID.
        Handles both sync and async versions of `create_using_usbmux`.
        """
        try:
            from pymobiledevice3.lockdown import create_using_usbmux

            client = await maybe_await(
                create_using_usbmux(serial=udid, autopair=autopair)
            )
            return client
        except Exception as exc:
            remediation = self.router.route(exc, context="lockdown_connect")
            raise BackupAppError(remediation, exc) from exc

    async def pair_device(self, udid: Optional[str] = None) -> bool:
        """Explicitly triggers pairing if the device is not yet trusted."""
        try:
            from pymobiledevice3.lockdown import create_using_usbmux

            client = await maybe_await(create_using_usbmux(serial=udid, autopair=False))
            paired = getattr(client, "paired", False)
            if not paired:
                await maybe_await(client.pair())
            return True
        except Exception as exc:
            remediation = self.router.route(exc, context="lockdown_pair")
            raise BackupAppError(remediation, exc) from exc

    async def get_device_telemetry(self, udid: Optional[str] = None) -> ConnectedDeviceInfo:
        """Queries comprehensive device identity, battery state, disk space, and backup encryption flags."""
        try:
            lockdown = await self.open_lockdown_client(udid=udid, autopair=True)

            all_vals = getattr(lockdown, "all_values", None)
            if callable(all_vals):
                all_vals = await maybe_await(all_vals())
            elif not isinstance(all_vals, dict):
                all_vals = await maybe_await(lockdown.get_value())

            dev_udid = all_vals.get("UniqueDeviceID") or getattr(lockdown, "udid", udid or "unknown")
            dev_name = all_vals.get("DeviceName", "iPhone")
            product_type = all_vals.get("ProductType", "iPhone")
            hw_model = all_vals.get("HardwareModel", "Unknown")
            ios_version = all_vals.get("ProductVersion", "Unknown")
            build_version = all_vals.get("BuildVersion", "Unknown")

            # Disk usage domain
            disk_info = await maybe_await(lockdown.get_value(domain="com.apple.disk_usage")) or {}
            total_disk = int(disk_info.get("TotalDiskCapacity", 0))
            free_disk = int(disk_info.get("AmountDataAvailable", disk_info.get("TotalDataAvailable", 0)))
            used_disk = max(0, total_disk - free_disk)
            photo_usage = int(disk_info.get("CameraUsage", disk_info.get("PhotoUsage", 0)))

            # Battery domain
            batt_info = await maybe_await(lockdown.get_value(domain="com.apple.mobile.battery")) or {}
            battery_level = int(batt_info.get("BatteryCurrentCapacity", 100))
            is_charging = bool(batt_info.get("BatteryIsCharging", False) or batt_info.get("ExternalConnected", False))

            # Backup encryption state
            backup_domain = await maybe_await(lockdown.get_value(domain="com.apple.mobile.backup")) or {}
            will_encrypt = bool(backup_domain.get("WillEncrypt", False))

            info = ConnectedDeviceInfo(
                udid=dev_udid,
                connection_type="USB",
                device_name=dev_name,
                product_type=product_type,
                hardware_model=hw_model,
                ios_version=ios_version,
                build_version=build_version,
                battery_level=battery_level,
                is_charging=is_charging,
                total_disk_bytes=total_disk,
                free_disk_bytes=free_disk,
                used_disk_bytes=used_disk,
                photo_usage_bytes=photo_usage,
                backup_will_encrypt=will_encrypt,
                is_paired=True,
            )

            self.db.upsert_device(
                udid=info.udid,
                device_name=info.device_name,
                product_type=info.product_type,
                ios_version=info.ios_version,
            )
            return info
        except BackupAppError:
            raise
        except Exception as exc:
            remediation = self.router.route(exc, context="device_telemetry")
            raise BackupAppError(remediation, exc) from exc

    @asynccontextmanager
    async def prevent_device_sleep(self, lockdown: Any) -> AsyncGenerator[None, None]:
        """
        Acquires an iOS power assertion via `AssertionAgentService` during active
        backups or photo offloads so the device does not drop its connection.
        """
        assertion_service = None
        try:
            from pymobiledevice3.services.assertion_agent import AssertionAgentService
            assertion_service = AssertionAgentService(lockdown=lockdown)
            if hasattr(assertion_service, "__aenter__"):
                await assertion_service.__aenter__()
        except Exception:
            # Non-fatal if AssertionAgentService is unavailable on a specific iOS build
            assertion_service = None

        try:
            yield
        finally:
            if assertion_service is not None:
                try:
                    if hasattr(assertion_service, "__aexit__"):
                        await assertion_service.__aexit__(None, None, None)
                    elif hasattr(assertion_service, "close"):
                        await maybe_await(assertion_service.close())
                except Exception:
                    pass


def Path_exists(path_str: str) -> bool:
    from pathlib import Path
    try:
        return Path(path_str).exists()
    except Exception:
        return False
