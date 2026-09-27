"""Synchronization orchestrator on the fake USB with the FakeLudusavi simulation."""
import os
import time

import pytest

from savesync.core.engine import COMMENT_INCOMPLETE, SyncLocked
from savesync.core.state import GameState as S
from savesync.core.state import Outcome as O
from savesync.core.sync import CancelToken, corrupt_usb_games
from world import World


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.fixture
def pc(world):
    pc = world.pc("A")
    pc.register()
    return pc


def _by_title(report):
    return {g.title: g for g in report.games}


def test_refuses_without_registered_usb(world):
    pc = world.pc("A")
    report = pc.sync()
    assert report.errors[0]["code"] == "usb_not_registered"
    assert pc.ludusavi.calls == [], "Ludusavi is never called for an untrusted drive"


def test_new_local_game_is_uploaded_and_baselined(world, pc):
    world.write_save("Hades", "v1")
    report = pc.sync()
    game = _by_title(report)["Hades"]
    assert game.outcome == O.BACKED_UP and game.state == S.SYNCED
    rec = pc.record("Hades")
    assert rec["last_synced_backup"] == world.usb_versions("Hades")[-1]["when"]
    assert rec["local_fingerprint"].startswith("sfp1:")
    assert rec["save_paths"] == [world.save_path("Hades")]
    assert report.success


def test_second_sync_is_synced_and_does_nothing(world, pc):
    world.write_save("Hades", "v1")
    pc.sync()
    report = pc.sync()
    assert _by_title(report)["Hades"].state == S.SYNCED
    assert _by_title(report)["Hades"].outcome == O.NOTHING
    assert len(world.usb_versions("Hades")) == 1


def test_local_change_goes_to_usb(world, pc):
    world.write_save("Hades", "v1")
    pc.sync()
    world.write_save("Hades", "v2")
    analysis = pc.service.analyze(world.drive)
    assert _by_title(analysis)["Hades"].state == S.LOCAL_NEWER
    assert len(world.usb_versions("Hades")) == 1, "analysis never writes"
    report = pc.sync()
    assert _by_title(report)["Hades"].outcome == O.BACKED_UP
    assert len(world.usb_versions("Hades")) == 2


def test_usb_newer_is_restored_after_safety_snapshot(world, pc):
    world.write_save("Hades", "v1")
    pc.sync()
    b = world.switch_pc("B")
    b.register()
    b_report = b.sync()
    assert _by_title(b_report)["Hades"].state == S.MISSING_LOCAL_PATH or \
        _by_title(b_report)["Hades"].state == S.FIRST_SYNC
    # PC B has no saves: first sync offers the USB version
    result = b.service.use_usb(world.drive, "Hades")
    assert result.outcome == O.RESTORED and world.read_save("Hades") == "v1"
    world.write_save("Hades", "v2 from B")
    b.sync()
    a = world.switch_pc("A")
    assert _by_title(a.service.analyze(world.drive))["Hades"].state == S.USB_NEWER
    report = a.sync()
    game = _by_title(report)["Hades"]
    assert game.outcome == O.RESTORED and game.state == S.SYNCED
    assert world.read_save("Hades") == "v2 from B"
    safety = a.record("Hades")["last_safety"]
    assert os.path.isdir(safety), "the PC state before the restore was kept"
    assert a.record("Hades")["last_synced_backup"] == world.usb_versions("Hades")[-1]["when"]
    assert _by_title(a.sync())["Hades"].state == S.SYNCED


def test_both_changed_is_a_conflict_and_nothing_moves(world, pc):
    world.write_save("Hades", "v1")
    pc.sync()
    b = world.switch_pc("B")
    b.register()
    b.service.use_usb(world.drive, "Hades")
    world.write_save("Hades", "B")
    b.sync()
    world.switch_pc("A")
    world.write_save("Hades", "A")
    versions_before = world.usb_versions("Hades")
    report = pc.sync()
    game = _by_title(report)["Hades"]
    assert game.state == S.CONFLICT and game.outcome == O.SKIPPED
    assert world.read_save("Hades") == "A"
    assert world.usb_versions("Hades") == versions_before
    assert pc.record("Hades")["conflict"] is True
    assert not report.success


