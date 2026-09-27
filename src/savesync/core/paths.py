"""Where Save Sync keeps its own state: `%APPDATA%\\SaveSync\\` (plan §11).

Only metadata lives here — configuration, the registry, fingerprints, logs and
temporary safety snapshots. Never a permanent copy of the save library.
"""
from __future__ import annotations

import os
import sys

APP_NAME = "SaveSync"
ENV_OVERRIDE = "SAVESYNC_HOME"


def app_dir() -> str:
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        return os.path.abspath(override)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


class AppPaths:
    def __init__(self, root: str | None = None):
        self.root = os.path.abspath(root or app_dir())

    @property
    def config(self) -> str:
        return os.path.join(self.root, "config.json")

    @property
    def registry(self) -> str:
        return os.path.join(self.root, "games.json")

    @property
    def events(self) -> str:
        return os.path.join(self.root, "events.jsonl")

    @property
    def safety(self) -> str:
        return os.path.join(self.root, "safety")

    @property
    def lock(self) -> str:
        return os.path.join(self.root, "sync.lock")

    @property
    def ludusavi_config(self) -> str:
        """Save Sync's private Ludusavi configuration directory."""
        return os.path.join(self.root, "ludusavi")

    @property
    def covers(self) -> str:
        return os.path.join(self.root, "covers")

    def ensure(self) -> "AppPaths":
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(self.safety, exist_ok=True)
        return self
