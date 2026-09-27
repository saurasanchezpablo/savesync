"""OS-dependent behavior, isolated behind interfaces (plan §5, §18.1).

The first implementation is Windows, but nothing here is Windows-specific: a
Linux implementation (udev, desktop-session integration) plugs in behind the same
contracts.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass

SAVESYNC_DIR = "SaveSync"
BACKUPS_DIR = "backups"
LUDUSAVI_DIR = "ludusavi"


@dataclass(frozen=True)
class DriveInfo:
    """One mounted volume. `drive_letter` is the mount root ("E:\\" on Windows, a
    plain directory for the fake medium) — callers never rely on its value being
    stable between connections."""
    drive_letter: str
    volume_serial: str
    label: str
    is_removable: bool
    backup_path: str = ""

    @property
    def root(self) -> str:
        return self.drive_letter

    @property
    def savesync_dir(self) -> str:
        return os.path.join(self.drive_letter, SAVESYNC_DIR)

    @property
    def backups_dir(self) -> str:
        return self.backup_path or os.path.join(self.savesync_dir, BACKUPS_DIR)

    @property
    def ludusavi_dir(self) -> str:
        return os.path.join(self.savesync_dir, LUDUSAVI_DIR)

    @property
    def display_name(self) -> str:
        label = self.label or "USB"
        return "%s (%s)" % (label, self.drive_letter.rstrip("\\/") or self.drive_letter)


class RemovableMediaDetector(ABC):
    @abstractmethod
    def get_removable_drives(self) -> list[DriveInfo]: ...

    @abstractmethod
    def start_monitoring(self, callback) -> None:
        """`callback(drives: list[DriveInfo])` whenever the set of drives changes.
        May be called from a background thread."""

    @abstractmethod
    def stop_monitoring(self) -> None: ...


class ProcessChecker(ABC):
    """Asked only while a synchronization is about to modify saves — never a
    permanent monitor (plan §2.2)."""

    def begin_cycle(self) -> None:
        """Take one process snapshot for the whole cycle."""

    def end_cycle(self) -> None:
        """Drop the snapshot."""

    @abstractmethod
    def is_running(self, title: str) -> bool: ...

    def is_running_now(self, title: str) -> bool:
        """Like is_running, but from a fresh look at the processes — asked right
        before saves are written, when the cycle's snapshot may be stale."""
        return self.is_running(title)


class StartupManager(ABC):
    @abstractmethod
    def is_enabled(self) -> bool: ...

    @abstractmethod
    def enable(self) -> None: ...

    @abstractmethod
    def disable(self) -> None: ...


class ShutdownIntegration(ABC):
    @abstractmethod
    def register_handler(self, callback, precheck=None) -> None:
        """`callback(deadline_seconds) -> bool` runs the shutdown sync; True when
        it finished. `precheck() -> bool` says whether there is anything to do
        (USB connected and pending changes). Must never block shutdown
        indefinitely."""

    @abstractmethod
    def unregister_handler(self) -> None: ...


class FileWatcher(ABC):
    @abstractmethod
    def watch(self, paths: list[str], callback) -> None:
        """Replace the watched set. `callback(path)` for each changed path; may be
        called from a background thread."""

    @abstractmethod
    def stop(self) -> None: ...


class NotificationProvider(ABC):
    @abstractmethod
    def notify(self, title: str, message: str) -> None: ...


class SingleInstanceGuard(ABC):
    @abstractmethod
    def acquire(self) -> bool:
        """True when this is the only running instance."""

    @abstractmethod
    def release(self) -> None: ...
