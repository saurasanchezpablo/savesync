"""Regression tests for the findings of the independent safety review — one per
finding, each reproducing the failure scenario the review described."""
import json
import os

import pytest

from savesync.core.engine import (COMMENT_INCOMPLETE, COMMENT_VALIDATED, BackupInfo, OpResult,
                                  latest_effective)
from savesync.core.state import GameState as S
from savesync.core.state import Outcome as O
from savesync.platform.windows.process import ProcessHints, WindowsProcessChecker
from support import write
from world import World


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _game(report, title="Hades"):
    return next(g for g in report.games if g.title == title)


def _usb_newer(world):
    """Hades synced on A, then changed on B — PC A is current and USB is newer."""
    a = world.pc("A")
    a.register()
    world.write_save("Hades", "v1")
    a.sync()
    b = world.switch_pc("B")
    b.register()
    b.service.use_usb(world.drive, "Hades")
    world.write_save("Hades", "v2 from B")
    world.write_save("Hades", "b only", name="b-only.sav")
    b.sync()
    world.switch_pc("A")
    return a, b


# 1 — a game launched after the cycle's process snapshot --------------------------

def test_game_started_after_the_snapshot_is_detected_before_writing(world):
    a, _ = _usb_newer(world)
    processes = []
    checker = WindowsProcessChecker(lambda t: ProcessHints(exe_names={"hades.exe"}),
                                    lister=lambda: list(processes))
    a.service.process = checker
    real_restore = a.engine.usb_restore

    def launch_then_restore(*args, **kw):
        return real_restore(*args, **kw)
    real_safety = a.engine.safety_backup_many

    def safety_then_launch(titles, snap):
        out = real_safety(titles, snap)
        processes.append(("hades.exe", r"C:\Games\Hades\Hades.exe"))  # user starts it now
        return out
    a.engine.safety_backup_many = safety_then_launch
    game = _game(a.sync())
    assert game.state == S.RUNNING
    assert world.read_save("Hades") == "v1"


# 2 — damaged index of a title with ":" --------------------------------------------

@pytest.mark.parametrize("damage", [b"", b"\x00" * 64, b"garbage: ["])
def test_damaged_index_of_colon_title_blocks_upload(world, damage):
    world.games["Isaac: Rebirth"] = [world.game_dir("Isaac")]
    a = world.pc("A")
    a.ludusavi.games = world.games
    a.register()
    world.write_save("Isaac", "v1")
    a.sync()
    world.write_save("Isaac", "v2")
    a.sync()
    mapping = os.path.join(world.drive.backups_dir, "Isaac_ Rebirth", "mapping.yaml")
    with open(mapping, "wb") as fh:
        fh.write(damage)
    world.write_save("Isaac", "v3")
    report = a.sync()
    game = _game(report, "Isaac: Rebirth")
    assert game.state == S.ERROR and game.message["code"] == "usb_backup_corrupt"
    with open(mapping, "rb") as fh:
        assert fh.read() == damage, "nothing was written over the damaged index"
    folders = [d for d in os.listdir(os.path.dirname(mapping)) if d.startswith("backup-")]
    assert len(folders) == 2, "no version was pruned"


# 3 — clock skew between PCs ------------------------------------------------------------

def test_newest_version_is_listing_order_not_when():
    old_clock_but_newest = BackupInfo("backup-b", "2026-01-01T09:00:00Z", comment=COMMENT_VALIDATED)
    earlier = BackupInfo("backup-a", "2026-01-01T10:00:00Z", comment=COMMENT_VALIDATED)
    assert latest_effective([earlier, old_clock_but_newest]).name == "backup-b"


def test_pc_with_clock_behind_does_not_lose_progress(world):
    a, b = _usb_newer(world)
    # rewrite B's version `when` one hour into the past, as a PC whose clock is behind would
    data = a.ludusavi.read_mapping(world.drive.backups_dir, "Hades")
    first, newest = data["backups"][0], data["backups"][-1]
    newest["when"] = "2000-01-01T00:00:00.000000000Z"
    a.ludusavi._write_mapping(world.drive.backups_dir, "Hades", data)
    b.registry.upsert({"title": "Hades", "last_synced_backup": newest["when"]})
    game = _game(a.sync())
    assert game.outcome == O.RESTORED and world.read_save("Hades") == "v2 from B"
    world.switch_pc("B")
    assert _game(b.sync()).state == S.SYNCED
    assert world.read_save("Hades") == "v2 from B"


# 4 — the pending-backup repair never marks another PC's version ------------------------