def test_first_sync_when_both_sides_have_unrelated_data(world):
    a = world.pc("A")
    a.register()
    world.write_save("Hades", "A")
    a.sync()
    b = world.switch_pc("B")
    b.register()
    world.write_save("Hades", "B's own save")
    report = b.sync()
    assert _by_title(report)["Hades"].state == S.FIRST_SYNC
    assert world.read_save("Hades") == "B's own save"
    assert b.record("Hades")["last_synced_backup"] == ""


def test_first_sync_with_identical_data_records_a_baseline(world):
    a = world.pc("A")
    a.register()
    world.write_save("Hades", "same")
    a.sync()
    b = world.switch_pc("B")
    b.register()
    world.write_save("Hades", "same")
    game = _by_title(b.sync())["Hades"]
    assert game.state == S.SYNCED and game.outcome == O.BASELINE
    assert b.record("Hades")["last_synced_backup"]


def test_usb_only_game_with_foreign_profile_path_is_missing_local_path(world, pc, tmp_path):
    # a backup whose files live under another user's profile
    other = os.path.join(world.root, "live", "Users", "alice", "AppData", "Game", "s.sav")
    world.games["Alice Game"] = [os.path.dirname(other)]
    from support import write
    write(other, "alice")
    pc.ludusavi.games = world.games
    pc.sync()
    import shutil
    shutil.rmtree(os.path.join(world.root, "live", "Users", "alice"))
    b = world.switch_pc("B")
    b.register()
    b.ludusavi.games = world.games
    game = _by_title(b.sync())["Alice Game"]
    assert game.state == S.MISSING_LOCAL_PATH
    assert game.message["code"] == "state_missing_local_path"
    refused = b.service.use_usb(world.drive, "Alice Game")
    assert refused.state == S.MISSING_LOCAL_PATH and not os.path.exists(other)


def test_running_game_is_left_untouched(world, pc):
    world.write_save("Hades", "v1")
    pc.process.running.add("Hades")
    report = pc.sync()
    assert _by_title(report)["Hades"].state == S.RUNNING
    assert world.usb_versions("Hades") == []


def test_excluded_game_is_skipped_by_automatic_sync(world, pc):
    world.write_save("Hades", "v1")
    pc.registry.upsert({"title": "Hades", "excluded": True})
    assert "Hades" not in _by_title(pc.sync())
    assert world.usb_versions("Hades") == []
    assert _by_title(pc.sync(titles=["Hades"]))["Hades"].outcome == O.BACKED_UP


def test_touch_without_change_keeps_synced(world, pc):
    path = world.write_save("Hades", "v1")
    pc.sync()
    os.utime(path, (1, 1))
    assert _by_title(pc.sync())["Hades"].state == S.SYNCED


def test_local_saves_deleted_after_sync_are_not_restored_silently(world, pc):
    path = world.write_save("Hades", "v1")
    pc.sync()
    os.remove(path)
    game = _by_title(pc.sync())["Hades"]
    assert game.state == S.MISSING_LOCAL_PATH
    assert game.message["code"] == "state_local_saves_missing"
    assert not os.path.exists(path)


def test_baseline_belongs_to_one_usb(world, pc, make_usb):
    world.write_save("Hades", "v1")
    pc.sync()
    other_root = os.path.join(world.root, "usb2")
    os.makedirs(other_root)
    from savesync.core import usb as usbmod
    from savesync.platform.fake import fake_drive
    other = fake_drive(other_root, serial="OTHER")
    pc.register(other)
    game = _by_title(pc.service.sync(other))["Hades"]
    assert game.outcome == O.BACKED_UP, "a new USB has no copy: upload, never trust old baseline"


def test_corrupt_usb_index_is_an_error_not_an_empty_usb(world, pc):
    world.write_save("Hades", "v1")
    pc.sync()
    mapping = os.path.join(world.drive.backups_dir, "Hades", "mapping.yaml")
    with open(mapping, "w") as fh:
        fh.write("garbage: [")
    world.write_save("Hades", "v2")
    game = _by_title(pc.sync())["Hades"]
    assert game.state == S.ERROR and game.message["code"] == "usb_backup_corrupt"
    with open(mapping) as fh:
        assert fh.read() == "garbage: [", "the damaged index was not overwritten"


