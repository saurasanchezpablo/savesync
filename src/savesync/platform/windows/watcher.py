"""Save-path watcher (plan §16).

Watches only the directories that hold known save files — never the whole
library — and reports "something changed under this save folder" after a quiet
period, coalescing bursts of events. It does not decide whether a meaningful
change happened; a later synchronization validates that through Ludusavi and the
fingerprint. On Windows watchdog uses ReadDirectoryChangesW.

A save folder that does not exist yet (or was deleted) is watched through its
nearest existing ancestor, non-recursively, and the watch is re-armed when it
appears again.
"""
from __future__ import annotations

import os
import threading

from ..interfaces import FileWatcher


def watch_roots(paths) -> dict:
    """{directory to watch: recursive?} for a list of save files/folders."""
    wanted = set()
    for path in paths or []:
        path = os.path.abspath(str(path))
        wanted.add(path if os.path.isdir(path) else os.path.dirname(path))
    roots = {}
    for folder in sorted(wanted):
        if os.path.isdir(folder):
            roots[folder] = True
            continue
        ancestor = folder
        while not os.path.isdir(ancestor):
            parent = os.path.dirname(ancestor)
            if parent == ancestor:
                ancestor = None
                break
            ancestor = parent
        if ancestor is not None:
            roots.setdefault(ancestor, False)
    # a recursive root already covers everything below it
    recursive = [r for r, rec in roots.items() if rec]
    out = {}
    for root, rec in roots.items():
        covered = any(root != r and (root + os.sep).startswith(r + os.sep) for r in recursive)
        if not covered:
            out[root] = rec
    return out


class _Handler:
    def __init__(self, owner):
        self.owner = owner

    def dispatch(self, event):
        for attr in ("src_path", "dest_path"):
            path = getattr(event, attr, None)
            if path:
                self.owner._event(os.fsdecode(path))


class WatchdogFileWatcher(FileWatcher):
    def __init__(self, debounce: float = 1.5, observer_factory=None):
        self.debounce = debounce
        self.observer_factory = observer_factory
        self._observer = None
        self._paths = []
        self._callback = None
        self._folders = []
        self._roots = {}
        self._pending = set()
        self._lock = threading.Lock()
        self._timer = None

    def _new_observer(self):
        if self.observer_factory is not None:
            return self.observer_factory()
        from watchdog.observers import Observer
        return Observer()

    def watch(self, paths, callback) -> None:
        self.stop()
        self._paths = list(paths or [])
        self._callback = callback
        self._folders = sorted({os.path.abspath(p) if os.path.isdir(p) else
                                os.path.dirname(os.path.abspath(p)) for p in self._paths})
        self._arm()

    def _arm(self) -> None:
        self._roots = watch_roots(self._paths)
        if not self._roots:
            return
        observer = self._new_observer()
        handler = _Handler(self)
        for root, recursive in self._roots.items():
            try:
                observer.schedule(handler, root, recursive=recursive)
            except OSError:
                continue
        observer.daemon = True
        observer.start()
        self._observer = observer

    def _relevant(self, path: str):
        """The watched save folder `path` belongs to, or None."""
        path = os.path.abspath(path)
        for folder in self._folders:
            if path == folder or path.startswith(folder + os.sep) or \
                    (folder + os.sep).startswith(path + os.sep):
                return folder
        return None

    def _event(self, path: str) -> None:
        if self._roots.get(os.path.abspath(path)) is False:
            # "this ancestor folder was modified" says nothing about the save
            # folder below it; its creation arrives as its own event
            return
        folder = self._relevant(path)
        if folder is None:
            return
        with self._lock:
            self._pending.add(folder)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce, self.flush)
            self._timer.daemon = True
            self._timer.start()

    def flush(self) -> None:
        """Deliver the coalesced changes (called by the debounce timer)."""
        with self._lock:
            pending, self._pending = sorted(self._pending), set()
            self._timer = None
        callback = self._callback
        if callback is not None:
            for folder in pending:
                try:
                    callback(folder)
                except Exception:
                    pass
        # folders removed or recreated change what can be watched
        if watch_roots(self._paths) != self._roots and self._callback is not None:
            self._restart()

    def _restart(self) -> None:
        observer, self._observer = self._observer, None
        if observer is not None:
            observer.stop()
            if observer is not threading.current_thread():
                try:
                    observer.join(timeout=2)
                except RuntimeError:
                    pass
        self._arm()

    def stop(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._pending = set()
        observer, self._observer = self._observer, None
        if observer is not None:
            observer.stop()
            try:
                observer.join(timeout=2)
            except RuntimeError:
                pass
        self._callback = None

    @property
    def roots(self) -> dict:
        return dict(self._roots)