def test_interrupted_upload_repair_only_touches_this_pcs_version(world):
    a = world.pc("A")
    a.register()
    world.write_save("Hades", "v1")
    a.sync()
    world.write_save("Hades", "a2")
    fake = a.ludusavi
    real = a.engine.usb_backup

    def interrupted(title, usb_path, *args, **kw):
        fake(["ludusavi", "backup", "--force", "--api", "--path", usb_path, "--full-limit", "2", title])
        name = fake.read_mapping(usb_path, title)["backups"][-1]["name"]
        return OpResult(False, backup_name=name, problem="USB removed")
    a.engine.usb_backup = interrupted
    a.sync()
    a.engine.usb_backup = real
    mine = a.record("Hades")["pending_op"]["created"]
    b = world.switch_pc("B")
    b.register()
    b.service.use_usb(world.drive, "Hades")
    world.write_save("Hades", "b progress")
    assert _game(b.sync()).outcome == O.BACKED_UP
    theirs = world.usb_versions("Hades")[-1]["name"]
    world.switch_pc("A")
    report = a.sync()
    comments = {v["name"]: v.get("comment") for v in world.usb_versions("Hades")}
    assert comments[theirs] == COMMENT_VALIDATED, "B's validated version untouched"
    assert comments.get(mine) in (COMMENT_INCOMPLETE, None)
    assert _game(report).state == S.CONFLICT, "A's unsent change vs B's newer version"


def test_failed_validation_mark_is_a_failed_upload(world):
    a = world.pc("A")
    a.register()
    world.write_save("Hades", "v1")
    a.ludusavi.hooks.append(
        lambda argv: (1, "", "edit failed") if "edit" in argv and "--lock" in argv else None)
    game = _game(a.sync())
    assert game.outcome == O.FAILED
    assert a.record("Hades")["last_synced_backup"] == ""


# 5 — trial start is exception-safe ---------------------------------------------------

def test_trial_start_failure_after_restore_returns_to_original(world):
    a, _ = _usb_newer(world)
    world.write_save("Hades", "A progress")
    real = a.service.safety.new_snapshot

    def failing(kind):
        if kind == "trial-usb":
            raise OSError("disk full")
        return real(kind)
    a.service.safety.new_snapshot = failing
    result = a.trials.start(world.drive, "Hades")
    assert result.outcome == O.FAILED
    assert world.read_save("Hades") == "A progress"
    assert world.read_save("Hades", "b-only.sav") is None
    assert a.trials.active("Hades") is None
    a.service.safety.new_snapshot = real
    assert _game(a.sync()).state == S.CONFLICT, "never mistaken for synchronized"


def test_trial_recorded_before_the_pc_changes(world):
    a, _ = _usb_newer(world)
    world.write_save("Hades", "A progress")
    seen = {}
    real = a.engine.usb_restore

    def spy(*args, **kw):
        seen["trial"] = a.trials.active("Hades")
        return real(*args, **kw)
    a.engine.usb_restore = spy
    a.trials.start(world.drive, "Hades")
    assert seen["trial"] and seen["trial"]["side"] == "starting"
    assert seen["trial"]["original"] in a.service.protected_snapshots()


# 6 — a restore killed without JSON ---------------------------------------------------

def test_killed_restore_removes_the_files_it_created(world):
    a, _ = _usb_newer(world)
    real = a.engine.usb_restore

    def killed(title, usb_path, backup=None):
        real(title, usb_path, backup)  # files land on the PC…
        return OpResult(False, problem="timeout after 3s")  # …but no JSON came back
    a.engine.usb_restore = killed
    game = _game(a.sync())
    assert game.outcome == O.ROLLED_BACK
    assert world.read_save("Hades") == "v1"
    assert world.read_save("Hades", "b-only.sav") is None


# 7 — USB removal must not disable the local rollback ------------------------------------

def test_rollback_works_after_the_runner_was_cancelled(world):
    a, _ = _usb_newer(world)
    real = a.engine.usb_restore

    def pulled(title, usb_path, backup=None):
        real(title, usb_path, backup)
        a.engine.cancel = lambda: None
        a.ludusavi.hooks.append(lambda argv: None)
        # what the runner does after cancel(): refuse everything
        a.engine._cancelled_calls = True
        return OpResult(False, problem="cancelled: USB removed")

    class Cancellable:
        def __init__(self, inner):
            self.inner, self.cancelled = inner, False

        def __call__(self, argv, timeout=None):
            if self.cancelled:
                return -1, "", "cancelled before start"
            return self.inner(argv, timeout)

        def cancel(self):
            self.cancelled = True

        def reset(self):
            self.cancelled = False
    runner = Cancellable(a.ludusavi)
    a.engine.runner = runner

    def restore_then_cancel(title, usb_path, backup=None):
        out = real(title, usb_path, backup)
        runner.cancel()
        return OpResult(False, created=out.created, problem="USB removed")
    a.engine.usb_restore = restore_then_cancel
    game = _game(a.sync())
    assert game.outcome == O.ROLLED_BACK, game.message
    assert world.read_save("Hades") == "v1"


# 8 — scan failure right before a restore ------------------------------------------------

def test_scan_failure_before_restore_changes_nothing(world):
    a, _ = _usb_newer(world)
    world.write_save("Hades", "old", name="old.sav")
    os.remove(world.save_path("Hades", "old.sav"))
    calls = {"n": 0}

    def fail_second_scan(argv):
        if "--preview" in argv and "backup" in argv:
            calls["n"] += 1
            if calls["n"] == 2:  # the scan inside the restore
                return 1, "", "scan failed"
        return None
    a.ludusavi.hooks.append(fail_second_scan)
    game = _game(a.sync())
    assert game.outcome == O.FAILED and game.state == S.ERROR
    assert world.read_save("Hades") == "v1"
    assert a.record("Hades")["last_synced_backup"] != world.usb_versions("Hades")[-1]["when"]


