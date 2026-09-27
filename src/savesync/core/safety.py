"""Temporary safety snapshots of PC saves (plan §14).

Each snapshot is its own Ludusavi backup directory under
`%APPDATA%\\SaveSync\\safety\\<stamp>-<kind>`, so expiring one is removing a folder —
never editing a Ludusavi index. Snapshots exist for rollback and recovery; they
are not part of the permanent save library, which lives only on the USB.
"""
from __future__ import annotations

import os
import shutil
import time
import uuid

MAX_AGE_DAYS = 7


def _size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return ("%d %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024
    return "%d B" % size


class SafetyStore:
    def __init__(self, root: str, clock=time.time):
        self.root = os.path.abspath(root)
        self.clock = clock

    def new_snapshot(self, kind: str) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
        path = os.path.join(self.root, "%s-%s-%s" % (stamp, kind, uuid.uuid4().hex[:6]))
        os.makedirs(path, exist_ok=True)
        # the creation time is recorded explicitly: directory mtimes change whenever
        # Ludusavi writes inside
        with open(os.path.join(path, ".created"), "w", encoding="utf-8") as fh:
            fh.write(str(self.clock()))
        return path

    def created(self, path: str) -> float:
        try:
            with open(os.path.join(path, ".created"), encoding="utf-8") as fh:
                return float(fh.read().strip())
        except (OSError, ValueError):
            try:
                return os.path.getmtime(path)
            except OSError:
                return 0.0

    def list(self) -> list:
        """[(path, created, size)] oldest first."""
        try:
            names = os.listdir(self.root)
        except OSError:
            return []
        out = []
        for name in names:
            path = os.path.join(self.root, name)
            if os.path.isdir(path):
                out.append((path, self.created(path), _size(path)))
        return sorted(out, key=lambda item: item[1])

    def total_size(self) -> int:
        return sum(size for _p, _c, size in self.list())

    def cleanup(self, keep=(), max_age_days: float = MAX_AGE_DAYS) -> tuple:
        """Remove snapshots older than `max_age_days` (or all with max_age_days=0),
        except the ones in `keep` (active trials, unrecovered failed restores).
        Returns (count, bytes)."""
        protected = {os.path.normcase(os.path.abspath(p)) for p in keep if p}
        limit = self.clock() - max_age_days * 86400
        count = freed = 0
        for path, created, size in self.list():
            if os.path.normcase(path) in protected:
                continue
            if max_age_days and created > limit:
                continue
            shutil.rmtree(path, ignore_errors=True)
            if not os.path.exists(path):
                count += 1
                freed += size
        return count, freed
