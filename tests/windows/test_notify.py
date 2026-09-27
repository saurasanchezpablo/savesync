import sys

import pytest

from savesync.platform.fake import FakeSingleInstance
from savesync.platform.windows.instance import ERROR_ALREADY_EXISTS, NamedMutexGuard
from savesync.platform.windows.notify import TrayNotificationProvider


class FakeTray:
    def __init__(self):
        self.shown = []

    def showMessage(self, title, message, icon, timeout):
        self.shown.append((title, message))


def test_notifications_go_to_the_tray(qapp):
    tray = FakeTray()
    provider = TrayNotificationProvider(tray)
    provider.notify("Save Sync", "✓ 126 games synchronized")
    assert provider.sent == [("Save Sync", "✓ 126 games synchronized")]


def test_notifier_without_tray_does_not_fail():
    provider = TrayNotificationProvider()
    provider.notify("a", "b")
    assert provider.sent == [("a", "b")]


class FakeMutexApi:
    existing = set()

    def create(self, name):
        existed = name in self.existing
        self.existing.add(name)
        return object(), existed

    def close(self, handle):
        pass


def test_named_mutex_single_instance():
    FakeMutexApi.existing = set()
    first = NamedMutexGuard("Local\\SaveSyncTest", api=FakeMutexApi())
    second = NamedMutexGuard("Local\\SaveSyncTest", api=FakeMutexApi())
    assert first.acquire() is True
    assert second.acquire() is False


def test_fake_single_instance():
    a, b = FakeSingleInstance("t1"), FakeSingleInstance("t1")
    assert a.acquire() and not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


@pytest.mark.windows_only
@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")
def test_real_named_mutex():
    first = NamedMutexGuard("Local\\SaveSync-pytest")
    second = NamedMutexGuard("Local\\SaveSync-pytest")
    try:
        assert first.acquire() and not second.acquire()
    finally:
        first.release()
    assert second.acquire()
    second.release()


def test_lock_file_single_instance(tmp_path, qapp):
    from savesync.platform.fallback import LockFileGuard
    a = LockFileGuard(str(tmp_path / "instance.lock"))
    assert a.acquire()
    a.release()
