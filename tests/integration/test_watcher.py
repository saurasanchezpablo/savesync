"""Save-path watcher with the real watchdog backend (inotify here,
ReadDirectoryChangesW on Windows)."""
import os
import shutil
import threading
import time

from savesync.platform.windows.watcher import WatchdogFileWatcher, watch_roots
from support import write


def _collector():
    seen, event = [], threading.Event()

    def callback(path):
        seen.append(path)
        event.set()
    return seen, event, callback


def test_watch_roots_dedupes_and_falls_back_to_ancestors(tmp_path):
    save = write(str(tmp_path / "Game" / "s.sav"), "x")
    nested = write(str(tmp_path / "Game" / "sub" / "t.sav"), "x")
    missing = str(tmp_path / "NotYet" / "deep" / "s.sav")
    roots = watch_roots([save, nested, missing])
    assert roots == {str(tmp_path / "Game"): True, str(tmp_path): False}


def test_change_is_reported_once_after_a_burst(tmp_path):
    save = write(str(tmp_path / "Game" / "s.sav"), "x")
    watcher = WatchdogFileWatcher(debounce=0.3)
    seen, event, callback = _collector()
    watcher.watch([save], callback)
    try:
        time.sleep(0.2)
        for i in range(20):
            write(save, "burst %d" % i)
        assert event.wait(5)
        time.sleep(0.6)
        assert seen == [str(tmp_path / "Game")], "coalesced into one notification"
    finally:
        watcher.stop()


def test_unrelated_changes_are_ignored(tmp_path):
    save = write(str(tmp_path / "Game" / "s.sav"), "x")
    missing = str(tmp_path / "Other" / "s.sav")
    watcher = WatchdogFileWatcher(debounce=0.2)
    seen, event, callback = _collector()
    watcher.watch([save, missing], callback)
    try:
        time.sleep(0.2)
        write(str(tmp_path / "unrelated.txt"), "noise")
        assert not event.wait(0.8)
    finally:
        watcher.stop()


def test_removed_and_recreated_folder_keeps_being_watched(tmp_path):
    save = write(str(tmp_path / "Game" / "s.sav"), "x")
    watcher = WatchdogFileWatcher(debounce=0.2)
    seen, event, callback = _collector()
    watcher.watch([save], callback)
    try:
        time.sleep(0.2)
        shutil.rmtree(str(tmp_path / "Game"))
        assert event.wait(5)
        event.clear()
        time.sleep(0.5)
        write(save, "recreated")
        assert event.wait(5), "recreation noticed through the ancestor"
        event.clear()
        time.sleep(0.5)
        write(save, "edited after recreation")
        assert event.wait(5), "the recreated folder is watched again"
    finally:
        watcher.stop()


def test_not_yet_existing_save_folder(tmp_path):
    save = str(tmp_path / "Game" / "s.sav")
    watcher = WatchdogFileWatcher(debounce=0.2)
    seen, event, callback = _collector()
    watcher.watch([save], callback)
    try:
        time.sleep(0.2)
        write(save, "first save ever")
        assert event.wait(5)
    finally:
        watcher.stop()


def test_stop_is_idempotent_and_silences(tmp_path):
    save = write(str(tmp_path / "Game" / "s.sav"), "x")
    watcher = WatchdogFileWatcher(debounce=0.1)
    seen, event, callback = _collector()
    watcher.watch([save], callback)
    watcher.stop()
    watcher.stop()
    write(save, "after stop")
    assert not event.wait(0.5)
