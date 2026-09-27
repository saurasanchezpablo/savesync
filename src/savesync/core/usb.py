"""Registered-USB identity (plan §8, §9).

A drive is trusted only when its marker (`SaveSync/savesync-identity.json`)
names the registered USB, its volume serial matches (when the platform reports
one) and the expected structure exists. A drive letter is never part of the
identity. Anything else is an unknown USB and is never used as the source of
truth (safety rule 3).
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from enum import Enum

from ..platform.interfaces import DriveInfo

IDENTITY_FILE = "savesync-identity.json"
IDENTITY_FORMAT = 1


class UsbMatch(Enum):
    REGISTERED = "registered"
    UNKNOWN = "unknown"            # a marker for another USB, or wrong serial/structure
    CORRUPT = "corrupt"            # marker present but unreadable
    UNINITIALIZED = "uninitialized"  # no marker: a plain drive
    UNREGISTERED = "unregistered"  # has a marker, but this PC has no registration yet


@dataclass(frozen=True)
class UsbIdentity:
    usb_id: str
    label: str = ""
    volume_serial: str = ""
    created: float = 0.0


class IdentityError(Exception):
    pass


def identity_path(drive: DriveInfo) -> str:
    return os.path.join(drive.savesync_dir, IDENTITY_FILE)


def read_identity(drive: DriveInfo):
    """UsbIdentity, None when there is no marker; IdentityError when it is damaged."""
    path = identity_path(drive)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise IdentityError(str(exc)) from exc
    if not isinstance(data, dict) or not str(data.get("usb_id") or "").strip():
        raise IdentityError("the marker has no usb_id")
    return UsbIdentity(usb_id=str(data["usb_id"]), label=str(data.get("label") or ""),
                       volume_serial=str(data.get("volume_serial") or ""),
                       created=float(data.get("created") or 0.0))


def classify(drive: DriveInfo, config: dict) -> tuple:
    """(UsbMatch, UsbIdentity|None, detail)."""
    registered_id = (config.get("usb_id") or "").strip()
    try:
        identity = read_identity(drive)
    except IdentityError as exc:
        return UsbMatch.CORRUPT, None, str(exc)
    if identity is None:
        return (UsbMatch.UNKNOWN if registered_id else UsbMatch.UNINITIALIZED), None, \
            "no Save Sync marker"
    if not registered_id:
        return UsbMatch.UNREGISTERED, identity, ""
    if identity.usb_id != registered_id:
        return UsbMatch.UNKNOWN, identity, "marker belongs to another USB"
    expected_serial = (config.get("usb_volume_serial") or "").strip()
    if expected_serial and drive.volume_serial and drive.volume_serial != expected_serial:
        # The same marker on a different volume: a copied folder or a cloned
        # stick. Not the medium that was registered.
        return UsbMatch.UNKNOWN, identity, "volume serial does not match"
    if not os.path.isdir(drive.backups_dir):
        return UsbMatch.UNKNOWN, identity, "SaveSync\\backups is missing"
    return UsbMatch.REGISTERED, identity, ""


def find_registered(drives, config: dict):
    """The connected registered drive, whatever letter it has now, or None."""
    for drive in drives or []:
        if classify(drive, config)[0] == UsbMatch.REGISTERED:
            return drive
    return None


def initialize(drive: DriveInfo) -> UsbIdentity:
    """Create (or adopt) the Save Sync structure on `drive`. An existing readable
    marker is kept, so registering the same USB on a second PC keeps its identity."""
    os.makedirs(drive.backups_dir, exist_ok=True)
    os.makedirs(drive.ludusavi_dir, exist_ok=True)
    try:
        existing = read_identity(drive)
    except IdentityError:
        existing = None
    if existing is not None:
        return existing
    identity = UsbIdentity(usb_id=str(uuid.uuid4()), label=drive.label,
                           volume_serial=drive.volume_serial, created=time.time())
    path = identity_path(drive)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"format": IDENTITY_FORMAT, "app": "SaveSync",
                   "usb_id": identity.usb_id, "label": identity.label,
                   "volume_serial": identity.volume_serial,
                   "created": identity.created}, fh, indent=1)
    os.replace(tmp, path)
    return identity


def registration_fields(drive: DriveInfo, identity: UsbIdentity) -> dict:
    """Config fields that register `drive` on this PC."""
    return {"usb_id": identity.usb_id, "usb_label": drive.label,
            "usb_volume_serial": drive.volume_serial}


LUDUSAVI_NAMES = ("ludusavi.exe", "ludusavi")


def ludusavi_on_usb(drive: DriveInfo):
    """The portable Ludusavi binary shipped on the USB, or None."""
    for name in LUDUSAVI_NAMES:
        candidate = os.path.join(drive.ludusavi_dir, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def resolve_ludusavi(config: dict, drive: DriveInfo | None):
    """Priority: explicit override → the USB's portable copy → `ludusavi_command`
    (PATH). Returns (command list, source) or (None, reason)."""
    override = (config.get("ludusavi_path_override") or "").strip()
    if override:
        if os.path.isfile(override):
            return [override], "override"
        return None, "override %s does not exist" % override
    if drive is not None:
        found = ludusavi_on_usb(drive)
        if found:
            return [found], "usb"
    command = list(config.get("ludusavi_command") or ["ludusavi"])
    exe = command[0]
    if os.path.isabs(exe):
        return (command, "config") if os.path.isfile(exe) else (None, "%s does not exist" % exe)
    found = shutil.which(exe)
    if found:
        return [found] + command[1:], "path"
    return None, "%s is not on PATH" % exe
