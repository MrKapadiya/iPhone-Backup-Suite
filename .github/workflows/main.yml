# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller Cross-Architecture Spec File for macOS (universal2 / arm64 / x86_64),
Windows (x64 / ARM64), and Linux (x64 / ARM64).
Includes Pillow and pillow-heif for optional HEIC-to-JPEG conversion and live thumbnails.
"""

import sys
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in ("pymobiledevice3", "construct", "pyimg4", "qh3", "cryptography", "PIL", "pillow_heif"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

a = Analysis(
    ['../desktop_app/main.py'],
    pathex=['..'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='iPhoneBackupStudio',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='iPhoneBackupStudio',
)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='iPhoneBackupStudio.app',
        icon=None,
        bundle_identifier='com.yourorg.iPhoneBackupStudio',
    )
