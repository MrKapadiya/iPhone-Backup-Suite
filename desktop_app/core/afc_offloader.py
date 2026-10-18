"""
Smart Photo, Video, and Atomic Live Photo Offloader over Apple File Connection (`AfcService`).
Supports:
  1. Configurable Folder Organization:
     - Date Hierarchy (`YYYY/MM-Month/`, e.g. `2026/10-October/IMG_4821.HEIC`)
     - Year/Month Numeric (`YYYY/MM/`, e.g. `2026/10/IMG_4821.HEIC`)
     - Original Apple DCIM (`100APPLE/IMG_4821.HEIC`)
     - Flat Folder (`IMG_4821.HEIC`)
  2. Optional Non-Destructive HEIC-to-JPEG Conversion:
     - Preserves original `.HEIC` for SHA-256 verification and iOS PhotoKit matching
       (or optionally replaces with `.JPG` after verification)
     - Uses `pillow-heif` + `Pillow` (cross-platform) with automatic fallback to macOS `sips`
     - Preserves EXIF metadata and modification timestamps
  3. Live Thumbnail Preview Generation:
     - Emits a fast 160x160 PNG thumbnail byte buffer on each completed image so the
       PySide6 UI can render a live visual gallery strip during offload.
"""

from __future__ import annotations

import calendar
import hashlib
import io
import os
import platform
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from .database import BackupLedgerDB, MediaRecord
from .device_manager import DeviceManager, maybe_await
from .exceptions import BackupAppError, ErrorCategory, ExceptionRouter, RemediationAction


SUPPORTED_IMAGE_EXTS = {".HEIC", ".JPG", ".JPEG", ".PNG", ".DNG", ".GIF", ".TIF", ".TIFF", ".WEBP"}
SUPPORTED_VIDEO_EXTS = {".MOV", ".MP4", ".M4V"}
ALL_MEDIA_EXTS = SUPPORTED_IMAGE_EXTS | SUPPORTED_VIDEO_EXTS


class FolderStructureMode(str, Enum):
    DATE_YEAR_MONTH_NAME = "YYYY/MM-Month (e.g. 2026/10-October)"
    DATE_YEAR_MONTH_NUM = "YYYY/MM (e.g. 2026/10)"
    ORIGINAL_DCIM = "Original DCIM Folders (e.g. 100APPLE)"
    FLAT_DIRECTORY = "Flat Destination Folder"


@dataclass
class OffloadOptions:
    folder_mode: FolderStructureMode = FolderStructureMode.DATE_YEAR_MONTH_NAME
    convert_heic_to_jpeg: bool = False          # Opt-in only (not forced)
    keep_original_heic_when_converting: bool = True
    jpeg_quality: int = 92
    generate_live_thumbnails: bool = True


@dataclass
class DcimRemoteAsset:
    dcim_folder: str            # e.g. "100APPLE"
    filename: str               # e.g. "IMG_4821.HEIC"
    remote_path: str            # e.g. "/DCIM/100APPLE/IMG_4821.HEIC"
    relative_dcim_path: str     # e.g. "100APPLE/IMG_4821.HEIC"
    file_size: int
    mtime_ts: int
    companion_mov: Optional["DcimRemoteAsset"] = None


@dataclass
class OffloadProgress:
    current_index: int
    total_files: int
    current_filename: str
    bytes_transferred: int
    total_bytes: int
    skipped_already_backed_up: int
    verified_count: int
    converted_jpeg_count: int
    latest_local_path: Optional[str] = None
    thumbnail_png_bytes: Optional[bytes] = None
    is_live_photo: bool = False


@dataclass
class OffloadSummary:
    udid: str
    total_scanned: int
    newly_backed_up: int
    skipped_existing: int
    live_photos_paired: int
    converted_jpegs: int
    total_bytes_written: int
    destination_root: Path
    aborted: bool = False


