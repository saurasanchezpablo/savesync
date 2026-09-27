"""Trial mode — "Explore both" (plan §15)."""
import os
import shutil

import pytest

from savesync.core.state import GameState as S
from savesync.core.state import Outcome as O
from world import World


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.fixture
def conflicted(world):
    """Hades changed on both PCs: PC A has "A" + a file only A has, USB has "B"."""
    a = world.pc("A")
    a.register()
    world.write_save("Hades", "v1")
    a.sync()
    b = world.switch_pc("B")
    b.register()
    b.service.use_usb(world.drive, "Hades")
    world.write_save("Hades", "B")
    world.write_save("Hades", "B only", name="b-only.sav")
    b.sync()
    world.switch_pc("A")
    world.write_save("Hades", "A")
    world.write_save("Hades", "A only", name="a-only.sav")
    assert next(g for g in a.sync().games if g.title == "Hades").state == S.CONFLICT
    return a


def _files(world):
    d = world.game_dir("Hades")
    return {n: open(os.path.join(d, n)).read() for n in sorted(os.listdir(d))}


def test_start_trial_tests_usb_version_and_keeps_both(world, conflicted):
    a = conflicted
    result = a.trials.start(world.drive, "Hades")
    assert result.outcome == O.RESTORED
    assert _files(world) == {"b-only.sav": "B only", "slot.sav": "B"}, \
        "the USB version exactly, without A's extra file"
    trial = a.trials.active("Hades")
    assert trial["side"] == "usb"
    assert os.path.isdir(trial["original"]) and os.path.isdir(trial["usb_copy"])
    assert a.record("Hades")["conflict"] is True, "nothing is decided yet"


def test_switching_is_offline_and_exact(world, conflicted):
    a = conflicted
    a.trials.start(world.drive, "Hades")
    hidden = world.drive.drive_letter + ".away"
    os.rename(world.drive.drive_letter, hidden)  # no USB needed to switch
    assert a.trials.switch("Hades", "pc").outcome == O.RESTORED
    assert _files(world) == {"a-only.sav": "A only", "slot.sav": "A"}
    assert a.trials.switch("Hades", "usb").outcome == O.RESTORED
    assert _files(world) == {"b-only.sav": "B only", "slot.sav": "B"}
    os.rename(hidden, world.drive.drive_letter)


def test_cancel_returns_to_original_and_keeps_conflict(world, conflicted):
    a = conflicted
    versions = world.usb_versions("Hades")
    a.trials.start(world.drive, "Hades")
    assert a.trials.cancel("Hades").outcome == O.ROLLED_BACK
    assert _files(world) == {"a-only.sav": "A only", "slot.sav": "A"}
    assert a.trials.active("Hades") is None
    assert world.usb_versions("Hades") == versions
    assert next(g for g in a.sync().games if g.title == "Hades").state == S.CONFLICT


def test_keep_usb_sets_baseline_and_progress_during_trial_uploads(world, conflicted):
    a = conflicted
    a.trials.start(world.drive, "Hades")
    result = a.trials.keep(world.drive, "Hades", "usb")
    assert result.state == S.SYNCED and a.trials.active("Hades") is None
    assert _files(world) == {"b-only.sav": "B only", "slot.sav": "B"}
    assert next(g for g in a.sync().games if g.title == "Hades").state == S.SYNCED
    world.write_save("Hades", "progress on B's save")
    assert next(g for g in a.sync().games if g.title == "Hades").outcome == O.BACKED_UP


def test_keep_pc_uploads_original(world, conflicted):
    a = conflicted
    a.trials.start(world.drive, "Hades")
    result = a.trials.keep(world.drive, "Hades", "pc")
    assert result.outcome == O.BACKED_UP and result.state == S.SYNCED
    assert _files(world) == {"a-only.sav": "A only", "slot.sav": "A"}
    assert a.record("Hades")["conflict"] is False
    b = world.switch_pc("B")
    report = b.sync()
    assert world.read_save("Hades") == "A"


def test_keep_pc_without_usb_keeps_trial(world, conflicted):
    a = conflicted
    a.trials.start(world.drive, "Hades")
    shutil.rmtree(world.drive.savesync_dir)
    result = a.trials.keep(world.drive, "Hades", "pc")
    assert result.outcome == O.SKIPPED
    assert a.trials.active("Hades") is not None


def test_automatic_sync_never_touches_a_game_in_trial(world, conflicted):
    a = conflicted
    a.trials.start(world.drive, "Hades")
    world.write_save("Hades", "played during trial")
    game = next(g for g in a.sync().games if g.title == "Hades")
    assert game.outcome == O.SKIPPED and game.message["code"] == "state_trial"
    assert world.read_save("Hades") == "played during trial"
    assert a.service.use_pc(world.drive, "Hades").message["code"] == "trial_active"


def test_trial_refuses_running_game(world, conflicted):
    a = conflicted
    a.process.running.add("Hades")
    assert a.trials.start(world.drive, "Hades").outcome == O.SKIPPED
    a.process.running.clear()
    a.trials.start(world.drive, "Hades")
    a.process.running.add("Hades")
    assert a.trials.switch("Hades", "pc").outcome == O.SKIPPED
    assert world.read_save("Hades") == "B"


def test_trial_snapshots_are_protected_from_cleanup(world, conflicted):
    a = conflicted
    a.trials.start(world.drive, "Hades")
    trial = a.trials.active("Hades")
    keep = a.service.protected_snapshots()
    assert trial["original"] in keep and trial["usb_copy"] in keep
    a.service.safety.cleanup(keep=keep, max_age_days=0)
    assert os.path.isdir(trial["original"]) and os.path.isdir(trial["usb_copy"])


def test_failed_trial_start_restores_original(world, conflicted):
    a = conflicted
    a.ludusavi.fail_restore_paths.add(world.save_path("Hades"))
    result = a.trials.start(world.drive, "Hades")
    assert result.outcome == O.FAILED and a.trials.active("Hades") is None
    assert _files(world) == {"a-only.sav": "A only", "slot.sav": "A"}
