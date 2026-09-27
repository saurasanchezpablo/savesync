"""Windows removable-media detector, driven by a fake Win32 volume API; plus the
real API on a Windows host."""
import os
import sys
import threading

import pytest

from savesync.core import usb as usbmod
from savesync.platform.windows.media import (DRIVE_FIXED, DRIVE_REMOVABLE,
                                             WindowsRemovableMediaDetector, format_serial)


class FakeVolumes:
    def __init__(self):
        self.drives = {}  # letter -> (type, label, serial) ; serial None = not ready

    def logical_drives(self):
        mask = 0
        for letter in self.drives:
            mask |= 1 << (ord(letter) - ord("A"))
        return mask

    def drive_type(self, root):
        return self.drives[root[0]][0]

    def volume_info(self, root):
        kind, label, serial = self.drives[root[0]]
        return None if serial is None else (label, serial)

    def system_drive(self):
        return "C:\\"


def test_lists_removable_drives_with_serial_and_label():
    api = FakeVolumes()
    api.drives = {"C": (DRIVE_FIXED, "OS", 1), "E": (DRIVE_REMOVABLE, "SAVES", 0x1A2B3C4D)}
    (drive,) = WindowsRemovableMediaDetector(api).get_removable_drives()
    assert drive.drive_letter == "E:\\" and drive.label == "SAVES"
    assert drive.volume_serial == "1A2B-3C4D" and drive.is_removable


def test_empty_card_reader_slot_is_skipped():
    api = FakeVolumes()
    api.drives = {"F": (DRIVE_REMOVABLE, "", None)}
    assert WindowsRemovableMediaDetector(api).get_removable_drives() == []


def test_serial_format():
    assert format_serial(0) == "0000-0000"
    assert format_serial(-1) == "FFFF-FFFF"


def test_monitoring_reports_insertions_letter_changes_and_removals():
    api = FakeVolumes()
    detector = WindowsRemovableMediaDetector(api, interval=0.01)
    seen = []
    detector._last = detector._signature(detector.get_removable_drives())
    api.drives["E"] = (DRIVE_REMOVABLE, "SAVES", 7)
    assert detector.poll_once(seen.append)
    assert not detector.poll_once(seen.append), "no change, no callback"
    del api.drives["E"]
    api.drives["G"] = (DRIVE_REMOVABLE, "SAVES", 7)  # same volume, new letter
    assert detector.poll_once(seen.append)
    api.drives.clear()
    assert detector.poll_once(seen.append)
    assert [[d.drive_letter for d in s] for s in seen] == [["E:\\"], ["G:\\"], []]
    assert seen[0][0].volume_serial == seen[1][0].volume_serial


def test_background_thread_starts_and_stops():
    api = FakeVolumes()
    detector = WindowsRemovableMediaDetector(api, interval=0.01)
    got = threading.Event()
    detector.start_monitoring(lambda drives: got.set())
    api.drives["E"] = (DRIVE_REMOVABLE, "X", 1)
    assert got.wait(2)
    detector.stop_monitoring()
    assert detector._thread is None


def test_identity_survives_a_letter_change(tmp_path):
    """The registered USB is recognized on a new letter (identity = marker + serial)."""
    from savesync.platform.interfaces import DriveInfo
    e = DriveInfo(str(tmp_path / "E"), "1A2B-3C4D", "SAVES", True)
    os.makedirs(e.drive_letter)
    identity = usbmod.initialize(e)
    cfg = {**usbmod.registration_fields(e, identity)}
    os.rename(e.drive_letter, str(tmp_path / "G"))
    g = DriveInfo(str(tmp_path / "G"), "1A2B-3C4D", "SAVES", True)
    assert usbmod.find_registered([g], cfg) == g


@pytest.mark.windows_only
@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")
def test_real_win32_enumeration_does_not_fail():
    drives = WindowsRemovableMediaDetector().get_removable_drives()
    assert isinstance(drives, list)
