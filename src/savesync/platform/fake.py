"""In-memory platform implementations for tests, demos and development.

`FakeRemovableMediaDetector` turns an ordinary directory into a USB drive at the
abstraction boundary (plan §26.1): tests connect, remove, re-letter and swap it
deterministically without hardware. It is also what `--fake-usb DIR` uses.
"""
from __future__ import annotations

import os
import threading

from .interfaces import (DriveInfo, FileWatcher, NotificationProvider, ProcessChecker,
                         RemovableMediaDetector, ShutdownIntegration, SingleInstanceGuard,
                         StartupManager)


def fake_drive(root: str, serial: str = "FAKE-0001", label: str = "SAVESYNC",
               removable: bool = True) -> DriveInfo:
    return DriveInfo(drive_letter=os.path.abspath(root), volume_serial=serial,
                     label=label, is_removable=removable)


class FakeRemovableMediaDetector(RemovableMediaDetector):
    def __init__(self, drives=None):
        self._drives = list(drives or [])
        self._callback = None
        self._lock = threading.Lock()
        self.monitoring = False

    # --- test controls ---

    def connect(self, drive: DriveInfo) -> None:
        with self._lock:
            self._drives = [d for d in self._drives if d.drive_letter != drive.drive_letter]
            self._drives.append(drive)
        self._fire()

    def remove(self, drive_or_root) -> None:
        root = getattr(drive_or_root, "drive_letter", drive_or_root)
        with self._lock:
            self._drives = [d for d in self._drives if d.drive_letter != root]
        self._fire()

    def remove_all(self) -> None:
        with self._lock:
            self._drives = []
        self._fire()

    def move(self, drive: DriveInfo, new_root: str) -> DriveInfo:
        """Same volume, new mount point — a drive-letter change."""
        moved = DriveInfo(drive_letter=os.path.abspath(new_root),
                          volume_serial=drive.volume_serial, label=drive.label,
                          is_removable=drive.is_removable)
        with self._lock:
            self._drives = [d for d in self._drives if d.drive_letter != drive.drive_letter]
            self._drives.append(moved)
        self._fire()
        return moved

    def _fire(self) -> None:
        callback = self._callback
        if callback is not None and self.monitoring:
            callback(self.get_removable_drives())

    # --- interface ---

    def get_removable_drives(self) -> list:
        with self._lock:
            # a removed directory is a removed drive
            return [d for d in self._drives if os.path.isdir(d.drive_letter)]

    def start_monitoring(self, callback) -> None:
        self._callback = callback
        self.monitoring = True

    def stop_monitoring(self) -> None:
        self.monitoring = False
        self._callback = None


class FakeProcessChecker(ProcessChecker):
    def __init__(self, running=()):
        self.running = set(running)
        self.cycles = 0
        self.queries = []

    def begin_cycle(self) -> None:
        self.cycles += 1

    def is_running(self, title: str) -> bool:
        self.queries.append(title)
        return title in self.running


class RecordingNotifier(NotificationProvider):
    def __init__(self):
        self.sent = []

    def notify(self, title: str, message: str) -> None:
        self.sent.append((title, message))


class FakeStartupManager(StartupManager):
    def __init__(self, enabled: bool = False):
        self.enabled = enabled

    def is_enabled(self) -> bool:
        return self.enabled

    def enable(self) -> None:
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False


class FakeShutdownIntegration(ShutdownIntegration):
    def __init__(self):
        self.callback = None

    def register_handler(self, callback) -> None:
        self.callback = callback

    def unregister_handler(self) -> None:
        self.callback = None

    def simulate_shutdown(self, deadline: float = 170):
        return self.callback(deadline) if self.callback else None


class FakeFileWatcher(FileWatcher):
    def __init__(self):
        self.paths = []
        self.callback = None

    def watch(self, paths, callback) -> None:
        self.paths = list(paths)
        self.callback = callback

    def stop(self) -> None:
        self.paths = []
        self.callback = None

    def emit(self, path: str) -> None:
        if self.callback:
            self.callback(path)


class FakeSingleInstance(SingleInstanceGuard):
    held = set()

    def __init__(self, name: str = "SaveSync"):
        self.name = name
        self.owned = False

    def acquire(self) -> bool:
        if self.name in FakeSingleInstance.held:
            return False
        FakeSingleInstance.held.add(self.name)
        self.owned = True
        return True

    def release(self) -> None:
        if self.owned:
            FakeSingleInstance.held.discard(self.name)
            self.owned = False
