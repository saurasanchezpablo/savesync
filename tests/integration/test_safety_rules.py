"""The seven mandatory data-safety rules (plan §27), one explicit test each (plus
variants), on the fake USB harness."""
import os
import shutil

import pytest

from savesync.core import usb as usbmod
from savesync.core.engine import COMMENT_VALIDATED
from savesync.core.state import GameState as S
from savesync.core.state import Outcome as O
from savesync.platform.fake import fake_drive
from world import World


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _game(report, title="Hades"):
    return next(g for g in report.games if g.title == title)


def _diverge(world):
    """Hades synced on A, then changed on B (USB newer) — returns PC A."""
    a = world.pc("A")
    a.register()
    world.write_save("Hades", "v1")
    a.sync()
    b = world.switch_pc("B")
    b.register()
    b.service.use_usb(world.drive, "Hades")
    world.write_save("Hades", "v2 from B")
    b.sync()
    world.switch_pc("A")
    return a


# Rule 1 — unknown error: DO NOT mark as synchronized ------------------------------

def test_rule_1_unknown_error_is_never_synchronized(world):
    a = world.pc("A")
    a.register()
    world.write_save("Hades", "v1")
    a.sync()
    world.write_save("Hades", "v2")
    before = dict(a.record("Hades"))
    # Ludusavi returns garbage for the scan
    a.ludusavi.hooks.append(lambda argv: (0, "not json", "") if "--preview" in argv else None)
    report = a.sync()
    assert not report.success and report.errors
    after = a.record("Hades")
    assert after["last_synced_backup"] == before["last_synced_backup"]
    assert after["local_fingerprint"] == before["local_fingerprint"]
    assert after["last_sync_ts"] == before["last_sync_ts"]


def test_rule_1_unreadable_save_is_unknown_not_synced(world):
    a = world.pc("A")
    a.register()
    path = world.write_save("Hades", "v1")
    a.sync()
    a.ludusavi.hooks.append(
        lambda argv: None if "--preview" not in argv else _failed_preview(a, path))
    game = _game(a.sync())
    assert game.state == S.UNKNOWN and not game.ok


def _failed_preview(pc, path):
    import json
    return 0, json.dumps({"games": {"Hades": {"decision": "Processed", "change": "Different",
                                              "files": {path: {"change": "Different", "failed": True}},
                                              "registry": {}}}}), ""


def test_rule_1_failed_backup_leaves_baseline_alone(world):
    a = world.pc("A")
    a.register()
    world.write_save("Hades", "v1")
    a.sync()
    good = a.record("Hades")["last_synced_backup"]
    a.ludusavi.fail_paths.add(world.write_save("Hades", "extra", name="locked.sav"))
    world.write_save("Hades", "v2")
    game = _game(a.sync())
    assert game.outcome == O.FAILED and game.state == S.ERROR
    assert a.record("Hades")["last_synced_backup"] == good
    assert a.record("Hades")["pending_upload"] is True


# Rule 2 — conflict: DO NOT overwrite ------------------------------------------------

def test_rule_2_conflict_overwrites_neither_side(world):
    a = _diverge(world)
    world.write_save("Hades", "A's progress")
    usb_before = world.usb_versions("Hades")
    for _ in range(3):
        assert _game(a.sync()).state == S.CONFLICT
    assert world.read_save("Hades") == "A's progress"
    assert world.usb_versions("Hades") == usb_before


def test_rule_2_first_sync_overwrites_neither_side(world):
    a = world.pc("A")
    a.register()
    world.write_save("Hades", "A")
    a.sync()
    b = world.switch_pc("B")
    b.register()
    world.write_save("Hades", "B")
    usb_before = world.usb_versions("Hades")
    assert _game(b.sync()).state == S.FIRST_SYNC
    assert world.read_save("Hades") == "B" and world.usb_versions("Hades") == usb_before


# Rule 3 — unknown USB: DO NOT trust it ---------------------------------------------

