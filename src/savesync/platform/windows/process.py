"""Running-game check (plan §18.3).

Asked only while a synchronization is about to modify saves — never a permanent
monitor. One process snapshot (psutil) serves a whole cycle. A title maps to
processes through hints: install folder names and launch executables from the
Ludusavi manifest, plus process names the user entered for the game.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from ..interfaces import ProcessChecker

# Executable names too common to identify one game on their own.
GENERIC_EXES = frozenset({
    "game.exe", "launcher.exe", "start.exe", "setup.exe", "play.exe", "run.exe",
    "main.exe", "client.exe", "app.exe", "unitycrashhandler64.exe",
    "unitycrashhandler32.exe", "crashreporter.exe", "python.exe", "java.exe",
    "javaw.exe", "dosbox.exe", "scummvm.exe", "retroarch.exe",
})


@dataclass
class ProcessHints:
    exe_names: set = field(default_factory=set)
    install_dirs: set = field(default_factory=set)

    def merge(self, other: "ProcessHints") -> "ProcessHints":
        return ProcessHints(self.exe_names | other.exe_names,
                            self.install_dirs | other.install_dirs)


def _psutil_processes():
    import psutil
    out = []
    for proc in psutil.process_iter(["name", "exe"]):
        info = proc.info
        out.append(((info.get("name") or ""), (info.get("exe") or "")))
    return out


def _norm(path: str) -> str:
    return re.sub(r"[\\/]+", "/", path or "").lower()


class WindowsProcessChecker(ProcessChecker):
    def __init__(self, hints=None, lister=None):
        """`hints(title) -> ProcessHints`; `lister() -> [(name, exe_path)]`."""
        self.hints = hints or (lambda title: ProcessHints())
        self.lister = lister or _psutil_processes
        self._snapshot = None

    def begin_cycle(self) -> None:
        self._snapshot = self._list()

    def end_cycle(self) -> None:
        self._snapshot = None

    def _list(self):
        return [(name.lower(), _norm(exe)) for name, exe in self.lister()]

    def is_running(self, title: str) -> bool:
        hints = self.hints(title)
        exes = {e.lower() for e in hints.exe_names if e}
        exes = {e for e in exes if e not in GENERIC_EXES}
        dirs = ["/" + _norm(d).strip("/") + "/" for d in hints.install_dirs if d and d.strip()]
        if not exes and not dirs:
            return False
        processes = self._snapshot if self._snapshot is not None else self._list()
        for name, exe in processes:
            if name in exes or (exe and os.path.basename(exe) in exes):
                return True
            if exe and any(d in exe for d in dirs):
                return True
        return False
