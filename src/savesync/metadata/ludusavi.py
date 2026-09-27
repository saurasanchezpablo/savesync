# Manifest reading approach derived from decky-nonsteam-sync (py_modules/sdsync/titles.py),
# Copyright (c) 2026, JoseArkadio — BSD-3-Clause, see LICENSE-UPSTREAM.
"""Metadata from Ludusavi's own game database (manifest.yaml).

Read as text, line by line: the manifest is tens of MB and a YAML library would
be both a dependency and far slower. Only the fields Save Sync uses are kept:
install folder names and launch executables (to recognize a running game),
and store IDs.
"""
from __future__ import annotations

import os
import threading
import unicodedata
from dataclasses import dataclass, field

from ..platform.windows.process import ProcessHints
from .interfaces import GameMetadataProvider


@dataclass
class ManifestEntry:
    install_dirs: set = field(default_factory=set)
    exes: set = field(default_factory=set)
    steam_id: str = ""
    gog_id: str = ""


def _key(line: str) -> str:
    """`  "Some: Key": {}` → `Some: Key`."""
    text = line.strip()
    if text.startswith("- "):
        text = text[2:]
    if text[:1] in ('"', "'"):
        quote = text[0]
        out, i = [], 1
        while i < len(text):
            ch = text[i]
            if quote == '"' and ch == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                if quote == "'" and text[i + 1:i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                break
            out.append(ch)
            i += 1
        return "".join(out)
    return text.split(":", 1)[0].strip()


def _value(line: str) -> str:
    return line.split(":", 1)[1].strip().strip('"\'') if ":" in line else ""


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def parse_manifest(path: str, titles=None) -> dict:
    """{title: ManifestEntry}; only `titles` when given."""
    wanted = set(titles) if titles is not None else None
    out = {}
    current = None
    section = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.rstrip("\n")
                if not line.strip() or line.lstrip().startswith("#") or line.startswith("---"):
                    continue
                depth = _indent(line)
                if depth == 0:
                    title = _key(line)
                    current = None
                    if wanted is None or title in wanted:
                        current = out.setdefault(title, ManifestEntry())
                    section = None
                    continue
                if current is None:
                    continue
                if depth == 2:
                    section = _key(line)
                    continue
                if depth == 4:
                    key = _key(line)
                    if section == "installDir":
                        current.install_dirs.add(key)
                    elif section == "launch":
                        exe = key.replace("\\", "/").rsplit("/", 1)[-1]
                        if exe.lower().endswith(".exe"):
                            current.exes.add(exe)
                    elif section == "steam" and key == "id":
                        current.steam_id = _value(line)
                    elif section == "gog" and key == "id":
                        current.gog_id = _value(line)
    except OSError:
        return {}
    return out


def fold(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", (text or "").lower())
                   if ch.isalnum())


class LudusaviMetadataProvider(GameMetadataProvider):
    name = "ludusavi"

    def __init__(self, manifest_path: str | None):
        self.manifest_path = manifest_path
        self._index = None
        self._lock = threading.Lock()

    def _entries(self) -> dict:
        with self._lock:
            if self._index is None:
                path = self.manifest_path
                self._index = parse_manifest(path) if path and os.path.isfile(path) else {}
            return self._index

    def reload(self) -> None:
        with self._lock:
            self._index = None

    def entry(self, title: str):
        return self._entries().get(title)

    def get_title(self, title):
        return title

    def get_ids(self, title):
        entry = self.entry(title)
        if entry is None:
            return {}
        ids = {}
        if entry.steam_id:
            ids["steam"] = entry.steam_id
        if entry.gog_id:
            ids["gog"] = entry.gog_id
        return ids

    def process_hints(self, title: str) -> ProcessHints:
        entry = self.entry(title)
        if entry is None:
            return ProcessHints()
        return ProcessHints(set(entry.exes), set(entry.install_dirs))
