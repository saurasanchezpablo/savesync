"""Conflict dialog actions and the trial-mode flow from the UI."""
import os

import pytest

from savesync.core.state import GameState as S
from tests_ui_helpers import rows_by_title


@pytest.fixture
def conflicted(ready):
    env, world = ready, ready.world
    world.write_save("Hades", "v1")
    env.controller.sync_now()
    b = world.switch_pc("B")
    b.register()
    b.service.use_usb(world.drive, "Hades")
    world.write_save("Hades", "B")
    b.sync()
    world.switch_pc("A")
    world.write_save("Hades", "A")
    env.controller.sync_now()
    assert rows_by_title(env)["Hades"].state == S.CONFLICT
    env.dialogs.clear()
    return env


def test_dialog_shows_both_sides_and_safety_note(conflicted):
    from savesync.ui.conflict import ConflictDialog
    dialog = ConflictDialog(conflicted.controller.row("Hades"))
    texts = " ".join(w.text() for w in dialog.findChildren(type(dialog.use_usb)))
    assert "Use USB" in texts and "Use PC" in texts and "Explore both" in texts
    from PySide6.QtWidgets import QLabel
    labels = " ".join(l.text() for l in dialog.findChildren(QLabel))
    assert "⚠ CONFLICT" in labels and "PC   → changed" in labels and "USB  → changed" in labels
    assert "safety snapshot" in labels


def test_use_pc(conflicted):
    conflicted.answers["ConflictDialog"] = lambda d: d.use_pc.click()
    conflicted.window.open_conflict("Hades")
    assert conflicted.world.read_save("Hades") == "A"
    assert rows_by_title(conflicted)["Hades"].state == S.SYNCED


def test_use_usb(conflicted):
    conflicted.answers["ConflictDialog"] = lambda d: d.use_usb.click()
    conflicted.window.open_conflict("Hades")
    assert conflicted.world.read_save("Hades") == "B"


def test_declined_confirmation_changes_nothing(conflicted, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))
    conflicted.answers["ConflictDialog"] = lambda d: d.use_usb.click()
    dialog = conflicted.window.open_conflict("Hades")
    assert dialog.choice is None and conflicted.world.read_save("Hades") == "A"


def test_explore_both_enters_trial_mode_and_keeps_usb(conflicted):
    env = conflicted
    trial_steps = []

    def drive_trial(dialog):
        trial_steps.append(dialog.side.text())
        dialog.test_pc.click()
        trial_steps.append(env.world.read_save("Hades"))
        dialog.test_usb.click()
        trial_steps.append(env.world.read_save("Hades"))
        dialog.keep_usb.click()
    env.answers["ConflictDialog"] = lambda d: d.explore.click()
    env.answers["TrialDialog"] = drive_trial
    env.window.open_conflict("Hades")
    assert env.dialogs_of("TrialDialog"), "trial window opened after the trial started"
    assert "USB version" in trial_steps[0]
    assert trial_steps[1:] == ["A", "B"]
    assert rows_by_title(env)["Hades"].trial is None
    assert rows_by_title(env)["Hades"].state == S.SYNCED
    assert env.world.read_save("Hades") == "B"


def test_trial_banner_and_cancel_returns_to_original(conflicted):
    env = conflicted
    seen = {}

    def cancel(dialog):
        from PySide6.QtWidgets import QLabel
        seen["banner"] = [l.text() for l in dialog.findChildren(QLabel)]
        seen["row"] = rows_by_title(env)["Hades"]
        dialog.cancel_trial.click()
    env.answers["ConflictDialog"] = lambda d: d.explore.click()
    env.answers["TrialDialog"] = cancel
    env.window.open_conflict("Hades")
    assert "⚠ Trial mode" in seen["banner"]
    assert seen["row"].trial is not None
    assert env.world.read_save("Hades") == "A"
    assert rows_by_title(env)["Hades"].state == S.CONFLICT
    assert rows_by_title(env)["Hades"].trial is None


def test_game_in_trial_opens_trial_window_from_list(conflicted):
    env = conflicted
    env.controller.trial_start("Hades")
    env.dialogs.clear()
    env.window.open_game("Hades")
    assert env.dialogs_of("TrialDialog")
    assert rows_by_title(env)["Hades"].trial


def test_trial_keep_pc_requires_usb(conflicted):
    env = conflicted
    env.controller.trial_start("Hades")
    env.detector.remove(env.world.drive)
    from savesync.ui.trial import TrialDialog
    dialog = TrialDialog(env.controller, "Hades")
    assert not dialog.keep_pc.isEnabled()
    assert dialog.keep_usb.isEnabled() and dialog.test_pc.isEnabled()
