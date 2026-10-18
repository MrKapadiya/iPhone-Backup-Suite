"""
PySide6 Main Window v2 with:
  - Configurable Folder Hierarchy Selector (`YYYY/MM-Month`, `YYYY/MM`, `Original DCIM`, `Flat`)
  - Opt-in (non-forced) HEIC-to-JPEG Conversion with Adjustable Quality Slider (50% - 100%)
  - Live Photo & Image Thumbnail Preview Strip (`QListWidget` in IconMode)
  - Interactive OS-Aware Remediation Modals
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from desktop_app.core import (
    BackupLedgerDB,
    BackupProgress,
    CompanionManifestSync,
    ConnectedDeviceInfo,
    DeviceManager,
    ExceptionRouter,
    FolderStructureMode,
    OffloadOptions,
    OffloadProgress,
    OffloadSummary,
    RemediationAction,
)
from .workers import (
    DeviceWatcherWorker,
    ManifestPushWorker,
    MediaOffloadWorker,
    SystemBackupWorker,
)

DARK_STYLESHEET = """
QMainWindow, QDialog {
    background-color: #0f1117;
    color: #e2e8f0;
}
QWidget {
    color: #e2e8f0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 13px;
}
QFrame#Card {
    background-color: #181b24;
    border: 1px solid #2a2f3d;
    border-radius: 10px;
    padding: 12px;
}
QLabel#HeaderTitle {
    font-size: 18px;
    font-weight: 700;
    color: #ffffff;
}
QLabel#BadgeConnected {
    background-color: rgba(16, 185, 129, 0.18);
    color: #34d399;
    border: 1px solid #059669;
    border-radius: 12px;
    padding: 4px 12px;
    font-weight: 700;
}
QLabel#BadgeDisconnected {
    background-color: rgba(239, 68, 68, 0.18);
    color: #f87171;
    border: 1px solid #b91c1c;
    border-radius: 12px;
    padding: 4px 12px;
    font-weight: 700;
}
QPushButton {
    background-color: #262b38;
    border: 1px solid #3b4254;
    border-radius: 7px;
    padding: 8px 16px;
    color: #f1f5f9;
    font-weight: 600;
}
QPushButton:hover {
    border-color: #38bdf8;
    background-color: #2e3545;
}
QPushButton#PrimaryButton {
    background-color: #0284c7;
    border: 1px solid #38bdf8;
    color: #ffffff;
    font-weight: 700;
    padding: 10px 18px;
}
QPushButton#PrimaryButton:hover {
    background-color: #0369a1;
}
QPushButton#AccentButton {
    background-color: #d97706;
    border: 1px solid #f59e0b;
    color: #ffffff;
    font-weight: 700;
    padding: 10px 18px;
}
QLineEdit, QPlainTextEdit, QComboBox, QListWidget {
    background-color: #0f1117;
    border: 1px solid #2a2f3d;
    border-radius: 6px;
    padding: 6px 10px;
    color: #e2e8f0;
}
QProgressBar {
    background-color: #0f1117;
    border: 1px solid #2a2f3d;
    border-radius: 6px;
    text-align: center;
    height: 22px;
    font-weight: 600;
}
QProgressBar::chunk {
    background-color: #0284c7;
    border-radius: 5px;
}
QTabWidget::pane {
    border: 1px solid #2a2f3d;
    border-radius: 8px;
    background: #141720;
}
QTabBar::tab {
    background: #181b24;
    border: 1px solid #2a2f3d;
    padding: 9px 18px;
    margin-right: 4px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    color: #94a3b8;
    font-weight: 600;
}
QTabBar::tab:selected {
    background: #0284c7;
    color: #ffffff;
    border-color: #38bdf8;
}
"""


class RemediationDialog(QDialog):
    """
    Interactive modal displaying OS-aware step-by-step recovery guidance
    for pairing, locked device, missing usbmuxd/AMDS driver, or disconnected cables.
    """

    def __init__(self, action: RemediationAction, parent=None):
        super().__init__(parent)
        self.action = action
        self.setWindowTitle(action.title)
        self.setMinimumWidth(520)
        self.setStyleSheet(DARK_STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title_lbl = QLabel(f"⚠️  {action.title}")
        title_lbl.setObjectName("HeaderTitle")
        layout.addWidget(title_lbl)

        msg_lbl = QLabel(action.user_message)
        msg_lbl.setWordWrap(True)
        layout.addWidget(msg_lbl)

        if action.steps:
            steps_card = QFrame()
            steps_card.setObjectName("Card")
            steps_layout = QVBoxLayout(steps_card)
            steps_header = QLabel("Recommended Recovery Steps:")
            steps_header.setStyleSheet("font-weight: 700; color: #38bdf8;")
            steps_layout.addWidget(steps_header)
            for step in action.steps:
                s_lbl = QLabel(f"• {step}")
                s_lbl.setWordWrap(True)
                steps_layout.addWidget(s_lbl)
            layout.addWidget(steps_card)

        if action.technical_details:
            tech_box = QPlainTextEdit()
            tech_box.setReadOnly(True)
            tech_box.setPlainText(action.technical_details)
            tech_box.setMaximumHeight(110)
            layout.addWidget(tech_box)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        if action.can_auto_retry:
            retry_btn = QPushButton("Retry Now")
            retry_btn.setObjectName("PrimaryButton")
            retry_btn.clicked.connect(self.accept)
            btn_row.addWidget(retry_btn)

        close_btn = QPushButton("Dismiss")
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("iPhone A-to-Z Backup & Photo Offload Studio v2 (pymobiledevice3)")
        self.resize(1100, 840)
        self.setStyleSheet(DARK_STYLESHEET)

        self.db = BackupLedgerDB()
        self.router = ExceptionRouter()
        self.device_manager = DeviceManager(db=self.db, router=self.router)
        self.manifest_sync = CompanionManifestSync(db=self.db, router=self.router)

        self.current_device: Optional[ConnectedDeviceInfo] = None
        self.offload_worker: Optional[MediaOffloadWorker] = None
        self.backup_worker: Optional[SystemBackupWorker] = None
        self.manifest_worker: Optional[ManifestPushWorker] = None
        self._offload_start_ts: float = 0.0
        self._last_remediation_title: str = ""

        self._build_ui()
        self._start_device_watcher()
        QTimer.singleShot(300, self._run_preflight_diagnostics)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(18, 16, 18, 16)
        root_layout.setSpacing(12)

        # 1. Top Device Telemetry Header Card
        header_card = QFrame()
        header_card.setObjectName("Card")
        header_layout = QVBoxLayout(header_card)

        top_row = QHBoxLayout()
        self.lbl_device_title = QLabel("No iPhone Connected")
        self.lbl_device_title.setObjectName("HeaderTitle")
        top_row.addWidget(self.lbl_device_title)
        top_row.addStretch()

        self.lbl_conn_badge = QLabel("Waiting for USB / Wi-Fi...")
        self.lbl_conn_badge.setObjectName("BadgeDisconnected")
        top_row.addWidget(self.lbl_conn_badge)
        header_layout.addLayout(top_row)

        meta_row = QHBoxLayout()
        self.lbl_device_meta = QLabel("Plug in your iPhone and tap 'Trust This Computer' when prompted.")
        self.lbl_device_meta.setStyleSheet("color: #94a3b8;")
        meta_row.addWidget(self.lbl_device_meta)
        meta_row.addStretch()
        self.lbl_battery = QLabel("Battery: --")
        meta_row.addWidget(self.lbl_battery)
        header_layout.addLayout(meta_row)

        self.bar_storage = QProgressBar()
        self.bar_storage.setRange(0, 100)
        self.bar_storage.setValue(0)
        self.bar_storage.setFormat("Device Storage: --")
        header_layout.addWidget(self.bar_storage)

        root_layout.addWidget(header_card)

        # 2. Main Tab Widget
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_photo_offload_tab(), "1. Smart Photo Offload & Companion Sync")
        self.tabs.addTab(self._build_system_backup_tab(), "2. Full System Backup (Mobilebackup2)")
        self.tabs.addTab(self._build_diagnostics_tab(), "3. Diagnostics & Exception Console")
        root_layout.addWidget(self.tabs, stretch=1)

    def _build_photo_offload_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        # Card 1: Destination & Organization Options
        dest_card = QFrame()
        dest_card.setObjectName("Card")
        dest_layout = QVBoxLayout(dest_card)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Destination Folder:"))
        default_photos_dir = str(Path.home() / "Pictures" / "iPhone_Verified_Backup")
        self.input_photo_dest = QLineEdit(default_photos_dir)
        path_row.addWidget(self.input_photo_dest, stretch=1)
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self._browse_photo_dest)
        path_row.addWidget(btn_browse)
        dest_layout.addLayout(path_row)

        # Folder Structure & Optional HEIC->JPEG Row
        opts_row = QHBoxLayout()
        opts_row.addWidget(QLabel("Folder Hierarchy:"))
        self.combo_folder_mode = QComboBox()
        for mode in FolderStructureMode:
            self.combo_folder_mode.addItem(mode.value, mode)
        self.combo_folder_mode.setCurrentIndex(0)  # Default: YYYY/MM-Month
        opts_row.addWidget(self.combo_folder_mode, stretch=1)

        opts_row.addSpacing(16)
        self.chk_convert_heic = QCheckBox("Also export JPEG copy for .HEIC photos (Optional)")
        self.chk_convert_heic.setChecked(False)  # Not forced; opt-in by user
        self.chk_convert_heic.toggled.connect(self._on_heic_toggle)
        opts_row.addWidget(self.chk_convert_heic)

        self.lbl_jpeg_quality = QLabel("JPEG Quality: 92%")
        self.lbl_jpeg_quality.setEnabled(False)
        opts_row.addWidget(self.lbl_jpeg_quality)

        self.slider_jpeg_quality = QSlider(Qt.Horizontal)
        self.slider_jpeg_quality.setRange(60, 100)
        self.slider_jpeg_quality.setValue(92)
        self.slider_jpeg_quality.setFixedWidth(100)
        self.slider_jpeg_quality.setEnabled(False)
        self.slider_jpeg_quality.valueChanged.connect(
            lambda val: self.lbl_jpeg_quality.setText(f"JPEG Quality: {val}%")
        )
        opts_row.addWidget(self.slider_jpeg_quality)

        dest_layout.addLayout(opts_row)
        layout.addWidget(dest_card)

        # Card 2: Live Progress & Visual Thumbnail Gallery
        prog_card = QFrame()
        prog_card.setObjectName("Card")
        prog_layout = QVBoxLayout(prog_card)

        self.lbl_offload_status = QLabel("Ready to scan /DCIM.")
        prog_layout.addWidget(self.lbl_offload_status)

        self.bar_offload = QProgressBar()
        self.bar_offload.setRange(0, 100)
        self.bar_offload.setValue(0)
        prog_layout.addWidget(self.bar_offload)

        self.lbl_offload_metrics = QLabel(
            "Speed: -- MB/s  |  Verified: 0  |  JPEGs Exported: 0  |  Skipped (Already Backed Up): 0"
        )
        self.lbl_offload_metrics.setStyleSheet("color: #94a3b8;")
        prog_layout.addWidget(self.lbl_offload_metrics)

        # Live Photo Thumbnail Preview Strip
        thumb_header_row = QHBoxLayout()
        thumb_header_row.addWidget(QLabel("Live Offload Thumbnail Preview:"))
        self.chk_live_thumbs = QCheckBox("Render Live Thumbnails")
        self.chk_live_thumbs.setChecked(True)
        thumb_header_row.addStretch()
        thumb_header_row.addWidget(self.chk_live_thumbs)
        prog_layout.addLayout(thumb_header_row)

        self.thumb_gallery = QListWidget()
        self.thumb_gallery.setViewMode(QListWidget.IconMode)
        self.thumb_gallery.setIconSize(QSize(96, 96))
        self.thumb_gallery.setResizeMode(QListWidget.Adjust)
        self.thumb_gallery.setMovement(QListWidget.Static)
        self.thumb_gallery.setSpacing(8)
        self.thumb_gallery.setFixedHeight(135)
        prog_layout.addWidget(self.thumb_gallery)

        action_row = QHBoxLayout()
        self.btn_start_offload = QPushButton("Start Incremental Photo Offload")
        self.btn_start_offload.setObjectName("PrimaryButton")
        self.btn_start_offload.clicked.connect(self._start_photo_offload)
        action_row.addWidget(self.btn_start_offload)

        self.btn_cancel_offload = QPushButton("Pause / Cancel")
        self.btn_cancel_offload.setEnabled(False)
        self.btn_cancel_offload.clicked.connect(self._cancel_photo_offload)
        action_row.addWidget(self.btn_cancel_offload)
        prog_layout.addLayout(action_row)
        layout.addWidget(prog_card)

        # Card 3: Companion App Sync Card
        comp_card = QFrame()
        comp_card.setObjectName("Card")
        comp_layout = QVBoxLayout(comp_card)
        comp_title = QLabel("iOS Companion App — 'Backed Up' Album & Storage Cleaner")
        comp_title.setStyleSheet("font-weight: 700; color: #f59e0b;")
        comp_layout.addWidget(comp_title)

        bundle_row = QHBoxLayout()
        bundle_row.addWidget(QLabel("Companion App Bundle ID:"))
        self.input_bundle_id = QLineEdit(CompanionManifestSync.DEFAULT_BUNDLE_ID)
        bundle_row.addWidget(self.input_bundle_id, stretch=1)
        comp_layout.addLayout(bundle_row)

        sync_btns = QHBoxLayout()
        self.btn_push_usb = QPushButton("Push Verified Manifest to iPhone via USB (HouseArrest)")
        self.btn_push_usb.setObjectName("AccentButton")
        self.btn_push_usb.clicked.connect(self._push_manifest_usb)
        sync_btns.addWidget(self.btn_push_usb)

        self.btn_wifi_server = QPushButton("Start Fallback Local Wi-Fi Manifest Server")
        self.btn_wifi_server.clicked.connect(self._toggle_wifi_manifest_server)
        sync_btns.addWidget(self.btn_wifi_server)
        comp_layout.addLayout(sync_btns)

        self.lbl_manifest_status = QLabel(
            "Pushes 'backed_up_manifest.json' directly into the BackupCompanion app's /Documents folder."
        )
        self.lbl_manifest_status.setStyleSheet("color: #94a3b8;")
        comp_layout.addWidget(self.lbl_manifest_status)
        layout.addWidget(comp_card)

        return page

    def _build_system_backup_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        card = QFrame()
        card.setObjectName("Card")
        c_layout = QVBoxLayout(card)
        c_layout.addWidget(QLabel("System Backup Destination Directory (Mobilebackup2):"))

        row = QHBoxLayout()
        default_sys_dir = str(Path.home() / "Backups" / "iPhone_System_Backups")
        self.input_sys_dest = QLineEdit(default_sys_dir)
        row.addWidget(self.input_sys_dest, stretch=1)
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self._browse_sys_dest)
        row.addWidget(btn_browse)
        c_layout.addLayout(row)

        self.chk_full_backup = QCheckBox("Force Full Backup (Uncheck for fast Incremental Backup)")
        c_layout.addWidget(self.chk_full_backup)

        self.bar_sys_backup = QProgressBar()
        self.bar_sys_backup.setRange(0, 100)
        self.bar_sys_backup.setValue(0)
        c_layout.addWidget(self.bar_sys_backup)

        self.lbl_sys_status = QLabel("Idle.")
        c_layout.addWidget(self.lbl_sys_status)

        self.btn_run_sys_backup = QPushButton("Run iOS System Backup")
        self.btn_run_sys_backup.setObjectName("PrimaryButton")
        self.btn_run_sys_backup.clicked.connect(self._start_system_backup)
        c_layout.addWidget(self.btn_run_sys_backup)

        layout.addWidget(card)
        layout.addStretch()
        return page

    def _build_diagnostics_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        top_bar = QHBoxLayout()
        btn_preflight = QPushButton("Run Environment Preflight Check")
        btn_preflight.setObjectName("PrimaryButton")
        btn_preflight.clicked.connect(self._run_preflight_diagnostics)
        top_bar.addWidget(btn_preflight)

        btn_clear = QPushButton("Clear Console")
        btn_clear.clicked.connect(lambda: self.console_log.clear())
        top_bar.addWidget(btn_clear)
        top_bar.addStretch()
        layout.addLayout(top_bar)

        self.console_log = QPlainTextEdit()
        self.console_log.setReadOnly(True)
        self.console_log.setFont(QFont("Menlo", 11))
        layout.addWidget(self.console_log, stretch=1)
        return page

    def _on_heic_toggle(self, checked: bool) -> None:
        self.lbl_jpeg_quality.setEnabled(checked)
        self.slider_jpeg_quality.setEnabled(checked)

    def _start_device_watcher(self) -> None:
        self.watcher = DeviceWatcherWorker(db=self.db, router=self.router, parent=self)
        self.watcher.device_connected.connect(self._on_device_connected)
        self.watcher.device_disconnected.connect(self._on_device_disconnected)
        self.watcher.remediation_needed.connect(self._on_watcher_remediation)
        self.watcher.log_emitted.connect(self._append_log)
        self.watcher.start()

    def _on_device_connected(self, info: ConnectedDeviceInfo) -> None:
        self.current_device = info
        self._last_remediation_title = ""
        self.lbl_device_title.setText(f"📱 {info.device_name} ({info.product_type})")
        self.lbl_conn_badge.setText(f"Connected via {info.connection_type}")
        self.lbl_conn_badge.setObjectName("BadgeConnected")
        self.lbl_conn_badge.style().unpolish(self.lbl_conn_badge)
        self.lbl_conn_badge.style().polish(self.lbl_conn_badge)

        enc_str = "Encrypted Backup ON" if info.backup_will_encrypt else "Unencrypted Backup"
        self.lbl_device_meta.setText(
            f"iOS {info.ios_version} ({info.build_version})  •  UDID: {info.udid[:12]}…  •  {enc_str}"
        )
        charging_icon = "⚡ Charging" if info.is_charging else "🔋"
        self.lbl_battery.setText(f"{charging_icon} {info.battery_level}%")

        if info.total_disk_bytes > 0:
            pct = int((info.used_disk_bytes / info.total_disk_bytes) * 100)
            used_gb = info.used_disk_bytes / 1e9
            total_gb = info.total_disk_bytes / 1e9
            free_gb = info.free_disk_bytes / 1e9
            self.bar_storage.setValue(pct)
            self.bar_storage.setFormat(
                f"Used: {used_gb:.1f} GB / {total_gb:.1f} GB ({free_gb:.1f} GB Free)"
            )

    def _on_device_disconnected(self) -> None:
        self.current_device = None
        self.lbl_device_title.setText("No iPhone Connected")
        self.lbl_conn_badge.setText("Disconnected")
        self.lbl_conn_badge.setObjectName("BadgeDisconnected")
        self.lbl_conn_badge.style().unpolish(self.lbl_conn_badge)
        self.lbl_conn_badge.style().polish(self.lbl_conn_badge)
        self.lbl_device_meta.setText("Plug in your iPhone via USB and unlock the screen.")
        self.lbl_battery.setText("Battery: --")
        self.bar_storage.setValue(0)
        self.bar_storage.setFormat("Device Storage: --")

    def _on_watcher_remediation(self, action: RemediationAction) -> None:
        if action.title == self._last_remediation_title:
            return
        self._last_remediation_title = action.title
        self._append_log("WARN", f"{action.title}: {action.user_message}")
        if action.category.value in ("PAIRING_AND_TRUST", "DEVICE_LOCKED"):
            dlg = RemediationDialog(action, self)
            dlg.exec()

    def _show_remediation_modal(self, action: RemediationAction) -> None:
        dlg = RemediationDialog(action, self)
        dlg.exec()

    def _browse_photo_dest(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select Photo Offload Destination")
        if folder:
            self.input_photo_dest.setText(folder)

    def _browse_sys_dest(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select System Backup Directory")
        if folder:
            self.input_sys_dest.setText(folder)

    def _start_photo_offload(self) -> None:
        dest = Path(self.input_photo_dest.text().strip())
        udid = self.current_device.udid if self.current_device else None
        self._offload_start_ts = time.time()

        selected_mode = self.combo_folder_mode.currentData() or FolderStructureMode.DATE_YEAR_MONTH_NAME
        options = OffloadOptions(
            folder_mode=selected_mode,
            convert_heic_to_jpeg=self.chk_convert_heic.isChecked(),
            keep_original_heic_when_converting=True,
            jpeg_quality=self.slider_jpeg_quality.value(),
            generate_live_thumbnails=self.chk_live_thumbs.isChecked(),
        )

        self.btn_start_offload.setEnabled(False)
        self.btn_cancel_offload.setEnabled(True)
        self.bar_offload.setValue(0)
        self.lbl_offload_status.setText("Scanning /DCIM and verifying existing ledger...")

        self.offload_worker = MediaOffloadWorker(
            destination_dir=dest,
            options=options,
            udid=udid,
            db=self.db,
            router=self.router,
            parent=self,
        )
        self.offload_worker.progress_updated.connect(self._on_offload_progress)
        self.offload_worker.offload_completed.connect(self._on_offload_completed)
        self.offload_worker.remediation_needed.connect(self._on_offload_error)
        self.offload_worker.log_emitted.connect(self._append_log)
        self.offload_worker.start()

    def _cancel_photo_offload(self) -> None:
        if self.offload_worker:
            self.offload_worker.cancel()
            self.lbl_offload_status.setText("Stopping after current file completes...")

    def _on_offload_progress(self, prog: OffloadProgress) -> None:
        pct = int((prog.bytes_transferred / prog.total_bytes) * 100) if prog.total_bytes > 0 else 100
        self.bar_offload.setValue(pct)
        live_tag = " [Live Photo]" if prog.is_live_photo else ""
        self.lbl_offload_status.setText(
            f"Offloading [{prog.current_index}/{prog.total_files}]: {prog.current_filename}{live_tag}"
        )
        elapsed = max(0.1, time.time() - self._offload_start_ts)
        mb_per_sec = (prog.bytes_transferred / (1024 * 1024)) / elapsed
        self.lbl_offload_metrics.setText(
            f"Speed: {mb_per_sec:.1f} MB/s  |  Verified: {prog.verified_count}  |  "
            f"JPEGs Exported: {prog.converted_jpeg_count}  |  "
            f"Skipped (Already Backed Up): {prog.skipped_already_backed_up}"
        )

        # Render live thumbnail card if PNG bytes were emitted
        if prog.thumbnail_png_bytes:
            pixmap = QPixmap()
            if pixmap.loadFromData(prog.thumbnail_png_bytes, "PNG"):
                badge_label = f"◎ {prog.current_filename}" if prog.is_live_photo else prog.current_filename
                item = QListWidgetItem(QIcon(pixmap), badge_label)
                if prog.latest_local_path:
                    item.setToolTip(prog.latest_local_path)
                self.thumb_gallery.insertItem(0, item)
                # Keep gallery lightweight (cap at 60 recent thumbnails in RAM)
                while self.thumb_gallery.count() > 60:
                    self.thumb_gallery.takeItem(self.thumb_gallery.count() - 1)

    def _on_offload_completed(self, summary: OffloadSummary) -> None:
        self.btn_start_offload.setEnabled(True)
        self.btn_cancel_offload.setEnabled(False)
        self.bar_offload.setValue(100 if not summary.aborted else self.bar_offload.value())
        state_word = "Paused" if summary.aborted else "Complete"
        self.lbl_offload_status.setText(
            f"Offload {state_word}: {summary.newly_backed_up} newly verified, "
            f"{summary.skipped_existing} already backed up, "
            f"{summary.live_photos_paired} Live Photos, "
            f"{summary.converted_jpegs} JPEGs exported."
        )

    def _on_offload_error(self, action: RemediationAction) -> None:
        self.btn_start_offload.setEnabled(True)
        self.btn_cancel_offload.setEnabled(False)
        self._show_remediation_modal(action)

    def _push_manifest_usb(self) -> None:
        udid = self.current_device.udid if self.current_device else None
        bundle_id = self.input_bundle_id.text().strip() or CompanionManifestSync.DEFAULT_BUNDLE_ID
        self.btn_push_usb.setEnabled(False)
        self.manifest_worker = ManifestPushWorker(
            bundle_id=bundle_id,
            udid=udid,
            db=self.db,
            router=self.router,
            parent=self,
        )
        self.manifest_worker.push_succeeded.connect(self._on_manifest_pushed)
        self.manifest_worker.remediation_needed.connect(self._on_manifest_error)
        self.manifest_worker.log_emitted.connect(self._append_log)
        self.manifest_worker.start()

    def _on_manifest_pushed(self, payload: dict) -> None:
        self.btn_push_usb.setEnabled(True)
        count = payload.get("total_verified_assets", 0)
        gb = payload.get("total_verified_bytes", 0) / 1e9
        self.lbl_manifest_status.setText(
            f"✅ Synced {count} verified photos ({gb:.2f} GB) to iPhone Companion App! "
            f"Open 'BackupCompanion' on your iPhone to add them to the 'Backed Up' album and delete."
        )
        QMessageBox.information(
            self,
            "Manifest Synced to iPhone",
            f"Pushed {count} verified asset records ({gb:.2f} GB) to BackupCompanion.\n\n"
            "Open the companion app on your iPhone to populate the 'Backed Up' album and free up space.",
        )

    def _on_manifest_error(self, action: RemediationAction) -> None:
        self.btn_push_usb.setEnabled(True)
        self._show_remediation_modal(action)

    def _toggle_wifi_manifest_server(self) -> None:
        udid = self.current_device.udid if self.current_device else "unknown"
        url = self.manifest_sync.start_fallback_wifi_manifest_server(udid=udid, port=8765)
        self.lbl_manifest_status.setText(f"📡 Local Wi-Fi Manifest URL active: {url}")
        self._append_log("INFO", f"Started local Wi-Fi manifest server at {url}")

    def _start_system_backup(self) -> None:
        dest = Path(self.input_sys_dest.text().strip())
        udid = self.current_device.udid if self.current_device else None
        full = self.chk_full_backup.isChecked()
        self.btn_run_sys_backup.setEnabled(False)
        self.bar_sys_backup.setValue(0)

        self.backup_worker = SystemBackupWorker(
            backup_dir=dest,
            full_backup=full,
            udid=udid,
            db=self.db,
            router=self.router,
            parent=self,
        )
        self.backup_worker.progress_updated.connect(self._on_sys_backup_progress)
        self.backup_worker.backup_completed.connect(self._on_sys_backup_completed)
        self.backup_worker.remediation_needed.connect(self._on_sys_backup_error)
        self.backup_worker.log_emitted.connect(self._append_log)
        self.backup_worker.start()

    def _on_sys_backup_progress(self, prog: BackupProgress) -> None:
        self.bar_sys_backup.setValue(int(prog.percentage))
        self.lbl_sys_status.setText(prog.status_message)

    def _on_sys_backup_completed(self, final_path: str) -> None:
        self.btn_run_sys_backup.setEnabled(True)
        self.bar_sys_backup.setValue(100)
        self.lbl_sys_status.setText(f"✅ System Backup Completed: {final_path}")

    def _on_sys_backup_error(self, action: RemediationAction) -> None:
        self.btn_run_sys_backup.setEnabled(True)
        self._show_remediation_modal(action)

    def _run_preflight_diagnostics(self) -> None:
        report = self.device_manager.run_host_preflight_check()
        self._append_log("INFO", "=== Host Environment Preflight Report ===")
        for line in report.details:
            self._append_log("INFO", f"  {line}")

    def _append_log(self, level: str, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.console_log.appendPlainText(f"[{ts}] [{level}] {message}")

    def closeEvent(self, event) -> None:
        if hasattr(self, "watcher") and self.watcher:
            self.watcher.stop()
            self.watcher.wait(1500)
        self.manifest_sync.stop_fallback_wifi_manifest_server()
        super().closeEvent(event)
