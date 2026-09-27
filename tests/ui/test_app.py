"""Application lifecycle and end-to-end flows through the real UI objects."""
import os
import time

import pytest

from savesync.app import AppController, TrayState, UsbStatus
from savesync.core.state import GameState as S
from savesync.core.state import Outcome as O
from tests_ui_helpers import rows_by_title


def test_first_run_opens_settings_for_initial_configuration(env):
    env.build_ui()
    env.answers["SettingsDialog"] = lambda d: 0
    env.controller.start()
    assert env.dialogs_of("SettingsDialog"), "initial configuration may open the window"
    assert env.window.isVisible()


def test_normal_background_start_keeps_the_window_closed(env):
    env.build_ui()
    env.register()
    env.world.write_save("Hades", "v1")
    env.controller.config.update(sync_on_usb_connect=True)
    env.controller.start()
    env.connect()
    assert rows_by_title(env)["Hades"].state == S.SYNCED
    assert not env.window.isVisible()
    assert env.dialogs == []


def test_startup_cleans_old_safety_snapshots_and_shutdown_stops_everything(env):
    old = env.controller.safety.new_snapshot("old")
    with open(os.path.join(old, ".created"), "w") as fh:
        fh.write(str(time.time() - 8 * 86400))
    fresh = env.controller.safety.new_snapshot("fresh")
    env.register()
    env.controller.start()
    assert not os.path.exists(old) and os.path.exists(fresh)
    assert env.detector.monitoring
    env.controller.stop()
    assert not env.detector.monitoring and env.watcher.callback is None


def test_end_to_end_fake_usb_detect_sync_now_result(tmp_path, qtbot, monkeypatch):
    """fake USB connected → detected → status updates → Sync Now → core
    orchestrator called (on the worker thread) → result shown in the UI."""
    from conftest import Env
    env = Env(tmp_path, qtbot, synchronous=False)
    try:
        window = env.build_ui()
        env.register()
        env.controller.config.update(sync_on_usb_connect=False)
        env.controller.start()
        env.world.write_save("Hades", "from this PC")
        assert env.controller.usb_status == UsbStatus.ABSENT or env.controller.drive is None
        env.detector.connect(env.world.drive)
        assert env.controller.drive is not None
        assert "USB connected" in window.banner.title.text()
        assert env.tray.state == TrayState.GREEN
        with qtbot.waitSignal(env.controller.syncFinished, timeout=10000) as finished:
            window.sync_button.click()
        report = finished.args[0]
        assert report.count(O.BACKED_UP) == 1
        assert any("--force" in c for c in env.ludusavi.calls), "the orchestrator ran Ludusavi"
        qtbot.waitUntil(lambda: not env.controller.busy)
        assert "1 synchronized" in window.status.text()
        assert "✓ 1 synchronized" in window.aggregate.text()
        assert env.tray.state == TrayState.GREEN
        assert env.notifier.sent[-1][1].startswith("✓ 1 game synchronized")
    finally:
        env.controller.stop()


def test_conflict_is_not_resolved_until_the_user_chooses(ready):
    env = ready
    world = env.world
    a = env.controller
    world.write_save("Hades", "v1")
    a.sync_now()
    # another PC writes a newer version to the USB
    b = world.switch_pc("B")
    b.register()
    b.service.use_usb(world.drive, "Hades")
    world.write_save("Hades", "B")
    b.sync()
    world.switch_pc("A")
    world.write_save("Hades", "A")
    versions = world.usb_versions("Hades")
    a.sync_now()
    assert rows_by_title(env)["Hades"].state == S.CONFLICT
    assert env.tray.state == TrayState.RED
    assert world.read_save("Hades") == "A" and world.usb_versions("Hades") == versions
    # the dialog is shown; cancelling it changes nothing
    dialog = env.window.open_conflict("Hades")
    assert dialog.choice is None
    assert world.read_save("Hades") == "A" and world.usb_versions("Hades") == versions
    # explicit resolution
    env.answers["ConflictDialog"] = lambda d: d.use_usb.click()
    env.window.open_conflict("Hades")
    assert world.read_save("Hades") == "B"
    assert rows_by_title(env)["Hades"].state == S.SYNCED


def test_usb_removed_with_pending_changes_notifies(ready):
    env = ready
    env.world.write_save("Hades", "v1")
    env.controller.sync_now()
    env.world.write_save("Hades", "v2")
    env.watcher.emit(env.world.game_dir("Hades"))
    assert rows_by_title(env)["Hades"].display_state == S.LOCAL_NEWER
    assert env.tray.state == TrayState.YELLOW
    env.detector.remove(env.world.drive)
    assert env.tray.state == TrayState.WHITE
    assert env.notifier.sent[-1][0] == "⚠ USB removed with pending changes"
    assert "reconnect" in env.window.banner.detail.text()
    env.detector.connect(env.world.drive)
    env.controller.sync_now()
    assert rows_by_title(env)["Hades"].display_state == S.SYNCED


def test_drive_letter_change_is_transparent(ready, tmp_path):
    env = ready
    env.world.write_save("Hades", "v1")
    env.controller.sync_now()
    new_root = str(tmp_path / "G")
    os.rename(env.world.drive.drive_letter, new_root)
    moved = env.detector.move(env.world.drive, new_root)
    assert env.controller.drive.drive_letter == moved.drive_letter
    env.world.drive = moved
    env.world.write_save("Hades", "v2")
    env.controller.sync_now()
    assert rows_by_title(env)["Hades"].state == S.SYNCED
    assert len(env.world.usb_versions("Hades")) == 2


