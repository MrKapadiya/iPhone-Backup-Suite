"""
Entry point for the Cross-Platform iPhone A-to-Z Backup & Media Offload Suite.
Compatible with macOS (Intel & Apple Silicon), Windows (x64 & ARM64), and Linux (x64 & ARM64).
"""

from __future__ import annotations

import sys
from PySide6.QtWidgets import QApplication

from desktop_app.core import setup_logging
from desktop_app.ui import MainWindow


def main() -> int:
    logger = setup_logging()
    logger.info("Initializing iPhone A-to-Z Backup Studio...")

    app = QApplication(sys.argv)
    app.setApplicationName("iPhoneBackupStudio")
    app.setOrganizationName("BackupSuite")

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