def test_corrupt_usb_games_reads_mapping_names(tmp_path):
    root = tmp_path / "b"
    (root / "Game_ X").mkdir(parents=True)
    (root / "Game_ X" / "mapping.yaml").write_text('---\nname: "Game: X"\n')
    (root / "Broken").mkdir()
    (root / "Broken" / "mapping.yaml").write_text("garbage")
    (root / "loose-folder").mkdir()
    assert corrupt_usb_games(str(root), {"Game: X": []}) == {
        "Broken": str(root / "Broken" / "mapping.yaml")}


def test_unknown_ludusavi_title_is_flagged_once(world, pc):
    world.write_save("Hades", "v1")
    pc.registry.upsert({"title": "Hades", "ludusavi_unknown": True})
    game = _by_title(pc.sync())["Hades"]
    assert game.state == S.ERROR and game.message["code"] == "ludusavi_unknown_title"


def test_scan_failure_changes_nothing(world, pc):
    world.write_save("Hades", "v1")
    pc.ludusavi.hooks.append(lambda argv: (1, "", "boom") if "--preview" in argv else None)
    report = pc.sync()
    assert report.errors[0]["code"] == "local_scan_failed"
    assert report.games == [] and world.usb_versions("Hades") == []


def test_usb_listing_failure_changes_nothing(world, pc):
    world.write_save("Hades", "v1")
    pc.ludusavi.unreadable_usb = True
    report = pc.sync()
    assert report.errors[0]["code"] == "usb_backups_unreadable"
    assert not report.success


def test_second_simultaneous_sync_is_refused(world, pc):
    world.write_save("Hades", "v1")
    with pc.engine.lock():
        report = pc.sync()
    assert report.errors[0]["code"] == "sync_locked"
    assert world.usb_versions("Hades") == []


def test_cancel_before_operations_marks_nothing_synced(world, pc):
    world.write_save("Hades", "v1")
    world.write_save("Celeste", "c1")
    token = CancelToken()
    token.cancel("usb_removed")
    report = pc.sync(cancel=token)
    assert not report.completed
    assert report.errors[-1]["code"] == "usb_removed_during_sync"
    assert all(g.outcome == O.SKIPPED for g in report.games)
    assert pc.record("Hades")["last_synced_backup"] == ""
    assert pc.record("Hades")["state"] == "local_newer", "still pending"


def test_deadline_stops_new_operations(world, pc):
    world.write_save("Hades", "v1")
    report = pc.sync(deadline=time.monotonic() + 1)
    assert not report.completed and report.errors[-1]["code"] == "sync_deadline"
    assert world.usb_versions("Hades") == []


def test_interrupted_backup_versions_are_marked_incomplete_on_reconnect(world, pc):
    world.write_save("Hades", "v1")
    pc.sync()
    good = world.usb_versions("Hades")[-1]["when"]
    world.write_save("Hades", "v2")
    # the USB vanished right after Ludusavi wrote a version: nothing validated it
    real = pc.engine.usb_backup

    def interrupted(title, usb_path, *a, **k):
        pc.ludusavi(["ludusavi", "backup", "--force", "--api", "--path", usb_path,
                     "--full-limit", "2", title])
        from savesync.core.engine import OpResult
        return OpResult(False, problem="USB removed")
    pc.engine.usb_backup = interrupted
    report = pc.sync()
    assert _by_title(report)["Hades"].outcome == O.FAILED
    assert pc.record("Hades")["pending_op"]["kind"] == "backup"
    assert pc.record("Hades")["last_synced_backup"] == good
    pc.engine.usb_backup = real
    pc.ludusavi.calls.clear()
    report = pc.sync()
    marked = [c for c in pc.ludusavi.calls if "edit" in c and COMMENT_INCOMPLETE in c]
    assert marked, "the unvalidated version was marked before anything else"
    # Ludusavi then confirms the live data equals that version, so it is
    # re-validated rather than left incomplete forever
    assert _by_title(report)["Hades"].outcome in (O.BACKED_UP, O.BASELINE)
    whens = [v["when"] for v in world.usb_versions("Hades")]
    assert good in whens
    assert pc.record("Hades")["last_synced_backup"] == whens[-1]
    assert pc.record("Hades")["pending_op"] is None


