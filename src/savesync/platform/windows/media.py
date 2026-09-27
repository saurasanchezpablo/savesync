"""Windows removable-media detection (plan §9, §18.2).

Volumes are enumerated with Win32 (GetLogicalDrives / GetDriveTypeW /
GetVolumeInformationW) and polled on a background thread about every two
seconds. The drive letter is reported but never part of the identity: the
volume serial plus the Save Sync marker are (see core/usb.py).
"""
from __future__ import annotations

import os
import string
import threading

from ..interfaces import SAVESYNC_DIR, DriveInfo, RemovableMediaDetector

DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOOPENFILEERRORBOX = 0x8000


class Win32VolumeApi:
    """Thin ctypes wrapper; replaced by a fake in tests."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ctypes = ctypes
        self._wintypes = wintypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # An empty card-reader slot must not pop up "There is no disk in the drive".
        self.kernel32.SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX)

    def logical_drives(self) -> int:
        return int(self.kernel32.GetLogicalDrives())

    def drive_type(self, root: str) -> int:
        return int(self.kernel32.GetDriveTypeW(self._wintypes.LPCWSTR(root)))

    def volume_info(self, root: str):
        """(label, serial) or None when the volume is not ready (empty slot)."""
        ctypes, wintypes = self._ctypes, self._wintypes
        label = ctypes.create_unicode_buffer(261)
        fs_name = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        max_len = wintypes.DWORD()
        flags = wintypes.DWORD()
        ok = self.kernel32.GetVolumeInformationW(
            wintypes.LPCWSTR(root), label, len(label), ctypes.byref(serial),
            ctypes.byref(max_len), ctypes.byref(flags), fs_name, len(fs_name))
        if not ok:
            return None
        return label.value, serial.value

    def system_drive(self) -> str:
        return (os.environ.get("SystemDrive") or "C:").rstrip("\\") + "\\"


def format_serial(serial: int) -> str:
    """The way `vol` shows it: 1A2B-3C4D."""
    text = "%08X" % (int(serial) & 0xFFFFFFFF)
    return text[:4] + "-" + text[4:]


class WindowsRemovableMediaDetector(RemovableMediaDetector):
    def __init__(self, api=None, interval: float = 2.0):
        self.api = api or Win32VolumeApi()
        self.interval = interval
        self._thread = None
        self._stop = threading.Event()
        self._last = None

    def get_removable_drives(self) -> list:
        mask = self.api.logical_drives()
        system = self.api.system_drive().upper()
        drives = []
        for index, letter in enumerate(string.ascii_uppercase):
            if not mask & (1 << index):
                continue
            root = letter + ":\\"
            kind = self.api.drive_type(root)
            if kind == DRIVE_REMOVABLE:
                removable = True
            elif kind == DRIVE_FIXED and root.upper() != system \
                    and os.path.isdir(os.path.join(root, SAVESYNC_DIR)):
                # USB SSDs and hard disks report themselves as fixed; they qualify
                # only when they already carry the Save Sync folder
                removable = False
            else:
                continue
            info = self.api.volume_info(root)
            if info is None:
                continue
            label, serial = info
            drives.append(DriveInfo(drive_letter=root, volume_serial=format_serial(serial),
                                    label=label or "", is_removable=removable))
        return drives

    @staticmethod
    def _signature(drives) -> frozenset:
        return frozenset((d.drive_letter, d.volume_serial, d.label) for d in drives)

    def poll_once(self, callback) -> bool:
        """One polling step; True when the set changed (and callback ran)."""
        try:
            drives = self.get_removable_drives()
        except OSError:
            return False
        signature = self._signature(drives)
        if signature == self._last:
            return False
        self._last = signature
        callback(drives)
        return True

    def start_monitoring(self, callback) -> None:
        self.stop_monitoring()
        self._stop.clear()
        self._last = self._signature(self.get_removable_drives())

        def loop():
            while not self._stop.wait(self.interval):
                self.poll_once(callback)

        self._thread = threading.Thread(target=loop, name="savesync-media", daemon=True)
        self._thread.start()

    def stop_monitoring(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1)
            self._thread = None