def test_unknown_usb_is_shown_and_never_used(env, make_usb):
    env.build_ui()
    env.register()
    env.controller.start()
    stranger, _ = make_usb("stranger", serial="OTHER")
    env.detector.connect(stranger)
    assert env.controller.usb_status == UsbStatus.UNKNOWN
    assert env.window.banner.title.text() == "Unknown USB"
    assert env.controller.sync_now() is False
    assert env.ludusavi.calls == []


def test_shutdown_sync_only_with_work_and_records_incomplete(ready):
    env = ready
    env.controller.config.update(sync_on_shutdown=True)
    env.controller.apply_shutdown_setting()
    assert env.shutdown.callback is not None
    assert env.controller.shutdown_has_work() is False
    env.world.write_save("Hades", "v1")
    env.controller.sync_now()
    env.world.write_save("Hades", "v2")
    env.watcher.emit(env.world.game_dir("Hades"))
    assert env.controller.shutdown_has_work() is True
    started = time.monotonic()
    assert env.shutdown.simulate_shutdown(30) is True
    assert time.monotonic() - started < 30
    assert len(env.world.usb_versions("Hades")) == 2
    # deadline too short: stays pending and is reported at next start
    env.world.write_save("Hades", "v3")
    assert env.controller.shutdown_sync(0.5) is False
    assert env.controller.config.get("shutdown_incomplete") is True
    env.controller.config.update(sync_on_shutdown=False)
    env.controller.apply_shutdown_setting()
    assert env.shutdown.callback is None


def test_incomplete_shutdown_is_reported_on_next_start(env):
    env.register()
    env.controller.config.update(shutdown_incomplete=True)
    env.controller.start()
    assert any("shutdown" in body.lower() for _t, body in env.notifier.sent)
    assert env.controller.config.get("shutdown_incomplete") is False


def test_running_game_prompt_and_wait_and_synchronize(ready, qtbot):
    env = ready
    env.world.write_save("Hades", "v1")
    env.process.running.add("Hades")
    env.answers["RunningPrompt"] = lambda d: d.wait_button.click()
    env.controller.sync_now(user=True)
    assert env.dialogs_of("RunningPrompt")
    assert "Hades is currently running." == env.dialogs_of("RunningPrompt")[0].text()
    assert env.controller.waiting_for() == ["Hades"]
    assert env.world.usb_versions("Hades") == []
    env.process.running.clear()
    timer = env.controller._waiting["Hades"][0]
    timer.timeout.emit()
    assert env.controller.waiting_for() == []
    assert rows_by_title(env)["Hades"].state == S.SYNCED


def test_background_sync_with_running_game_does_not_prompt(ready):
    env = ready
    env.world.write_save("Hades", "v1")
    env.process.running.add("Hades")
    env.controller.sync_now(user=False)
    assert env.dialogs_of("RunningPrompt") == []
    assert any(t == "Game running" for t, _b in env.notifier.sent)


def test_first_sync_opens_wizard_automatically(ready):
    env = ready
    world = env.world
    world.write_save("Hades", "A")
    env.controller.sync_now()
    world.switch_pc("B")
    # a second PC: fresh app state, same USB
    from conftest import Env
    env_b = Env(os.path.join(world.root, "pcB-tmp"), env.qtbot)
    env_b.world = world
    env_b.ludusavi.games = world.games
    env_b.build_ui()
    env_b.controller.register_usb(world.drive)
    env_b.controller.config.update(sync_on_usb_connect=False)
    env_b.controller.start()
    env_b.detector.connect(world.drive)
    world.write_save("Hades", "B's own")
    env_b.answers["FirstSyncWizard"] = lambda d: (d.set_choice("Hades", "use_usb"), 1)[1]
    env_b.controller.sync_now()
    assert env_b.dialogs_of("FirstSyncWizard")
    assert world.read_save("Hades") == "A"
    assert rows_by_title(env_b)["Hades"].state == S.SYNCED
    env_b.controller.stop()


def test_watcher_marks_dirty_only_matching_games(ready):
    env = ready
    env.world.write_save("Hades", "h")
    env.world.write_save("Celeste", "c")
    env.controller.sync_now()
    assert set(env.watcher.paths) == {env.world.save_path("Hades"), env.world.save_path("Celeste")}
    env.watcher.emit(env.world.game_dir("Celeste"))
    rows = rows_by_title(env)
    assert rows["Celeste"].dirty and not rows["Hades"].dirty


def test_ludusavi_missing_is_reported_not_crashing(env, monkeypatch):
    env.build_ui()
    env.register()
    env.controller.config.update(sync_on_usb_connect=False)
    env.controller._engine_factory = env.controller._default_engine
    env.controller.config.update(ludusavi_command=["definitely-not-ludusavi-xyz"])
    env.controller.start()
    env.connect()
    env.controller.sync_now()
    report = env.controller.last_report
    assert report.errors and "Ludusavi was not found" in report.errors[0]["message"]
    assert env.tray.state == TrayState.RED


def test_single_instance_guard():
    from savesync.platform.fake import FakeSingleInstance
    first, second = FakeSingleInstance("app"), FakeSingleInstance("app")
    assert first.acquire() and not second.acquire()
    first.release()
