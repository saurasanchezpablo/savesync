import sys

import pytest

from savesync.platform.windows.startup import VALUE_NAME, WindowsStartupManager, launch_command


class FakeRunKey:
    def __init__(self):
        self.values = {}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)


def test_enable_disable_round_trip():
    key = FakeRunKey()
    manager = WindowsStartupManager(command='"C:\\SaveSync\\SaveSync.exe" --background', run_key=key)
    assert not manager.is_enabled()
    manager.enable()
    assert manager.is_enabled() and key.values[VALUE_NAME].endswith("--background")
    manager.disable()
    manager.disable()  # idempotent
    assert not manager.is_enabled()


def test_launch_command_starts_in_background():
    assert "--background" in launch_command()
    assert launch_command().startswith('"')


@pytest.mark.windows_only
@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")
def test_real_run_key(monkeypatch):
    from savesync.platform.windows import startup
    monkeypatch.setattr(startup, "VALUE_NAME", "SaveSyncTest")
    manager = WindowsStartupManager(command="savesync-test")
    try:
        manager.enable()
        assert manager.is_enabled()
    finally:
        manager.disable()
    assert not manager.is_enabled()
