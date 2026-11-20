# PyInstaller: сборка Synex в папку (onedir). Запускает packaging/build.py, он же
# готовит папку SYNEX_STAGE: ui/ (собранный frontend), worker/ (воркер этой ОС), VERSION.
# -*- mode: python -*-

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 (SPECPATH задаёт PyInstaller)
BACKEND = ROOT / "backend"
ASSETS = ROOT / "packaging" / "assets"
STAGE = Path(os.environ["SYNEX_STAGE"]).resolve()
VERSION = (STAGE / "VERSION").read_text(encoding="utf-8").strip()
WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"
WORKER = STAGE / "worker" / ("synex_worker.exe" if WINDOWS else "synex_worker")

hidden = [
    # Плагины ищутся на ходу (pkgutil): все модули app нужны в сборке
    *collect_submodules("app"),
    *collect_submodules("uvicorn"),
    # Хранилище паролей ОС для мастер-ключа ключей SSH
    *collect_submodules("keyring"),
]
datas = [
    (str(STAGE / "ui"), "ui"),
    (str(STAGE / "VERSION"), "."),
    *collect_data_files("keyring"),
]
# pywebview (окно приложения на Windows и macOS) PyInstaller находит сам по импорту в
# app/desktop.py, если он установлен в среде сборки; без него UI откроется в браузере.

a = Analysis(  # noqa: F821
    [str(BACKEND / "app" / "desktop.py")],
    pathex=[str(BACKEND)],
    binaries=[(str(WORKER), "worker")],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "pytest", "_pytest", "ruff", "IPython", "matplotlib", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Synex",
    # Windows и macOS: окно без консоли (вывод в logs/synex.log папки данных).
    # Linux: из терминала видно журнал, из меню приложений консоль не появляется.
    console=not (WINDOWS or MACOS),
    icon=str(ASSETS / ("synex.ico" if WINDOWS else "synex.icns")),
    upx=False,
    strip=False,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    name="Synex",
    upx=False,
    strip=False,
)
if MACOS:
    app = BUNDLE(  # noqa: F821
        coll,
        name="Synex.app",
        icon=str(ASSETS / "synex.icns"),
        bundle_identifier="org.zeroddp.synex",
        version=VERSION,
        info_plist={
            "CFBundleName": "Synex",
            "CFBundleDisplayName": "Synex",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
