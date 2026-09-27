# Derived from decky-nonsteam-sync (py_modules/sdsync/log.py),
# Copyright (c) 2026, JoseArkadio — BSD-3-Clause, see LICENSE-UPSTREAM.
"""Append-only JSONL event log (plan §22).

It is the user's only diagnostic window, so no damaged line and no bad argument
may take it down: writing a log entry must never become a second failure.
"""
from __future__ import annotations

import json
import os
import threading
import time

# Rotation instead of trimming keeps the file append-only: a full file is renamed
# once to `.1`, never rewritten.
MAX_BYTES = 5 * 1024 * 1024

_LOCK = threading.Lock()


class EventLog:
    def __init__(self, path: str, max_bytes: int = MAX_BYTES):
        self.path = path
        self.max_bytes = max_bytes
        self._listeners = []

    def subscribe(self, callback) -> None:
        """`callback(entry)` after every write — the History view refreshes from it."""
        self._listeners.append(callback)

    def unsubscribe(self, callback) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def add(self, operation: str, message=None, *, game: str = "", source: str = "",
            destination: str = "", result: str = "", duration: float | None = None,
            error: str = "") -> dict:
        """`message` is a `messages.msg()` dict or a plain string."""
        entry = {
            "timestamp": time.time(),
            "operation": str(operation or ""),
            "game": str(game or ""),
            "source": str(source or ""),
            "destination": str(destination or ""),
            "result": str(result or ""),
            "duration": round(float(duration), 3) if duration is not None else None,
            "error": str(error or ""),
        }
        if isinstance(message, dict):
            entry["code"] = str(message.get("code") or "")
            entry["params"] = message.get("params") or {}
            entry["message"] = str(message.get("message") or "")
        elif message is not None:
            entry["message"] = str(message)
        try:
            with _LOCK:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                self._rotate_if_needed()
                with open(self.path, "a", encoding="utf-8") as fh:
                    # default=str: parameters come from exceptions and paths; an
                    # unserializable value must keep its content, not kill the write
                    fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass
        for listener in list(self._listeners):
            try:
                listener(entry)
            except Exception:  # a broken listener must not break logging
                pass
        return entry

    def _rotate_if_needed(self) -> None:
        try:
            if os.path.getsize(self.path) < self.max_bytes:
                return
        except OSError:
            return
        os.replace(self.path, self.path + ".1")

    def _lines(self, path: str) -> list:
        try:
            # errors="replace": one damaged byte must not take the whole log away;
            # that line simply fails to parse
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.readlines()
        except OSError:
            return []

    def tail(self, count: int = 200) -> list:
        """Newest first."""
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 200
        if count <= 0:
            return []
        out = []
        for path in (self.path, self.path + ".1"):
            for line in reversed(self._lines(path)):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    out.append(entry)
                if len(out) >= count:
                    return out
        return out


def format_entry(entry: dict) -> str:
    """One human-readable line, e.g. `19:42:31  Cyberpunk 2077  USB → PC  RESTORE  SUCCESS  1.8 s`."""
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(entry.get("timestamp"))))
    except (TypeError, ValueError):
        stamp = "?"
    parts = [stamp]
    if entry.get("game"):
        parts.append(entry["game"])
    if entry.get("source") or entry.get("destination"):
        parts.append("%s → %s" % (entry.get("source") or "?", entry.get("destination") or "?"))
    if entry.get("operation"):
        parts.append(str(entry["operation"]).upper())
    if entry.get("result"):
        parts.append(str(entry["result"]).upper())
    if entry.get("duration") is not None:
        parts.append("%.1f s" % entry["duration"])
    line = "  ".join(parts)
    detail = entry.get("error") or entry.get("message")
    return line + ("  — " + detail if detail else "")
