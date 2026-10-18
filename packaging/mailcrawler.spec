# -*- coding: utf-8 -*-
"""PyInstaller 打包配置：macOS .app / Windows .exe 通用。

用法（在项目根目录）：
    pyinstaller packaging/mailcrawler.spec
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent
ASSETS = ROOT / "assets"

hidden = []
hidden += collect_submodules("keyring.backends")   # keyring 的后端是动态加载的
hidden += collect_submodules("webview")            # pywebview 平台后端

a = Analysis(
    [str(ROOT / "mailcrawler" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / "mailcrawler" / "static"), "mailcrawler/static")],
    hiddenimports=hidden,
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
    name="MailCrawler",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=str(ASSETS / "icon.ico") if (ASSETS / "icon.ico").exists() else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MailCrawler",
)
app = BUNDLE(
    coll,
    name="MailCrawler.app",
    icon=str(ASSETS / "icon.icns") if (ASSETS / "icon.icns").exists() else None,
    bundle_identifier="io.github.mailcrawler",
    info_plist={
        "CFBundleName": "MailCrawler",
        "CFBundleDisplayName": "邮件爬虫",
        "NSHighResolutionCapable": True,
    },
)