def test_multiple_games_in_one_cycle(world, pc):
    for i in range(12):
        world.games["Game %02d" % i] = [world.game_dir("Game %02d" % i)]
        world.write_save("Game %02d" % i, "s%d" % i)
    pc.ludusavi.games = world.games
    report = pc.sync()
    assert report.count(O.BACKED_UP) == 12 and report.success
    assert pc.process.cycles == 1, "one process snapshot per cycle"


def test_sync_logs_operations(world, pc):
    world.write_save("Hades", "v1")
    pc.sync()
    ops = [(e["operation"], e["result"]) for e in pc.log.tail(10)]
    assert ("backup", "success") in ops and ("sync", "success") in ops
    backup = next(e for e in pc.log.tail(10) if e["operation"] == "backup")
    assert backup["source"] == "PC" and backup["destination"] == "USB"
    assert backup["duration"] is not None


def test_use_pc_resolves_conflict(world, pc):
    world.write_save("Hades", "v1")
    pc.sync()
    b = world.switch_pc("B")
    b.register()
    b.service.use_usb(world.drive, "Hades")
    world.write_save("Hades", "B")
    b.sync()
    world.switch_pc("A")
    world.write_save("Hades", "A")
    pc.sync()
    result = pc.service.use_pc(world.drive, "Hades")
    assert result.outcome == O.BACKED_UP
    assert pc.record("Hades")["conflict"] is False
    assert _by_title(pc.sync())["Hades"].state == S.SYNCED
    world.switch_pc("B")
    b.sync()
    assert world.read_save("Hades") == "A"


def test_use_usb_resolves_conflict_with_snapshot(world, pc):
    world.write_save("Hades", "v1")
    pc.sync()
    b = world.switch_pc("B")
    b.register()
    b.service.use_usb(world.drive, "Hades")
    world.write_save("Hades", "B")
    b.sync()
    world.switch_pc("A")
    world.write_save("Hades", "A")
    pc.sync()
    result = pc.service.use_usb(world.drive, "Hades")
    assert result.outcome == O.RESTORED and world.read_save("Hades") == "B"
    assert _by_title(pc.sync())["Hades"].state == S.SYNCED
    # the overwritten PC version is recoverable
    assert pc.service.recover_safety("Hades").outcome == O.RESTORED
    assert world.read_save("Hades") == "A"


def test_restore_older_version_becomes_newest(world, pc):
    world.write_save("Hades", "v1")
    pc.sync()
    first = world.usb_versions("Hades")[-1]["name"]
    world.write_save("Hades", "v2")
    pc.sync()
    result = pc.service.restore_version(world.drive, "Hades", first)
    assert result.outcome == O.RESTORED and world.read_save("Hades") == "v1"
    assert _by_title(pc.sync())["Hades"].state == S.SYNCED
    assert world.read_save("Hades") == "v1", "the next cycle does not bring v2 back"


def test_explicit_actions_refuse_running_games(world, pc):
    world.write_save("Hades", "v1")
    pc.sync()
    pc.process.running.add("Hades")
    assert pc.service.use_usb(world.drive, "Hades").state == S.RUNNING
    assert pc.service.use_pc(world.drive, "Hades").state == S.RUNNING


def test_usb_newer_makes_the_pc_equal_to_the_usb_version(world, pc):
    """Ludusavi never deletes on restore. A save slot deleted on PC B must not
    survive on PC A as a mix of both versions — but it stays recoverable."""
    world.write_save("Hades", "v1")
    world.write_save("Hades", "slot 2", name="slot2.sav")
    pc.sync()
    b = world.switch_pc("B")
    b.register()
    b.service.use_usb(world.drive, "Hades")
    os.remove(world.save_path("Hades", "slot2.sav"))
    world.write_save("Hades", "v2")
    assert next(g for g in b.sync().games if g.title == "Hades").outcome == O.BACKED_UP
    world.switch_pc("A")
    game = _by_title(pc.sync())["Hades"]
    assert game.outcome == O.RESTORED
    assert world.read_save("Hades") == "v2"
    assert world.read_save("Hades", "slot2.sav") is None
    assert _by_title(pc.sync())["Hades"].state == S.SYNCED
    pc.service.recover_safety("Hades")
    assert world.read_save("Hades", "slot2.sav") == "slot 2"
