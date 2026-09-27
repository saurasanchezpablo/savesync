"""Real Ludusavi against controlled fixtures (plan §26.3).

Run with: pytest tests/integration -v --ludusavi-path=/path/to/ludusavi[.exe]
Games are Ludusavi custom games in a private, offline configuration
(`manifest.enable: false`), so no network and no real game is needed.
"""
import os
import sys
import time

import pytest

from savesync.core import usb as usbmod
from savesync.core.engine import (COMMENT_INCOMPLETE, COMMENT_VALIDATED, LudusaviEngine,
                                  apply_redirects, latest_effective)
from savesync.core.log import EventLog
from savesync.core.paths import AppPaths
from savesync.core.registry import Config, Registry
from savesync.core.state import GameState as S
from savesync.core.state import Outcome as O
from savesync.core.sync import SyncService
from savesync.platform.fake import FakeProcessChecker, fake_drive
from support import read, write

pytestmark = pytest.mark.ludusavi


def _yaml(value):
    return '"%s"' % str(value).replace("\\", "\\\\").replace('"', '\\"')


class RealEnv:
    def __init__(self, tmp_path, exe, games):
        self.tmp = tmp_path
        self.config_dir = str(tmp_path / "ludusavi-config")
        os.makedirs(self.config_dir)
        self.games = {}
        for title, folder in games.items():
            self.games[title] = str(tmp_path / "pc" / folder)
        self.write_config()
        self.app = AppPaths(str(tmp_path / "appdata")).ensure()
        self.engine = LudusaviEngine([exe], self.app.safety, lock_path=self.app.lock,
                                     config_dir=self.config_dir)
        self.usb = str(tmp_path / "usb" / "SaveSync" / "backups")
        os.makedirs(self.usb)

    def write_config(self, extra=""):
        lines = ["manifest:", "  enable: false", "customGames:"]
        for title, path in self.games.items():
            lines += ["  - name: %s" % _yaml(title), "    files:", "      - %s" % _yaml(path)]
        with open(os.path.join(self.config_dir, "config.yaml"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n" + extra)

    def save(self, title, content, name="slot 1.sav"):
        return write(os.path.join(self.games[title], name), content)

    def read(self, title, name="slot 1.sav"):
        path = os.path.join(self.games[title], name)
        return read(path) if os.path.exists(path) else None

    def versions(self, title):
        return (self.engine.backups(self.usb, [title]) or {}).get(title) or []


@pytest.fixture
def env(tmp_path, ludusavi_path):
    return RealEnv(tmp_path, ludusavi_path, {"Game A": "Game A", "Game: B": "Game B"})


def _later():
    time.sleep(1.1)  # Ludusavi names versions by the second


def test_version_is_reported(env):
    ok, text = env.engine.version()
    assert ok and "ludusavi" in text.lower()


def test_new_backup_is_locked_and_validated(env):
    env.save("Game A", "v1")
    result = env.engine.usb_backup("Game A", env.usb)
    assert result.ok and result.changed
    (only,) = env.versions("Game A")
    assert only.locked and only.comment == COMMENT_VALIDATED


def test_unchanged_backup_keeps_identity(env):
    env.save("Game A", "v1")
    first = env.engine.usb_backup("Game A", env.usb)
    again = env.engine.usb_backup("Game A", env.usb)
    assert again.ok and not again.changed and again.when == first.when
    assert len(env.versions("Game A")) == 1


def test_newer_live_data_and_restore(env):
    env.save("Game A", "v1")
    first = env.engine.usb_backup("Game A", env.usb)
    _later()
    env.save("Game A", "v2")
    scan = env.engine.scan(env.usb, ["Game A"])
    assert scan["Game A"].change == "Different"
    second = env.engine.usb_backup("Game A", env.usb)
    assert second.ok and second.when > first.when
    versions = {v.when: v for v in env.versions("Game A")}
    assert versions[second.when].locked and not versions[first.when].locked
    env.save("Game A", "local edit")
    restored = env.engine.usb_restore("Game A", env.usb, backup=first.backup_name)
    assert restored.ok and env.read("Game A") == "v1"


def test_title_with_colon_and_paths_with_spaces(env):
    env.save("Game: B", "b1", name="save file with spaces.dat")
    result = env.engine.usb_backup("Game: B", env.usb)
    assert result.ok
    assert os.path.isdir(os.path.join(env.usb, "Game_ B"))


def test_failed_entry_is_detected_and_version_marked_incomplete(env):
    if sys.platform == "win32" or os.geteuid() == 0:
        pytest.skip("uses POSIX permissions")
    env.save("Game A", "v1")
    good = env.engine.usb_backup("Game A", env.usb)
    _later()
    locked = env.save("Game A", "secret", name="locked.sav")
    env.save("Game A", "v2")
    os.chmod(locked, 0)
    try:
        for _ in range(3):
            result = env.engine.usb_backup("Game A", env.usb, full_limit=2, differential_limit=0)
            assert not result.ok and "failed" in result.problem
            _later()
            env.save("Game A", "v%s" % time.time())
    finally:
        os.chmod(locked, 0o644)
    versions = env.versions("Game A")
    assert good.when in [v.when for v in versions], "rule 7: the validated version survived"
    assert latest_effective(versions).when == good.when
    assert any(v.comment == COMMENT_INCOMPLETE for v in versions)


def test_declared_but_missing_backup_file_fails_restore(env):
    env.save("Game A", "v1")
    result = env.engine.usb_backup("Game A", env.usb)
    for root, _dirs, files in os.walk(os.path.join(env.usb, "Game A")):
        for name in files:
            if name == "slot 1.sav":
                os.remove(os.path.join(root, name))
    env.save("Game A", "local")
    restored = env.engine.usb_restore("Game A", env.usb, backup=result.backup_name)
    assert not restored.ok
    assert env.read("Game A") == "local"


def test_unknown_game_mixed_with_known(env):
    env.save("Game A", "v1")
    env.engine.usb_backup("Game A", env.usb)
    listing = env.engine.backups(env.usb, ["Game A", "Not A Game"])
    assert "Game A" in listing and "Not A Game" in env.engine.unknown_titles
    assert env.engine.scan(env.usb, ["Not A Game"]) == {}


def test_cli_error_and_timeout(env, ludusavi_path):
    broken = LudusaviEngine([ludusavi_path, "--no-such-flag"], env.app.safety,
                            config_dir=env.config_dir)
    assert broken.scan(env.usb) is None
    slow = LudusaviEngine([ludusavi_path], env.app.safety, config_dir=env.config_dir)
    slow.deadline = time.monotonic() - 10  # forces the minimum timeout
    assert slow._timeout(600) == 1.0


def test_multiple_games_scan(env):
    env.save("Game A", "a")
    env.save("Game: B", "b")
    scan = env.engine.scan(env.usb)
    assert {"Game A", "Game: B"} <= set(scan)
    assert all(g.change == "New" for g in scan.values())


def test_retention_keeps_limit_plus_locked(env):
    env.save("Game A", "v0")
    for i in range(5):
        env.save("Game A", "v%d" % i)
        assert env.engine.usb_backup("Game A", env.usb, full_limit=2, differential_limit=0).ok
        _later()
    versions = env.versions("Game A")
    assert 2 <= len(versions) <= 3
    assert versions[-1].locked and sum(v.locked for v in versions) == 1


def test_restore_to_a_different_username_through_redirect(env, tmp_path):
    alice = tmp_path / "Users" / "alice" / "Saves"
    env.games = {"Profile Game": str(alice)}
    env.write_config()
    write(str(alice / "s.sav"), "alice's save")
    assert env.engine.usb_backup("Profile Game", env.usb).ok
    bob = tmp_path / "Users" / "bob" / "Saves"
    import shutil
    shutil.rmtree(tmp_path / "Users" / "alice")
    (tmp_path / "Users" / "bob").mkdir(parents=True)
    preview = env.engine.usb_preview("Profile Game", env.usb)
    from savesync.core.engine import target_path_problem
    home = str(tmp_path / "Users" / "bob")
    assert any(target_path_problem(p, home) for p in preview["files"])
    assert apply_redirects(env.config_dir, [("restore", str(tmp_path / "Users" / "alice"),
                                             str(tmp_path / "Users" / "bob"))])
    preview = env.engine.usb_preview("Profile Game", env.usb)
    assert all(target_path_problem(p, home) is None for p in preview["files"])
    assert env.engine.usb_restore("Profile Game", env.usb).ok
    assert read(str(bob / "s.sav")) == "alice's save"


def test_safety_snapshot_contract(env):
    env.save("Game A", "v1")
    snap = str(env.tmp / "snap")
    assert env.engine.safety_backup_many(["Game A", "Game: B"], snap) == {"Game A": True,
                                                                          "Game: B": None}
    env.save("Game A", "changed")
    assert env.engine.restore_from("Game A", snap).ok
    assert env.read("Game A") == "v1"


def test_full_orchestrator_on_real_ludusavi(env, tmp_path):
    """PC A → USB → PC B round trip with the production orchestrator."""
    drive = fake_drive(str(tmp_path / "usb"))
    identity = usbmod.initialize(drive)

    def service(app):
        config = Config(app.config)
        config.update(**usbmod.registration_fields(drive, identity))
        return SyncService(Registry(app.registry), config, env.engine, FakeProcessChecker(),
                           EventLog(app.events))
    a = service(env.app)
    env.save("Game A", "from A")
    report = a.sync(drive)
    assert report.success and report.count(O.BACKED_UP) == 1
    assert next(g for g in a.sync(drive).games if g.title == "Game A").state == S.SYNCED
    _later()
    env.save("Game A", "A again")
    assert next(g for g in a.sync(drive).games if g.title == "Game A").outcome == O.BACKED_UP

    # PC B: same user paths, no Save Sync state, no saves
    import shutil
    shutil.rmtree(env.games["Game A"])
    b_app = AppPaths(str(tmp_path / "appdata-b")).ensure()
    b_engine = LudusaviEngine(env.engine.command, b_app.safety, lock_path=b_app.lock,
                              config_dir=env.config_dir)
    b_config = Config(b_app.config)
    b_config.update(**usbmod.registration_fields(drive, identity))
    b = SyncService(Registry(b_app.registry), b_config, b_engine, FakeProcessChecker(),
                    EventLog(b_app.events))
    game = next(g for g in b.sync(drive).games if g.title == "Game A")
    assert game.state == S.FIRST_SYNC
    assert b.use_usb(drive, "Game A").outcome == O.RESTORED
    assert env.read("Game A") == "A again"
    assert next(g for g in b.sync(drive).games if g.title == "Game A").state == S.SYNCED


def test_corrupted_mapping_is_reported_by_the_orchestrator(env, tmp_path):
    drive = fake_drive(str(tmp_path / "usb"))
    identity = usbmod.initialize(drive)
    config = Config(env.app.config)
    config.update(**usbmod.registration_fields(drive, identity))
    svc = SyncService(Registry(env.app.registry), config, env.engine, FakeProcessChecker())
    env.save("Game A", "v1")
    svc.sync(drive)
    with open(os.path.join(env.usb, "Game A", "mapping.yaml"), "w") as fh:
        fh.write("garbage: [")
    _later()
    env.save("Game A", "v2")
    game = next(g for g in svc.sync(drive).games if g.title == "Game A")
    assert game.state == S.ERROR and game.message["code"] == "usb_backup_corrupt"


def test_back_to_back_backups_keep_both_versions(env):
    """Without the same-second guard the second full backup overwrote the first."""
    env.save("Game A", "first")
    first = env.engine.usb_backup("Game A", env.usb, full_limit=3, differential_limit=0)
    env.save("Game A", "second")
    second = env.engine.usb_backup("Game A", env.usb, full_limit=3, differential_limit=0)
    assert first.ok and second.ok and first.backup_name != second.backup_name
    env.save("Game A", "local")
    assert env.engine.usb_restore("Game A", env.usb, backup=first.backup_name).ok
    assert env.read("Game A") == "first"
