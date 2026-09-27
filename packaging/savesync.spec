# PyInstaller spec — portable one-folder build (plan §6, §25 phase 4).
#   pyinstaller packaging/savesync.spec --noconfirm
# Output: dist/SaveSync/SaveSync(.exe) plus its runtime folder. Copy the whole
# folder anywhere (it does not need installation).
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[os.path.join(ROOT, "src")],
    hiddenimports=[
        "watchdog.observers.read_directory_changes",  # Windows backend
        "watchdog.observers.inotify",
        "PySide6.QtNetwork",
    ],
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
              "PySide6.Qt3DCore", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtPdf",
              "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SaveSync",
    console=False,          # tray application: no console window
    disable_windowed_traceback=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="SaveSync")