class AfcMediaOffloader:
    """
    Traverses `/DCIM` over `AfcService`, pairs Live Photos (`.HEIC`/`.JPG` + `.MOV`),
    streams downloads to `.part` files with on-the-fly SHA-256 verification, optionally
    generates high-quality `.JPG` copies from `.HEIC`, and emits live thumbnail previews.
    """

    CHUNK_SIZE = 256 * 1024  # 256 KB streaming chunks

    def __init__(
        self,
        device_manager: Optional[DeviceManager] = None,
        db: Optional[BackupLedgerDB] = None,
        router: Optional[ExceptionRouter] = None,
    ):
        self.db = db or BackupLedgerDB()
        self.router = router or ExceptionRouter()
        self.device_manager = device_manager or DeviceManager(db=self.db, router=self.router)
        self._cancel_event = threading.Event()
        self._heif_registered = False

    def cancel(self) -> None:
        """Signals the active transfer loop to stop cleanly after the current chunk."""
        self._cancel_event.set()

    def _ensure_heif_registered(self) -> bool:
        if self._heif_registered:
            return True
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
            self._heif_registered = True
            return True
        except Exception:
            return False

    @staticmethod
    def resolve_destination_subfolder(
        destination_root: Path,
        asset: DcimRemoteAsset,
        folder_mode: FolderStructureMode,
    ) -> Path:
        """Determines the target folder based on the user's selected organization structure."""
        dt = datetime.fromtimestamp(asset.mtime_ts) if asset.mtime_ts > 0 else datetime.now()
        year_str = f"{dt.year:04d}"
        month_num = f"{dt.month:02d}"
        month_name = calendar.month_name[dt.month]

        if folder_mode == FolderStructureMode.DATE_YEAR_MONTH_NAME:
            return destination_root / year_str / f"{month_num}-{month_name}"
        if folder_mode == FolderStructureMode.DATE_YEAR_MONTH_NUM:
            return destination_root / year_str / month_num
        if folder_mode == FolderStructureMode.FLAT_DIRECTORY:
            return destination_root
        # Default / ORIGINAL_DCIM
        return destination_root / asset.dcim_folder

    def convert_heic_file_to_jpeg(
        self,
        heic_path: Path,
        quality: int = 92,
        mtime_ts: int = 0,
    ) -> Optional[Path]:
        """
        Converts a local `.HEIC` file to `.JPG` using `pillow-heif` + `Pillow` (with EXIF preservation)
        or falls back to macOS native `sips` if `pillow-heif` is not installed.
        """
        if heic_path.suffix.upper() != ".HEIC":
            return None

        jpeg_path = heic_path.with_suffix(".jpg")
        try:
            if self._ensure_heif_registered():
                from PIL import Image, ImageOps

                with Image.open(heic_path) as img:
                    exif_bytes = img.info.get("exif")
                    img_oriented = ImageOps.exif_transpose(img)
                    rgb_img = img_oriented.convert("RGB")
                    save_kwargs: dict[str, Any] = {"format": "JPEG", "quality": quality, "optimize": True}
                    if exif_bytes:
                        save_kwargs["exif"] = exif_bytes
                    rgb_img.save(jpeg_path, **save_kwargs)
            elif platform.system() == "Darwin" and shutil.which("sips"):
                subprocess.run(
                    [
                        "sips",
                        "-s",
                        "format",
                        "jpeg",
                        "-s",
                        "formatOptions",
                        str(quality),
                        str(heic_path),
                        "--out",
                        str(jpeg_path),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=20,
                )
            else:
                return None

            if mtime_ts > 0 and jpeg_path.is_file():
                try:
                    os.utime(jpeg_path, (mtime_ts, mtime_ts))
                except OSError:
                    pass
            return jpeg_path if jpeg_path.is_file() else None
        except Exception:
            return None

    def build_thumbnail_png_bytes(self, image_path: Path, max_dim: int = 140) -> Optional[bytes]:
        """
        Generates an in-memory 140x140 PNG thumbnail for the PySide6 Live Preview Gallery.
        Supports JPG, PNG, WEBP, and HEIC (via pillow-heif or macOS sips).
        """
        ext = image_path.suffix.upper()
        if ext not in SUPPORTED_IMAGE_EXTS:
            return None

        try:
            self._ensure_heif_registered()
            from PIL import Image, ImageOps

            with Image.open(image_path) as img:
                img = ImageOps.exif_transpose(img)
                img.thumbnail((max_dim, max_dim))
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                return buf.getvalue()
        except Exception:
            return None

    async def scan_dcim_inventory(self, afc: Any) -> list[DcimRemoteAsset]:
        """
        Scans `/DCIM` subdirectories (`100APPLE`, `101APPLE`, etc.), pairs Live Photo
        companion `.MOV` files with their parent still images, and returns a sorted asset list.
        """
        dcim_root = "/DCIM"
        try:
            folders = await maybe_await(afc.listdir(dcim_root))
        except Exception as exc:
            remediation = self.router.route(exc, context="afc_scan_dcim")
            raise BackupAppError(remediation, exc) from exc

        all_assets: list[DcimRemoteAsset] = []

        for folder in sorted(folders):
            if folder.startswith(".") or folder in (".", "..", ".MISC"):
                continue
            folder_path = f"{dcim_root}/{folder}"
            try:
                stat_folder = await maybe_await(afc.stat(folder_path))
                if stat_folder.get("st_ifmt") != "S_IFDIR":
                    continue
                entries = await maybe_await(afc.listdir(folder_path))
            except Exception:
                continue

            raw_items: dict[str, DcimRemoteAsset] = {}
            for name in sorted(entries):
                if name.startswith("."):
                    continue
                ext = Path(name).suffix.upper()
                if ext not in ALL_MEDIA_EXTS:
                    continue

                remote_path = f"{folder_path}/{name}"
                try:
                    st = await maybe_await(afc.stat(remote_path))
                except Exception:
                    continue

                if st.get("st_ifmt") != "S_IFREG":
                    continue

                size = int(st.get("st_size", 0))
                mtime_raw = st.get("st_mtime", 0)
                if isinstance(mtime_raw, datetime):
                    mtime_ts = int(mtime_raw.timestamp())
                else:
                    mtime_val = int(mtime_raw)
                    mtime_ts = mtime_val // 1_000_000_000 if mtime_val > 10_000_000_000 else mtime_val

                raw_items[name] = DcimRemoteAsset(
                    dcim_folder=folder,
                    filename=name,
                    remote_path=remote_path,
                    relative_dcim_path=f"{folder}/{name}",
                    file_size=size,
                    mtime_ts=mtime_ts,
                )

            consumed_movs: set[str] = set()
            for name, asset in raw_items.items():
                stem = Path(name).stem
                ext = Path(name).suffix.upper()
                if ext in SUPPORTED_IMAGE_EXTS:
                    for mov_candidate in (f"{stem}.MOV", f"{stem}.mov"):
                        if mov_candidate in raw_items:
                            mov_asset = raw_items[mov_candidate]
                            if mov_asset.file_size <= 35 * 1024 * 1024:
                                asset.companion_mov = mov_asset
                                consumed_movs.add(mov_candidate)
                                break

            for name, asset in raw_items.items():
                if name in consumed_movs:
                    continue
                all_assets.append(asset)

        return all_assets

    async def offload_photos(
        self,
        destination_dir: Path,
        options: Optional[OffloadOptions] = None,
        udid: Optional[str] = None,
        progress_callback: Optional[Callable[[OffloadProgress], None]] = None,
    ) -> OffloadSummary:
        """
        Performs an incremental, SHA-256-verified offload of `/DCIM` media to `destination_dir`,
        applying the chosen folder hierarchy, optional HEIC-to-JPEG conversion, and live thumbnails.
        """
        self._cancel_event.clear()
        opts = options or OffloadOptions()
        destination_dir = Path(destination_dir).expanduser().resolve()
        destination_dir.mkdir(parents=True, exist_ok=True)

        lockdown = await self.device_manager.open_lockdown_client(udid=udid, autopair=True)
        telemetry = await self.device_manager.get_device_telemetry(udid=udid)
        dev_udid = telemetry.udid

        try:
            from pymobiledevice3.services.afc import AfcService
            afc = AfcService(lockdown=lockdown)
        except Exception as exc:
            remediation = self.router.route(exc, context="afc_init")
            raise BackupAppError(remediation, exc) from exc

        async with self.device_manager.prevent_device_sleep(lockdown):
            assets = await self.scan_dcim_inventory(afc)

            pending_assets: list[DcimRemoteAsset] = []
            skipped_count = 0
            total_bytes_needed = 0

            for asset in assets:
                primary_ok = self.db.is_media_already_verified(
                    dev_udid, asset.relative_dcim_path, asset.file_size
                )
                companion_ok = True
                if asset.companion_mov is not None:
                    companion_ok = self.db.is_media_already_verified(
                        dev_udid, asset.companion_mov.relative_dcim_path, asset.companion_mov.file_size
                    )

                if primary_ok and companion_ok:
                    skipped_count += 1
                else:
                    pending_assets.append(asset)
                    total_bytes_needed += asset.file_size
                    if asset.companion_mov:
                        total_bytes_needed += asset.companion_mov.file_size

            # Add 25% headroom if keeping both HEIC and converted JPEG
            space_multiplier = 1.25 if opts.convert_heic_to_jpeg else 1.0
            required_host_bytes = int(total_bytes_needed * space_multiplier) + (250 * 1024 * 1024)
            free_host_bytes = shutil.disk_usage(destination_dir).free
            if required_host_bytes > free_host_bytes:
                raise BackupAppError(
                    RemediationAction(
                        category=ErrorCategory.STORAGE_EXHAUSTED,
                        title="Insufficient Host Disk Space",
                        user_message=(
                            f"Need {(required_host_bytes / 1e9):.2f} GB for new photos, "
                            f"but only {(free_host_bytes / 1e9):.2f} GB is available on {destination_dir}."
                        ),
                        steps=[
                            "Select an external drive or free up space on the target disk.",
                            "Re-run the offload once space is available.",
                        ],
                    )
                )

            bytes_written = 0
            newly_backed_up = 0
            live_photos_paired = 0
            converted_jpegs = 0
            total_to_process = len(pending_assets)

            for idx, asset in enumerate(pending_assets, start=1):
                if self._cancel_event.is_set():
                    return OffloadSummary(
                        udid=dev_udid,
                        total_scanned=len(assets),
                        newly_backed_up=newly_backed_up,
                        skipped_existing=skipped_count,
                        live_photos_paired=live_photos_paired,
                        converted_jpegs=converted_jpegs,
                        total_bytes_written=bytes_written,
                        destination_root=destination_dir,
                        aborted=True,
                    )

                target_subfolder = self.resolve_destination_subfolder(
                    destination_dir, asset, opts.folder_mode
                )
                primary_dest = self._resolve_collision_safe_path(
                    target_subfolder, asset.dcim_folder, asset.filename, opts.folder_mode
                )

                primary_sha256 = await self._download_and_verify_single_file(
                    afc=afc,
                    remote_path=asset.remote_path,
                    expected_size=asset.file_size,
                    dest_path=primary_dest,
                    mtime_ts=asset.mtime_ts,
                )
                bytes_written += asset.file_size

                # Optional HEIC -> JPEG conversion (only when user enables it)
                converted_jpeg_str: Optional[str] = None
                preview_source_path = primary_dest
                if opts.convert_heic_to_jpeg and primary_dest.suffix.upper() == ".HEIC":
                    jpeg_path = self.convert_heic_file_to_jpeg(
                        primary_dest, quality=opts.jpeg_quality, mtime_ts=asset.mtime_ts
                    )
                    if jpeg_path is not None:
                        converted_jpegs += 1
                        converted_jpeg_str = str(jpeg_path)
                        preview_source_path = jpeg_path

                # Download Live Photo companion MOV into the exact same folder
                companion_dest_str: Optional[str] = None
                if asset.companion_mov is not None:
                    comp = asset.companion_mov
                    comp_dest = self._resolve_collision_safe_path(
                        target_subfolder, comp.dcim_folder, comp.filename, opts.folder_mode
                    )
                    comp_sha256 = await self._download_and_verify_single_file(
                        afc=afc,
                        remote_path=comp.remote_path,
                        expected_size=comp.file_size,
                        dest_path=comp_dest,
                        mtime_ts=comp.mtime_ts,
                    )
                    bytes_written += comp.file_size
                    companion_dest_str = str(comp_dest)
                    live_photos_paired += 1

                    self.db.record_verified_media(
                        MediaRecord(
                            udid=dev_udid,
                            relative_dcim_path=comp.relative_dcim_path,
                            dcim_folder=comp.dcim_folder,
                            filename=comp.filename,
                            file_size=comp.file_size,
                            device_mtime_ts=comp.mtime_ts,
                            sha256_hash=comp_sha256,
                            local_backup_path=str(comp_dest),
                            is_live_photo_paired=True,
                            companion_mov_path=None,
                            converted_jpeg_path=None,
                            verified_on_disk=True,
                            backed_up_at_utc=datetime.now(timezone.utc).isoformat(),
                        )
                    )

                self.db.record_verified_media(
                    MediaRecord(
                        udid=dev_udid,
                        relative_dcim_path=asset.relative_dcim_path,
                        dcim_folder=asset.dcim_folder,
                        filename=asset.filename,
                        file_size=asset.file_size,
                        device_mtime_ts=asset.mtime_ts,
                        sha256_hash=primary_sha256,
                        local_backup_path=str(primary_dest),
                        is_live_photo_paired=asset.companion_mov is not None,
                        companion_mov_path=companion_dest_str,
                        converted_jpeg_path=converted_jpeg_str,
                        verified_on_disk=True,
                        backed_up_at_utc=datetime.now(timezone.utc).isoformat(),
                    )
                )
                newly_backed_up += 1

                thumb_bytes: Optional[bytes] = None
                if opts.generate_live_thumbnails:
                    thumb_bytes = self.build_thumbnail_png_bytes(preview_source_path)

                if progress_callback:
                    progress_callback(
                        OffloadProgress(
                            current_index=idx,
                            total_files=total_to_process,
                            current_filename=asset.filename,
                            bytes_transferred=bytes_written,
                            total_bytes=total_bytes_needed,
                            skipped_already_backed_up=skipped_count,
                            verified_count=newly_backed_up,
                            converted_jpeg_count=converted_jpegs,
                            latest_local_path=str(preview_source_path),
                            thumbnail_png_bytes=thumb_bytes,
                            is_live_photo=asset.companion_mov is not None,
                        )
                    )

            return OffloadSummary(
                udid=dev_udid,
                total_scanned=len(assets),
                newly_backed_up=newly_backed_up,
                skipped_existing=skipped_count,
                live_photos_paired=live_photos_paired,
                converted_jpegs=converted_jpegs,
                total_bytes_written=bytes_written,
                destination_root=destination_dir,
                aborted=False,
            )

    @staticmethod
    def _resolve_collision_safe_path(
        target_subfolder: Path,
        dcim_folder: str,
        filename: str,
        folder_mode: FolderStructureMode,
    ) -> Path:
        """
        Ensures that if two different DCIM folders (e.g. `100APPLE` and `109APPLE`) contain
        an identically named `IMG_0001.HEIC` taken in the same month, they never overwrite each other.
        """
        candidate = target_subfolder / filename
        if folder_mode == FolderStructureMode.ORIGINAL_DCIM or not candidate.exists():
            return candidate
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        return target_subfolder / f"{stem}_{dcim_folder}{suffix}"

    async def _download_and_verify_single_file(
        self,
        afc: Any,
        remote_path: str,
        expected_size: int,
        dest_path: Path,
        mtime_ts: int,
    ) -> str:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        part_path = dest_path.with_suffix(dest_path.suffix + ".part")
        hasher = hashlib.sha256()
        handle = None

        try:
            handle = await maybe_await(afc.fopen(remote_path, "rb"))
            bytes_read = 0
            with open(part_path, "wb") as out_f:
                while bytes_read < expected_size:
                    if self._cancel_event.is_set():
                        raise InterruptedError("Transfer cancelled by user.")
                    to_read = min(self.CHUNK_SIZE, expected_size - bytes_read)
                    chunk = await maybe_await(afc.fread(handle, to_read))
                    if not chunk:
                        break
                    out_f.write(chunk)
                    hasher.update(chunk)
                    bytes_read += len(chunk)

                out_f.flush()
                os.fsync(out_f.fileno())

            if bytes_read != expected_size:
                raise IOError(
                    f"Incomplete AFC read for {remote_path}: expected {expected_size} bytes, got {bytes_read}"
                )

            part_path.replace(dest_path)
            if mtime_ts > 0:
                try:
                    os.utime(dest_path, (mtime_ts, mtime_ts))
                except OSError:
                    pass

            return hasher.hexdigest()

        except Exception as exc:
            if part_path.exists():
                try:
                    part_path.unlink()
                except OSError:
                    pass
            remediation = self.router.route(exc, context="afc_file_download")
            raise BackupAppError(remediation, exc) from exc
        finally:
            if handle is not None:
                try:
                    await maybe_await(afc.fclose(handle))
                except Exception:
                    pass
