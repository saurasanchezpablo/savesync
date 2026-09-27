"""Chooses the platform implementations for the running OS."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from .interfaces import (FileWatcher, ProcessChecker, RemovableMediaDetector,
                         ShutdownIntegration, SingleInstanceGuard, StartupManager)


@dataclass
class Platform:
    media: RemovableMediaDetector
    process: ProcessChecker
    startup: StartupManager
    shutdown: ShutdownIntegration
    watcher: FileWatcher
    instance: SingleInstanceGuard
    name: str = ""


def create_platform(app_root: str, process_hints=None, shutdown_deadline=lambda: 170,
                    fake_usb: str | None = None) -> Platform:
    from .windows.process import WindowsProcessChecker
    from .windows.watcher import WatchdogFileWatcher

    process = WindowsProcessChecker(process_hints)  # psutil: portable
    watcher = WatchdogFileWatcher()                  # watchdog: portable
    if sys.platform == "win32":
        from .windows.instance import NamedMutexGuard
        from .windows.media import WindowsRemovableMediaDetector
        from .windows.shutdown import WindowsShutdownIntegration
        from .windows.startup import WindowsStartupManager
        media = WindowsRemovableMediaDetector()
        startup = WindowsStartupManager()
        shutdown = WindowsShutdownIntegration(shutdown_deadline)
        instance = NamedMutexGuard()
        name = "windows"
    else:
        from .fallback import (LockFileGuard, MountPointDetector, NullShutdownIntegration,
                               UnsupportedStartupManager)
        media = MountPointDetector()
        startup = UnsupportedStartupManager()
        shutdown = NullShutdownIntegration()
        instance = LockFileGuard(os.path.join(app_root, "instance.lock"))
        name = "fallback"
    if fake_usb:
        from .fake import FakeRemovableMediaDetector, fake_drive
        media = FakeRemovableMediaDetector([fake_drive(fake_usb, serial="FAKE-USB")])
        name += "+fake-usb"
    return Platform(media, process, startup, shutdown, watcher, instance, name)