# 9 — an interrupted (killed) restore ------------------------------------------------------

def test_killed_process_mid_restore_is_protected_and_blocks_use_pc(world):
    a, _ = _usb_newer(world)
    snap = a.service.safety.new_snapshot("restore")
    a.engine.safety_backup("Hades", snap)
    world.write_save("Hades", "half restored mix")
    a.registry.write({"title": "Hades", "pending_op": {
        "kind": "restore", "started": 0, "snapshot": snap, "safety_ok": True,
        "created": [world.save_path("Hades", "b-only.sav")]}})
    assert snap in a.service.protected_snapshots()
    a.service.safety.cleanup(keep=a.service.protected_snapshots(), max_age_days=0)
    assert os.path.isdir(snap)
    game = _game(a.sync())
    assert game.state == S.ERROR and game.message["code"] == "incomplete_operation"
    assert a.service.use_pc(world.drive, "Hades").outcome == O.SKIPPED
    assert a.service.recover_safety("Hades").outcome == O.RESTORED
    assert world.read_save("Hades") == "v1"
    assert _game(a.sync()).outcome == O.RESTORED, "normal flow resumes after recovery"


# 10 — trial progress survives switching ------------------------------------------------------

def test_trial_progress_is_checkpointed_and_restored(world):
    a, _ = _usb_newer(world)
    world.write_save("Hades", "A")
    assert _game(a.sync()).state == S.CONFLICT
    a.trials.start(world.drive, "Hades")
    world.write_save("Hades", "played on the USB version")
    a.trials.switch("Hades", "pc")
    assert world.read_save("Hades") == "A"
    world.write_save("Hades", "played on the PC version")
    a.trials.switch("Hades", "usb")
    assert world.read_save("Hades") == "played on the USB version"
    for path in (a.trials.active("Hades")["latest"] or {}).values():
        assert path in a.service.protected_snapshots()
    a.trials.keep(world.drive, "Hades", "usb")
    assert world.read_save("Hades") == "played on the USB version"
    assert _game(a.sync()).outcome == O.BACKED_UP, "the trial progress is uploaded"


def test_cancel_returns_to_the_pristine_original(world):
    a, _ = _usb_newer(world)
    world.write_save("Hades", "A")
    a.sync()
    a.trials.start(world.drive, "Hades")
    a.trials.switch("Hades", "pc")
    world.write_save("Hades", "tinkered during the trial")
    a.trials.switch("Hades", "usb")
    a.trials.cancel("Hades")
    assert world.read_save("Hades") == "A"


# 11 — validated results survive a failure later in the cycle ---------------------------------

def test_validated_results_are_persisted_per_game(world):
    a = world.pc("A")
    a.register()
    world.write_save("Hades", "h")
    world.write_save("Celeste", "c")
    real = a.registry.update_many
    calls = {"n": 0}

    def crash_on_final_batch(changes):
        calls["n"] += 1
        if len(changes) > 1:
            raise OSError("killed")
        return real(changes)
    a.registry.update_many = crash_on_final_batch
    with pytest.raises(OSError):
        a.sync()
    a.registry.update_many = real
    for title in ("Hades", "Celeste"):
        assert a.record(title)["last_synced_backup"], title


# 12 — nothing to snapshot, yet files would be overwritten -------------------------------------

def test_use_usb_refuses_to_overwrite_undetected_files(world):
    a = world.pc("A")
    a.register()
    world.write_save("Hades", "from A")
    a.sync()
    b = world.switch_pc("B")
    b.register()
    world.write_save("Hades", "B's file that Ludusavi does not attribute to Hades")
    b.ludusavi.games = {**world.games, "Hades": []}  # this PC's scan finds nothing

    real_backup = b.ludusavi._backup
    b.ludusavi.games = world.games

    def blind_scan(root, titles, opts, flags):
        saved = b.ludusavi.games
        b.ludusavi.games = {**saved, "Hades": []}
        try:
            return real_backup(root, titles, opts, flags)
        finally:
            b.ludusavi.games = saved
    b.ludusavi._backup = blind_scan
    result = b.service.use_usb(world.drive, "Hades")
    assert result.outcome == O.FAILED and "cannot be protected" in result.message["message"]
    assert world.read_save("Hades") == "B's file that Ludusavi does not attribute to Hades"


# version gate --------------------------------------------------------------------------------

def test_ludusavi_without_backups_edit_is_refused(world):
    a = world.pc("A")
    a.register()
    world.write_save("Hades", "v1")
    a.ludusavi.hooks.append(lambda argv: (0, "ludusavi 0.29.1\n", "") if "--version" in argv else None)
    report = a.sync()
    assert report.errors and "too old" in report.errors[0]["message"]
    assert world.usb_versions("Hades") == []
