# Derived from decky-nonsteam-sync (py_modules/sdsync/registry.py),
# Copyright (c) 2026, JoseArkadio — BSD-3-Clause, see LICENSE-UPSTREAM.
"""Thread-safe JSON stores for per-game synchronization metadata and for the
application configuration (plan §11).

Nothing here ever holds save-file contents: only backup identities, fingerprints
(digests), paths and flags.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import unicodedata

FIELDS = {
    "title": "",
    # `when` of the USB backup this PC last synchronized with. It is an IDENTITY,
    # compared only for equality, so skewed clocks between PCs cannot break it.
    "last_synced_backup": "",
    # Which registered USB that baseline belongs to. A baseline against another
    # USB is no baseline at all.
    "baseline_usb_id": "",
    "local_fingerprint": "",
    "save_paths": [],
    "conflict": False,
    "excluded": False,
    # Ludusavi's database does not know this title (measured upstream: one such
    # title used to fail the whole batch call). Cleared when the title changes.
    "ludusavi_unknown": False,
    "last_sync_ts": None,
    # Set by the file watcher. Only a hint that a later sync should look; the sync
    # itself validates through Ludusavi and the fingerprint.
    "dirty": False,
    "error": "",
    # Last classification, so the UI can show state without a USB.
    "state": "unknown",
    "state_message": None,
    # An upload of local state started but was never confirmed by validation —
    # the USB may hold a partial version from this PC (`local_ahead`).
    "pending_upload": False,
    # An operation that was interrupted: {"kind", "started", ...}. Kept until a
    # later validated operation replaces it.
    "pending_op": None,
    # Most recent safety snapshot directory for this game (rollback / recovery).
    "last_safety": "",
    # Active trial (plan §15) or None.
    "trial": None,
    # User-provided process names that mean "this game is running".
    "process_names": [],
    # What the last cycle saw, for the UI (never used for decisions).
    "pc_has_save": False,
    "usb_has_backup": False,
    "restore_problem": "",
    "last_restore_created": [],
}

CONFIG_FIELDS = {
    "usb_id": "",
    "usb_label": "",
    "usb_volume_serial": "",
    "ludusavi_command": ["ludusavi"],
    # Explicit executable that beats both the USB copy and `ludusavi_command`.
    "ludusavi_path_override": "",
    # Ludusavi configuration directory; empty = Save Sync's private one.
    "ludusavi_config_dir": "",
    "safety_path": "",
    "full_limit": 2,
    "differential_limit": 1,
    "sync_on_shutdown": False,
    "sync_on_usb_connect": True,
    "log_path": "",
    "first_run": True,
    "notifications": {
        "usb_connected": True,
        "sync_completed": True,
        "conflict": True,
        "error": True,
        "usb_removed_pending": True,
        "game_running": True,
        "shutdown_incomplete": True,
    },
    "playnite_export_path": "",
    "last_sync_ts": None,
    "shutdown_deadline_seconds": 170,
    "shutdown_incomplete": False,
}

# Ludusavi writes a full backup in place when only one is kept, which would
# destroy the previous version before the new one is validated (measured).
MIN_FULL_LIMIT = 2

_ASCII_FALLBACK = str.maketrans({
    "ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D",
    "ð": "d", "Ð": "D", "þ": "th", "Þ": "Th", "æ": "ae", "Æ": "Ae",
    "œ": "oe", "Œ": "Oe", "ß": "ss",
})

# One lock per process, not per instance: several store objects may point at the
# same file, and a per-instance lock would protect nothing.
_WRITE_LOCK = threading.RLock()


def title_key(title: str) -> str:
    """Registry key: ASCII transliteration, spaces to "-". A title without a single
    Latin letter gets a stable hash key — never an empty one."""
    if not title or not title.strip():
        return ""
    normalized = unicodedata.normalize("NFKD", title.strip().translate(_ASCII_FALLBACK))
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^a-z0-9\s]", "", stripped.lower())
    key = re.sub(r"\s+", "-", cleaned).strip("-")
    if key:
        return key
    seed = unicodedata.normalize("NFKC", title.strip()).lower().encode("utf-8")
    return "t-" + hashlib.sha1(seed).hexdigest()[:12]


class _JsonStore:
    prefix = ".store-"

    def __init__(self, path: str):
        self.path = path

    def _read(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            # A damaged file is not the same as no file. A silent {} would let the
            # next write persist emptiness and erase every baseline — so the file
            # is set aside where it can be recovered.
            try:
                os.replace(self.path, self.path + ".broken")
            except OSError:
                pass
            return {}
        except OSError:
            return {}

    def _write(self, data: dict) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        # A unique temporary name is mandatory: with a fixed ".tmp" two writers
        # open the same file and produce invalid JSON.
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=self.prefix, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1, default=str)
            for attempt in range(10):
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:
                    # Windows: a reader holding the file open blocks the rename
                    if attempt == 9:
                        raise
                    time.sleep(0.05)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class Registry(_JsonStore):
    prefix = ".games-"

    def _blank(self, key: str) -> dict:
        rec = {"title_key": key}
        for field, default in FIELDS.items():
            # copies of mutable defaults: a shared list would mean one game's
            # paths appear in every other record
            rec[field] = copy.deepcopy(default)
        return rec

    def _complete(self, rec: dict) -> dict:
        out = self._blank(rec.get("title_key", ""))
        out.update(rec)
        return out

    def all(self) -> list:
        return [self._complete(r) for r in self._read().values() if isinstance(r, dict)]

    def get(self, key: str):
        rec = self._read().get(key)
        return self._complete(rec) if isinstance(rec, dict) else None

    def get_by_title(self, title: str):
        return self.get(title_key(title))

    def ensure(self, title: str) -> dict:
        """The record for `title`, created blank if missing."""
        key = title_key(title)
        with _WRITE_LOCK:
            existing = self.get(key)
            if existing is not None:
                return existing
            return self.upsert({"title": title})

    def upsert(self, record: dict) -> dict:
        title = (record.get("title") or "").strip()
        key = record.get("title_key") or title_key(title)
        if not key:
            raise ValueError("a record needs a title or a title_key")
        # read-modify-write must be atomic, or another thread writes back its own
        # older copy of the whole file in between
        with _WRITE_LOCK:
            data = self._read()
            merged = data.get(key) or self._blank(key)
            for field, value in record.items():
                if value is not None or field not in merged:
                    merged[field] = value
            merged["title"] = (title or merged.get("title") or "").strip()
            if not merged["title"]:
                raise ValueError("a record needs a non-empty title")
            merged["title_key"] = key
            data[key] = merged
            self._write(data)
            return self._complete(merged)

    def write(self, record: dict) -> dict:
        """Like upsert, but None is a value: `{"trial": None}` clears the trial.
        (upsert keeps the upstream meaning of None = "leave as it is".)"""
        title = (record.get("title") or "").strip()
        key = record.get("title_key") or title_key(title)
        if not key:
            raise ValueError("a record needs a title or a title_key")
        with _WRITE_LOCK:
            data = self._read()
            merged = data.get(key) or self._blank(key)
            merged.update(record)
            merged["title"] = (title or merged.get("title") or "").strip()
            if not merged["title"]:
                raise ValueError("a record needs a non-empty title")
            merged["title_key"] = key
            data[key] = merged
            self._write(data)
            return self._complete(merged)

    def set_fields(self, key: str, **fields) -> dict:
        with _WRITE_LOCK:
            data = self._read()
            if key not in data:
                raise KeyError(key)
            data[key].update(fields)
            self._write(data)
            return self._complete(data[key])

    def update_many(self, changes: dict) -> None:
        """{key: {field: value}} in ONE read-modify-write — a 200-game cycle must not
        rewrite the file 200 times. Unknown keys need a "title" to be created."""
        if not changes:
            return
        with _WRITE_LOCK:
            data = self._read()
            for key, fields in changes.items():
                if key not in data:
                    title = (fields.get("title") or "").strip()
                    if not title:
                        continue
                    data[key] = self._blank(key)
                data[key].update(fields)
            self._write(data)

    def remove(self, key: str) -> bool:
        with _WRITE_LOCK:
            data = self._read()
            if key not in data:
                return False
            del data[key]
            self._write(data)
            return True


class Config(_JsonStore):
    prefix = ".config-"

    def load(self) -> dict:
        data = self._read()
        out = copy.deepcopy(CONFIG_FIELDS)
        for key, value in data.items():
            if key == "notifications" and isinstance(value, dict):
                out["notifications"].update(value)
            else:
                out[key] = value
        return _sanitize(out)

    def get(self, key: str):
        return self.load().get(key)

    def update(self, **fields) -> dict:
        with _WRITE_LOCK:
            data = self._read()
            for key, value in fields.items():
                if key == "notifications" and isinstance(value, dict):
                    merged = dict(data.get("notifications") or {})
                    merged.update(value)
                    data[key] = merged
                else:
                    data[key] = value
            self._write(data)
            return self.load()


def _sanitize(cfg: dict) -> dict:
    """Values that would break a safety rule are clamped, not trusted."""
    try:
        cfg["full_limit"] = max(MIN_FULL_LIMIT, min(255, int(cfg.get("full_limit"))))
    except (TypeError, ValueError):
        cfg["full_limit"] = CONFIG_FIELDS["full_limit"]
    try:
        cfg["differential_limit"] = max(0, min(255, int(cfg.get("differential_limit"))))
    except (TypeError, ValueError):
        cfg["differential_limit"] = CONFIG_FIELDS["differential_limit"]
    command = cfg.get("ludusavi_command")
    if isinstance(command, str):
        cfg["ludusavi_command"] = [command] if command.strip() else ["ludusavi"]
    elif not isinstance(command, list) or not command:
        cfg["ludusavi_command"] = ["ludusavi"]
    try:
        cfg["shutdown_deadline_seconds"] = max(10, min(600, int(cfg.get("shutdown_deadline_seconds"))))
    except (TypeError, ValueError):
        cfg["shutdown_deadline_seconds"] = CONFIG_FIELDS["shutdown_deadline_seconds"]
    return cfg
