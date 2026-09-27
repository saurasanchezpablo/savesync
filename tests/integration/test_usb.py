"""Registered-USB identity on the fake USB medium (plan §9, §26.1)."""
import json
import os
import shutil

from savesync.core import usb as usbmod
from savesync.core.usb import UsbMatch
from savesync.platform.fake import FakeRemovableMediaDetector, fake_drive


def test_initialize_creates_layout_and_marker(make_usb):
    drive, identity = make_usb()
    assert os.path.isdir(drive.backups_dir) and os.path.isdir(drive.ludusavi_dir)
    with open(usbmod.identity_path(drive)) as fh:
        marker = json.load(fh)
    assert marker["usb_id"] == identity.usb_id and marker["volume_serial"] == "FAKE-0001"


def test_initialize_keeps_an_existing_identity(make_usb):
    drive, identity = make_usb()
    assert usbmod.initialize(drive).usb_id == identity.usb_id


def test_registered_drive_matches_whatever_its_letter(make_usb, config, tmp_path):
    drive, identity = make_usb("E")
    config.update(**usbmod.registration_fields(drive, identity))
    assert usbmod.classify(drive, config.load())[0] == UsbMatch.REGISTERED
    detector = FakeRemovableMediaDetector([drive])
    moved = detector.move(drive, str(tmp_path / "F"))
    shutil.move(drive.drive_letter, moved.drive_letter)
    found = usbmod.find_registered(detector.get_removable_drives(), config.load())
    assert found is not None and found.drive_letter == moved.drive_letter


def test_unregistered_and_uninitialized(make_usb, config):
    drive, _ = make_usb("A")
    assert usbmod.classify(drive, config.load())[0] == UsbMatch.UNREGISTERED
    plain, _ = make_usb("B", initialize=False)
    assert usbmod.classify(plain, config.load())[0] == UsbMatch.UNINITIALIZED


def test_other_usb_is_unknown(make_usb, config):
    mine, identity = make_usb("mine")
    config.update(**usbmod.registration_fields(mine, identity))
    other, _ = make_usb("other", serial="OTHER")
    match, _, detail = usbmod.classify(other, config.load())
    assert match == UsbMatch.UNKNOWN and "another" in detail


def test_copied_marker_on_another_volume_is_unknown(make_usb, config, tmp_path):
    mine, identity = make_usb("mine")
    config.update(**usbmod.registration_fields(mine, identity))
    clone_root = tmp_path / "clone"
    shutil.copytree(mine.drive_letter, clone_root)
    clone = fake_drive(str(clone_root), serial="CLONE-SERIAL")
    assert usbmod.classify(clone, config.load())[0] == UsbMatch.UNKNOWN


def test_corrupt_or_empty_marker(make_usb, config):
    drive, identity = make_usb()
    config.update(**usbmod.registration_fields(drive, identity))
    for content in ("{not json", '{"usb_id": ""}', "[]"):
        with open(usbmod.identity_path(drive), "w") as fh:
            fh.write(content)
        assert usbmod.classify(drive, config.load())[0] == UsbMatch.CORRUPT


def test_missing_structure_is_not_trusted(make_usb, config):
    drive, identity = make_usb()
    config.update(**usbmod.registration_fields(drive, identity))
    os.rmdir(drive.backups_dir)
    assert usbmod.classify(drive, config.load())[0] == UsbMatch.UNKNOWN


def test_label_change_is_tolerated(make_usb, config):
    drive, identity = make_usb(label="OLD")
    config.update(**usbmod.registration_fields(drive, identity))
    relabeled = fake_drive(drive.drive_letter, serial=drive.volume_serial, label="NEW")
    assert usbmod.classify(relabeled, config.load())[0] == UsbMatch.REGISTERED


def test_removed_directory_is_a_removed_drive(make_usb):
    drive, _ = make_usb()
    detector = FakeRemovableMediaDetector([drive])
    seen = []
    detector.start_monitoring(seen.append)
    shutil.rmtree(drive.drive_letter)
    assert detector.get_removable_drives() == []
    detector.remove(drive)
    assert seen[-1] == []


def test_ludusavi_resolution_priority(make_usb, tmp_path):
    drive, _ = make_usb()
    config = {"ludusavi_path_override": "", "ludusavi_command": ["definitely-not-on-path-xyz"]}
    command, source = usbmod.resolve_ludusavi(config, drive)
    assert command is None and "PATH" in source
    exe = os.path.join(drive.ludusavi_dir, "ludusavi.exe")
    open(exe, "w").close()
    assert usbmod.resolve_ludusavi(config, drive) == ([exe], "usb")
    override = tmp_path / "dev-ludusavi"
    override.write_text("")
    config["ludusavi_path_override"] = str(override)
    assert usbmod.resolve_ludusavi(config, drive) == ([str(override)], "override")
    config["ludusavi_path_override"] = str(tmp_path / "missing")
    assert usbmod.resolve_ludusavi(config, drive)[0] is None


def test_static_fixture_is_a_valid_medium(tmp_path):
    source = os.path.join(os.path.dirname(__file__), "..", "fixtures", "fake_usb")
    root = tmp_path / "E"
    shutil.copytree(source, root)
    drive = fake_drive(str(root), serial="FAKE-0001")
    identity = usbmod.read_identity(drive)
    assert identity.usb_id == "00000000-0000-4000-8000-00000000f1a7"
    config = usbmod.registration_fields(drive, identity)
    assert usbmod.classify(drive, config)[0] == UsbMatch.REGISTERED
    other_serial = fake_drive(str(root), serial="FAKE-9999")
    assert usbmod.classify(other_serial, config)[0] == UsbMatch.UNKNOWN
