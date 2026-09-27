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
        # one lock serializes watch/stop/restart and the pending set; the
        # generation makes a timer from before a stop() or re-watch harmless
        self._lock = threading.RLock()
        self._generation = 0
        self._timer = None

    def _new_observer(self):
        if self.observer_factory is not None:
            return self.observer_factory()
        from watchdog.observers import Observer
        return Observer()

    def watch(self, paths, callback) -> None:
        paths = list(paths or [])
        with self._lock:
            if paths == self._paths and self._observer is not None:
                # nothing to re-arm (called after every job): keep pending changes
                self._callback = callback
                return
            undelivered = sorted(self._pending)
            old_callback = self._callback
            retired = self._stop_locked()
            self._paths = paths
            self._callback = callback
            self._folders = sorted({os.path.abspath(p) if os.path.isdir(p) else
                                    os.path.dirname(os.path.abspath(p)) for p in paths})
            self._arm_locked()
        self._retire(retired)
        # changes seen just before the re-watch are delivered, not dropped
        for folder in undelivered:
            self._deliver(old_callback or callback, folder)

    def _arm_locked(self) -> None:
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
        with self._lock:
            if self._observer is None:
                return  # events from an observer being retired
            if self._roots.get(os.path.abspath(path)) is False:
                # "this ancestor folder was modified" says nothing about the save
                # folder below it; its creation arrives as its own event
                return
            folder = self._relevant(path)
            if folder is None:
                return
            self._pending.add(folder)
            if self._timer is not None:
                self._timer.cancel()
            generation = self._generation
            self._timer = threading.Timer(self.debounce, self.flush, args=(generation,))
            self._timer.daemon = True
            self._timer.start()

    @staticmethod
    def _deliver(callback, folder) -> None:
        if callback is None:
            return
        try:
            callback(folder)
        except Exception:
            pass

    def flush(self, generation=None) -> None:
        """Deliver the coalesced changes (called by the debounce timer)."""
        with self._lock:
            if generation is not None and generation != self._generation:
                return  # a timer from before stop()/re-watch
            pending, self._pending = sorted(self._pending), set()
            self._timer = None
            callback = self._callback
        for folder in pending:
            self._deliver(callback, folder)
        retired = None
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            # folders removed or recreated change what can be watched
            if self._callback is not None and watch_roots(self._paths) != self._roots:
                retired, self._observer = self._observer, None
                self._arm_locked()
        self._retire(retired)

    def _stop_locked(self):
        """Detach the observer; the caller stops it AFTER releasing the lock.

        Stopping needs watchdog's internal lock, which the observer thread holds
        while it is inside `_event` waiting for ours — stopping under our lock
        deadlocks (seen in the tests)."""
        self._generation += 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._pending = set()
        observer, self._observer = self._observer, None
        return observer

    @staticmethod
    def _retire(observer) -> None:
        if observer is None:
            return
        observer.stop()
        if observer is not threading.current_thread():
            try:
                observer.join(timeout=2)
            except RuntimeError:
                pass

    def stop(self) -> None:
        with self._lock:
            retired = self._stop_locked()
            self._callback = None
            self._paths = []
        self._retire(retired)

    @property
    def roots(self) -> dict:
        return dict(self._roots)
