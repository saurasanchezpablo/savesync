"""Development fallbacks for non-Windows hosts.

Not Linux support (plan §32): just enough to run and test the application on a
developer machine. A mounted volume qualifies when it already carries the
SaveSync folder; its marker is still verified like on Windows.
"""
from __future__ import annotations

import os
import threading

from .interfaces import (SAVESYNC_DIR, DriveInfo, RemovableMediaDetector, ShutdownIntegration,
                         SingleInstanceGuard, StartupManager)


class MountPointDetector(RemovableMediaDetector):
    def __init__(self, interval: float = 2.0):
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def get_removable_drives(self) -> list:
        import psutil
        drives = []
        for part in psutil.disk_partitions(all=False):
            mount = part.mountpoint
            if os.path.isdir(os.path.join(mount, SAVESYNC_DIR)):
                drives.append(DriveInfo(drive_letter=mount, volume_serial="",
                                        label=os.path.basename(mount.rstrip("/")) or mount,
                                        is_removable=True))
        return drives

    def start_monitoring(self, callback) -> None:
        self._stop.clear()
        last = {d.drive_letter for d in self.get_removable_drives()}

        def loop():
            nonlocal last
            while not self._stop.wait(self.interval):
                drives = self.get_removable_drives()
                now = {d.drive_letter for d in drives}
                if now != last:
                    last = now
                    callback(drives)
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop_monitoring(self) -> None:
        self._stop.set()


class UnsupportedStartupManager(StartupManager):
    supported = False

    def is_enabled(self) -> bool:
        return False

    def enable(self) -> None:
        raise NotImplementedError("Start with the system is only available on Windows")

    def disable(self) -> None:
        pass


class NullShutdownIntegration(ShutdownIntegration):
    def register_handler(self, callback, precheck=None) -> None:
        self.callback = callback

    def unregister_handler(self) -> None:
        self.callback = None


class LockFileGuard(SingleInstanceGuard):
    """QLockFile-based single instance for non-Windows development hosts."""

    def __init__(self, path: str):
        self.path = path
        self._lock = None

    def acquire(self) -> bool:
        from PySide6.QtCore import QLockFile
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = QLockFile(self.path)
        self._lock.setStaleLockTime(0)
        return self._lock.tryLock(100)

    def release(self) -> None:
        if self._lock is not None:
            self._lock.unlock()
            self._lock = None