@pytest.mark.parametrize("case", ["no_marker", "other_marker", "cloned_serial", "corrupt_marker"])
def test_rule_3_unknown_usb_is_never_used(world, tmp_path, case):
    a = world.pc("A")
    a.register()
    world.write_save("Hades", "v1")
    stranger_root = tmp_path / "stranger"
    stranger_root.mkdir()
    stranger = fake_drive(str(stranger_root), serial="STRANGER")
    if case == "other_marker":
        usbmod.initialize(stranger)
    elif case == "cloned_serial":
        shutil.copytree(world.drive.savesync_dir, stranger.savesync_dir)
    elif case == "corrupt_marker":
        usbmod.initialize(stranger)
        with open(usbmod.identity_path(stranger), "w") as fh:
            fh.write("{broken")
    a.ludusavi.calls.clear()
    report = a.service.sync(stranger)
    assert report.games == [] and report.errors
    assert report.errors[0]["code"] in ("usb_unknown", "usb_identity_corrupt")
    assert a.ludusavi.calls == []
    assert a.service.use_pc(stranger, "Hades").outcome == O.SKIPPED
    assert a.trials.start(stranger, "Hades").outcome == O.SKIPPED


# Rule 4 — running game: DO NOT modify saves without an explicit decision ------------

def test_rule_4_running_game_is_not_modified(world):
    a = _diverge(world)
    a.process.running.add("Hades")
    game = _game(a.sync())
    assert game.state == S.RUNNING
    assert world.read_save("Hades") == "v1"
    assert a.service.use_usb(world.drive, "Hades").state == S.RUNNING
    assert world.read_save("Hades") == "v1"
    a.process.running.clear()
    assert _game(a.sync()).outcome == O.RESTORED


def test_rule_4_game_started_during_the_cycle_is_left_alone(world):
    a = _diverge(world)
    calls = {"n": 0}

    def starts_later(title):
        calls["n"] += 1
        return calls["n"] > 1  # not running at classification, running at the operation
    a.process.is_running = starts_later
    game = _game(a.sync())
    assert game.state == S.RUNNING and world.read_save("Hades") == "v1"


# Rule 5 — USB removed during operation: stop, record, do not declare success --------

def _remove_usb_on(world, pc, needle):
    hidden = world.drive.drive_letter + ".removed"

    def hook(argv):
        if needle(argv) and os.path.isdir(world.drive.drive_letter):
            os.rename(world.drive.drive_letter, hidden)
            return -1, "", "the device is not ready"
        return None
    pc.ludusavi.hooks.append(hook)
    return lambda: (pc.ludusavi.hooks.clear(), os.rename(hidden, world.drive.drive_letter))


def test_rule_5_usb_removed_during_backup(world):
    a = world.pc("A")
    a.register()
    world.write_save("Hades", "v1")
    world.write_save("Celeste", "c1")
    reconnect = _remove_usb_on(world, a, lambda argv: "--force" in argv and "backup" in argv
                               and "safety" not in " ".join(argv))
    report = a.sync()
    assert not report.completed and not report.success
    assert any(e["code"] == "usb_removed_during_sync" for e in report.errors)
    for title in ("Hades", "Celeste"):
        rec = a.record(title)
        assert rec["last_synced_backup"] == "", "nothing declared synchronized"
    assert a.record("Celeste")["pending_op"] or a.record("Hades")["pending_op"], \
        "the incomplete operation is recorded"
    reconnect()
    report = a.sync()
    assert report.success
    assert {g.outcome for g in report.games} <= {O.BACKED_UP, O.BASELINE}


def test_rule_5_usb_removed_during_restore(world):
    a = _diverge(world)
    newest = world.usb_versions("Hades")[-1]["when"]
    reconnect = _remove_usb_on(world, a, lambda argv: argv[1:2] == ["--try-manifest-update"]
                               and "restore" in argv and "--preview" not in argv
                               and "safety" not in " ".join(argv))
    report = a.sync()
    assert not report.success
    assert a.record("Hades")["last_synced_backup"] != newest
    assert world.read_save("Hades") == "v1", "the PC state was preserved/recovered"
    reconnect()
    assert _game(a.sync()).outcome == O.RESTORED
    assert world.read_save("Hades") == "v2 from B"


