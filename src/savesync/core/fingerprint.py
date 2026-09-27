"""Local-save fingerprints (plan §10.4).

A fingerprint is a digest of the save files Ludusavi identified for a game — never
their contents. It answers "did the PC saves change since the baseline?".

Fast path: a different set of paths, a missing file or a different size is a
change without reading a byte. When the metadata looks unchanged, contents are
hashed anyway, because an edit that keeps size and timestamp must still count as
a change. Every error is `None` ("don't know"), never "unchanged".
"""
from __future__ import annotations

import hashlib
import json
import os

try:  # optional fast digest
    import xxhash
except ImportError:  # pragma: no cover - depends on the environment
    xxhash = None

FORMAT = "sfp1"
CHUNK = 1 << 20


class FingerprintError(Exception):
    pass


def _available_algorithms() -> list:
    return (["xxh3_128"] if xxhash is not None else []) + ["sha256"]


def _hasher(algorithm: str):
    if algorithm == "xxh3_128":
        if xxhash is None:
            raise FingerprintError("xxhash is not installed")
        return xxhash.xxh3_128()
    if algorithm == "sha256":
        return hashlib.sha256()
    raise FingerprintError("unknown fingerprint algorithm %r" % algorithm)


def normalize(path: str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def expand(paths) -> list:
    """Files behind `paths`; directories are walked so a folder given by a caller
    covers the files inside it."""
    out = []
    for path in paths or []:
        path = str(path)
        if os.path.isdir(path):
            for root, _dirs, files in os.walk(path):
                out.extend(os.path.join(root, name) for name in files)
        else:
            out.append(path)
    return sorted({normalize(p) for p in out})


class Fingerprint:
    def __init__(self, algorithm: str | None = None):
        self.algorithm = algorithm or _available_algorithms()[0]
        _hasher(self.algorithm)  # fail early on an unusable choice

    # --- public interface (plan §10.4) ---

    def compute(self, paths, extra: dict | None = None) -> str:
        """Raises FingerprintError when a file exists but cannot be read."""
        return self._encode(self._describe(expand(paths), extra, self.algorithm))

    def has_changed(self, previous: str, paths, extra: dict | None = None):
        """True / False / None. None = unknown (no usable previous fingerprint,
        unreadable file, algorithm unavailable) and must never be read as False."""
        old = self.decode(previous)
        if old is None:
            return None
        files = expand(paths)
        old_files = old.get("files") or {}
        if set(files) != set(old_files):
            return True
        if (self._extra_digests(extra, old["alg"]) if extra else {}) != (old.get("extra") or {}):
            return True
        try:
            for path in files:
                size = self._size(path)
                previous_entry = old_files.get(path)
                previous_size = previous_entry[0] if previous_entry else None
                if size != previous_size:
                    return True
            current = self._describe(files, None, old["alg"])
        except FingerprintError:
            return None
        return current["files"] != old_files

    # --- helpers ---

    @staticmethod
    def decode(value):
        if not value or not isinstance(value, str) or not value.startswith(FORMAT + ":"):
            return None
        try:
            data = json.loads(value[len(FORMAT) + 1:])
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or data.get("alg") not in _available_algorithms():
            return None
        return data

    @staticmethod
    def _encode(data: dict) -> str:
        return FORMAT + ":" + json.dumps(data, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _size(path: str):
        try:
            return os.stat(path).st_size
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise FingerprintError(str(exc)) from exc

    def _digest_file(self, path: str, algorithm: str):
        hasher = _hasher(algorithm)
        try:
            with open(path, "rb") as fh:
                for block in iter(lambda: fh.read(CHUNK), b""):
                    hasher.update(block)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise FingerprintError("%s: %s" % (path, exc)) from exc
        return hasher.hexdigest()

    def _extra_digests(self, extra: dict, algorithm: str) -> dict:
        out = {}
        for name, value in sorted((extra or {}).items()):
            if value is None:
                continue
            hasher = _hasher(algorithm)
            hasher.update(str(value).encode("utf-8"))
            out[str(name)] = hasher.hexdigest()
        return out

    def _describe(self, files: list, extra, algorithm: str) -> dict:
        entries = {}
        for path in files:
            size = self._size(path)
            digest = self._digest_file(path, algorithm) if size is not None else None
            # a listed file that vanished is part of the state ("missing"), not an error
            entries[path] = [size, digest] if digest is not None else [None, None]
        data = {"alg": algorithm, "files": entries}
        digests = self._extra_digests(extra, algorithm)
        if digests:
            data["extra"] = digests
        return data
