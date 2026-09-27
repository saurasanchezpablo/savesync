"""The production AppController with the REAL Ludusavi on the fake USB: the
engine comes from the controller's own factory (USB copy of Ludusavi, private
configuration, redirects), exactly as on a user's PC."""
import os
import shutil

import pytest

from savesync.app import AppController, TrayState
from savesync.core.paths import AppPaths
from savesync.core.state import GameState as S
from savesync.platform.factory import Platform
from savesync.platform.fake import (FakeFileWatcher, FakeProcessChecker,
                                    FakeRemovableMediaDetector, FakeShutdownIntegration,
                                    FakeSingleInstance, FakeStartupManager, RecordingNotifier,
                                    fake_drive)
from support import read, write

pytestmark = pytest.mark.ludusavi


def _config(config_dir, games):
    os.makedirs(config_dir, exist_ok=True)
    lines = ["manifest:", "  enable: false", "customGames:"]
    for title, path in games.items():
        lines += ['  - name: "%s"' % title, "    files:", '      - "%s"' % path]
    with open(os.path.join(config_dir, "config.yaml"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


def _pc(tmp_path, name, drive, games, qtbot=None):
    paths = AppPaths(str(tmp_path / ("appdata-" + name)))
    platform = Platform(FakeRemovableMediaDetector([drive]), FakeProcessChecker(),
                        FakeStartupManager(), FakeShutdownIntegration(), FakeFileWatcher(),
                        FakeSingleInstance("real-" + name))
    controller = AppController(paths, platform, RecordingNotifier(), synchronous=True,
                               home=str(tmp_path / "Users" / "player"))
    config_dir = str(tmp_path / ("ludusavi-config-" + name))
    _config(config_dir, games)
    controller.config.update(ludusavi_config_dir=config_dir, sync_on_usb_connect=False)
    return controller


@pytest.fixture
def usb_with_ludusavi(tmp_path, ludusavi_path):
    root = tmp_path / "usb"
    root.mkdir()
    drive = fake_drive(str(root))
    exe = os.path.join(drive.ludusavi_dir, os.path.basename(ludusavi_path))
    os.makedirs(drive.ludusavi_dir)
    shutil.copy2(ludusavi_path, exe)
    return drive


def test_portable_ludusavi_from_the_usb_round_trip(tmp_path, usb_with_ludusavi):
    drive = usb_with_ludusavi
    save_dir = tmp_path / "Users" / "player" / "AppData" / "Hades"
    games = {"Hades": str(save_dir)}
    a = _pc(tmp_path, "A", drive, games)
    a.register_usb(drive)
    a.start()
    write(str(save_dir / "slot.sav"), "from A")
    a.sync_now()
    report = a.last_report
    assert report.success, report.errors
    assert a.row("Hades").state == S.SYNCED and a.tray_state() == TrayState.GREEN
    assert a._engine.command[0].startswith(drive.ludusavi_dir), "Ludusavi ran from the USB"
    a.stop()

    shutil.rmtree(save_dir)
    b = _pc(tmp_path, "B", drive, games)
    b.register_usb(drive)
    b.start()
    b.sync_now()
    assert b.row("Hades").state == S.FIRST_SYNC
    b.use_usb("Hades")
    assert read(str(save_dir / "slot.sav")) == "from A"
    b.sync_now()
    assert b.row("Hades").state == S.SYNCED
    b.stop()


@pytest.mark.skipif(os.name == "nt", reason="simulates user profiles through $HOME")
def test_different_username_through_manage_redirect(tmp_path, usb_with_ludusavi, monkeypatch):
    """Like a manifest entry, the custom game uses <home>: Ludusavi resolves it to
    the CURRENT user, while the backup records the other user's absolute path."""
    drive = usb_with_ludusavi
    games = {"Game": "<home>/AppData/Game"}
    alice_home = tmp_path / "Users" / "alice"
    monkeypatch.setenv("HOME", str(alice_home))
    a = _pc(tmp_path, "A", drive, games)
    a.register_usb(drive)
    a.start()
    write(str(alice_home / "AppData" / "Game" / "s.sav"), "alice")
    a.sync_now()
    assert a.row("Game").state == S.SYNCED, a.last_report.errors
    a.stop()
    stash = tmp_path / "pc-a-profile"
    shutil.move(str(alice_home), str(stash))  # that profile does not exist on PC B

    player_home = tmp_path / "Users" / "player"
    player_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(player_home))
    b = _pc(tmp_path, "B", drive, games)
    b.register_usb(drive)
    b.start()
    b.sync_now()
    row = b.row("Game")
    assert row.state == S.MISSING_LOCAL_PATH and "user profile" in row.restore_problem
    b.add_redirect(recorded=str(alice_home), local=str(player_home))
    b.sync_now()
    assert b.row("Game").state == S.FIRST_SYNC
    got = []
    b.actionFinished.connect(lambda name, result: got.append(result))
    b.use_usb("Game")
    assert got and got[-1].state == S.SYNCED, (got[-1].message, b.config.get("redirects"))
    assert read(str(player_home / "AppData" / "Game" / "s.sav")) == "alice"
    b.sync_now()
    assert b.row("Game").state == S.SYNCED, "bidirectional: B's scan matches the USB paths"
    # progress on B travels back to A under A's own paths
    write(str(player_home / "AppData" / "Game" / "s.sav"), "bob played")
    b.sync_now()
    assert b.row("Game").state == S.SYNCED
    b.stop()
    monkeypatch.setenv("HOME", str(alice_home))
    shutil.move(str(stash), str(alice_home))  # back on PC A
    a.start()
    a.sync_now()
    assert a.row("Game").state == S.SYNCED
    assert read(str(alice_home / "AppData" / "Game" / "s.sav")) == "bob played"
    a.stop()