def test_rule_5_cancel_token_from_usb_removal(world):
    from savesync.core.sync import CancelToken
    a = world.pc("A")
    a.register()
    world.write_save("Hades", "v1")
    token = CancelToken()
    a.ludusavi.hooks.append(lambda argv: token.cancel("usb_removed") if "--preview" in argv else None)
    report = a.sync(cancel=token)
    assert not report.completed
    assert a.record("Hades")["last_synced_backup"] == ""


# Rule 6 — failed restore: allow recovery of the previous state ----------------------

def test_rule_6_failed_restore_rolls_back_automatically(world):
    a = _diverge(world)
    a.ludusavi.fail_restore_paths.add(world.save_path("Hades"))
    game = _game(a.sync())
    assert game.outcome == O.ROLLED_BACK and game.message["code"] == "restore_rolled_back"
    assert world.read_save("Hades") == "v1"
    assert a.record("Hades")["last_synced_backup"] != world.usb_versions("Hades")[-1]["when"]


def test_rule_6_failed_rollback_keeps_a_manual_recovery_path(world):
    a = _diverge(world)
    path = world.save_path("Hades")
    a.ludusavi.fail_restore_paths.add(path)
    real_restore_from = a.engine.restore_from

    def restore_breaking_file(title, source, backup=None):
        if "safety" in source:
            from savesync.core.engine import OpResult
            return OpResult(False, problem="disk full")
        with open(path, "w") as fh:
            fh.write("half-written garbage")
        return real_restore_from(title, source, backup)
    a.engine.restore_from = restore_breaking_file
    game = _game(a.sync())
    assert game.message["code"] == "restore_rollback_failed"
    pending = a.record("Hades")["pending_op"]
    assert pending["failed"] and os.path.isdir(pending["snapshot"])
    assert pending["snapshot"] in a.service.protected_snapshots()
    a.engine.restore_from = real_restore_from
    a.ludusavi.fail_restore_paths.clear()
    assert a.service.recover_safety("Hades").outcome == O.RESTORED
    assert world.read_save("Hades") == "v1"
    assert a.record("Hades")["pending_op"] is None


def test_rule_6_failed_first_restore_removes_the_partial_files(world):
    a = world.pc("A")
    a.register()
    world.write_save("Hades", "v1")
    world.write_save("Hades", "v1b", name="second.sav")
    a.sync()
    b = world.switch_pc("B")
    b.register()
    b.ludusavi.fail_paths.add(world.save_path("Hades", "second.sav"))
    result = b.service.use_usb(world.drive, "Hades")
    assert result.outcome == O.ROLLED_BACK
    assert world.read_save("Hades") is None, "the PC is back to having no saves"


# Rule 7 — previous USB version is kept until the new one succeeds -------------------

def test_rule_7_previous_version_survives_failed_backups(world):
    a = world.pc("A")
    a.register()
    world.write_save("Hades", "v1")
    a.sync()
    good = world.usb_versions("Hades")[-1]["when"]
    a.ludusavi.fail_paths.add(world.write_save("Hades", "x", name="locked.sav"))
    for i in range(5):
        world.write_save("Hades", "attempt %d" % i)
        assert _game(a.sync()).outcome == O.FAILED
    versions = {v["when"]: v for v in world.usb_versions("Hades")}
    assert good in versions and versions[good]["locked"]
    # PC B restores the validated version, never a partial one
    b = world.switch_pc("B")
    b.register()
    b.service.use_usb(world.drive, "Hades")
    assert world.read_save("Hades") == "v1"


def test_rule_7_old_version_released_only_after_success(world):
    a = world.pc("A")
    a.register()
    world.write_save("Hades", "v1")
    a.sync()
    world.write_save("Hades", "v2")
    a.sync()
    versions = world.usb_versions("Hades")
    assert [v["locked"] for v in versions] == [False, True]
    assert versions[-1]["comment"] == COMMENT_VALIDATED
