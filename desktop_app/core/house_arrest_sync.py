"""
Zero-Cloud USB & Local Wi-Fi Manifest Sync Bridge to the iOS Companion App.
Generates `backed_up_manifest.json` from verified SQLite ledger entries and pushes
it directly into `/Documents/backed_up_manifest.json` inside the iOS Companion App's
sandbox via `HouseArrestService` (`VendDocuments`).
"""

from __future__ import annotations

import json
import socket
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Optional

from .database import BackupLedgerDB
from .device_manager import DeviceManager, maybe_await
from .exceptions import BackupAppError, ExceptionRouter


class CompanionManifestSync:
    """
    Synchronizes the verified photo ledger to the iOS Companion App (`BackupCompanion`)
    so the user can review backed-up photos in the 'Backed Up' album and batch-delete them.
    """

    DEFAULT_BUNDLE_ID = "com.yourorg.BackupCompanion"
    REMOTE_MANIFEST_PATH = "/Documents/backed_up_manifest.json"

    def __init__(
        self,
        bundle_id: str = DEFAULT_BUNDLE_ID,
        device_manager: Optional[DeviceManager] = None,
        db: Optional[BackupLedgerDB] = None,
        router: Optional[ExceptionRouter] = None,
    ):
        self.bundle_id = bundle_id
        self.db = db or BackupLedgerDB()
        self.router = router or ExceptionRouter()
        self.device_manager = device_manager or DeviceManager(db=self.db, router=self.router)
        self._http_server: Optional[HTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None

    def build_manifest_payload(self, udid: str) -> dict[str, Any]:
        """Builds the JSON manifest dictionary containing only disk-verified media assets."""
        records = self.db.get_verified_media_for_device(udid=udid, reverify_disk=True)
        assets_list = [
            {
                "filename": r.filename,
                "dcim_folder": r.dcim_folder,
                "relative_dcim_path": r.relative_dcim_path,
                "file_size": r.file_size,
                "created_timestamp": r.device_mtime_ts,
                "sha256": r.sha256_hash,
                "is_live_photo_paired": r.is_live_photo_paired,
                "backed_up_at_utc": r.backed_up_at_utc,
            }
            for r in records
        ]
        return {
            "schema_version": 1,
            "sync_id": str(uuid.uuid4()),
            "device_udid": udid,
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "total_verified_assets": len(assets_list),
            "total_verified_bytes": sum(a["file_size"] for a in assets_list),
            "assets": assets_list,
        }

    async def push_manifest_via_usb(self, udid: Optional[str] = None) -> dict[str, Any]:
        """
        Pushes `backed_up_manifest.json` directly into the iOS Companion App's `/Documents`
        container over USB using `HouseArrestService` (`VendDocuments`).
        Compatible with both sync and async `HouseArrestService` implementations.
        """
        lockdown = await self.device_manager.open_lockdown_client(udid=udid, autopair=True)
        telemetry = await self.device_manager.get_device_telemetry(udid=udid)
        payload = self.build_manifest_payload(telemetry.udid)
        manifest_bytes = json.dumps(payload, indent=2).encode("utf-8")

        house_arrest = None
        try:
            from pymobiledevice3.services.house_arrest import HouseArrestService

            # Newer pymobiledevice3 exposes `HouseArrestService.create(lockdown, bundle_id, documents_only=True)`
            if hasattr(HouseArrestService, "create"):
                house_arrest = await maybe_await(
                    HouseArrestService.create(
                        lockdown=lockdown,
                        bundle_id=self.bundle_id,
                        documents_only=True,
                    )
                )
            else:
                # Older pymobiledevice3 constructor signature
                house_arrest = HouseArrestService(
                    lockdown=lockdown,
                    bundle_id=self.bundle_id,
                    send_cmd="VendDocuments",
                )

            # Write `/Documents/backed_up_manifest.json` atomically via AFC
            if hasattr(house_arrest, "set_file_contents"):
                await maybe_await(
                    house_arrest.set_file_contents(self.REMOTE_MANIFEST_PATH, manifest_bytes)
                )
            else:
                handle = await maybe_await(house_arrest.fopen(self.REMOTE_MANIFEST_PATH, "wb"))
                try:
                    await maybe_await(house_arrest.fwrite(handle, manifest_bytes))
                finally:
                    await maybe_await(house_arrest.fclose(handle))

            return payload

        except Exception as exc:
            remediation = self.router.route(exc, context="house_arrest")
            raise BackupAppError(remediation, exc) from exc
        finally:
            if house_arrest is not None and hasattr(house_arrest, "close"):
                try:
                    await maybe_await(house_arrest.close())
                except Exception:
                    pass

    def start_fallback_wifi_manifest_server(self, udid: str, port: int = 8765) -> str:
        """
        Starts a lightweight local HTTP server serving `/backed_up_manifest.json` on the LAN
        in case the user wants to pull the manifest over Wi-Fi inside the iOS Companion App.
        Returns the full LAN URL (e.g., `http://192.168.1.42:8765/backed_up_manifest.json`).
        """
        self.stop_fallback_wifi_manifest_server()
        payload_bytes = json.dumps(self.build_manifest_payload(udid), indent=2).encode("utf-8")

        class _ManifestHandler(BaseHTTPRequestHandler):
            def do_GET(self_req) -> None:
                if self_req.path.startswith("/backed_up_manifest.json"):
                    self_req.send_response(200)
                    self_req.send_header("Content-Type", "application/json; charset=utf-8")
                    self_req.send_header("Content-Length", str(len(payload_bytes)))
                    self_req.end_headers()
                    self_req.wfile.write(payload_bytes)
                else:
                    self_req.send_response(404)
                    self_req.end_headers()

            def log_message(self_req, format: str, *args: Any) -> None:
                pass  # Silence console spam

        self._http_server = HTTPServer(("0.0.0.0", port), _ManifestHandler)
        self._http_thread = threading.Thread(
            target=self._http_server.serve_forever,
            name="ManifestWiFiServer",
            daemon=True,
        )
        self._http_thread.start()

        lan_ip = self._detect_lan_ip()
        return f"http://{lan_ip}:{port}/backed_up_manifest.json"

    def stop_fallback_wifi_manifest_server(self) -> None:
        if self._http_server is not None:
            try:
                self._http_server.shutdown()
                self._http_server.server_close()
            except Exception:
                pass
            self._http_server = None

    @staticmethod
    def _detect_lan_ip() -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
