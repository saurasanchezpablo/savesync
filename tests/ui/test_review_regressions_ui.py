"""Regression tests for the second review (controller, UI, Windows adapters)."""
import os
import threading
import time

import pytest

from savesync.core.state import GameState as S
from savesync.core.state import Outcome as O
from savesync.platform.windows.shutdown import WM_ENDSESSION, WM_QUERYENDSESSION, ShutdownProtocol
from savesync.platform.windows.startup import VALUE_NAME, WindowsStartupManager
from tests_ui_helpers import rows_by_title


def _conflict(env):
    world = env.world
    world.write_save("Hades", "v1")
    env.controller.sync_now()
    b = world.switch_pc("B")
    b.register()
    b.service.use_usb(world.drive, "Hades")
    world.write_save("Hades", "B")
    world.write_save("Hades", "B only", name="b-only.sav")
    b.sync()
    world.switch_pc("A")
    world.write_save("Hades", "A")


# 1 — a failed trial switch can neither be kept nor checkpointed as a side
def test_failed_trial_switch_is_mixed_and_cannot_be_kept(ready):
    env = ready
    _conflict(env)
    env.controller.sync_now()
    env.controller.trial_start("Hades")
    from savesync.core.engine import OpResult

    def failing_factory(drive):
        engine = env._engine(drive)
        engine.restore_from = lambda *a, **k: OpResult(False, problem="disk full")
        return engine
    env.controller._engine_factory = failing_factory
    env.controller.trial_switch("Hades", "pc")
    trial = rows_by_title(env)["Hades"].trial
    assert trial["side"] == "mixed"
    env.controller._engine_factory = env._engine
    env.controller.trial_keep("Hades", "pc")  # must re-apply the PC side first
    assert env.world.read_save("Hades") == "A"
    assert env.world.read_save("Hades", "b-only.sav") is None


# 2 — recovering after a SUCCESSFUL restore removes the USB-only files and asks
def test_recover_after_successful_restore_is_exact_and_asks(ready):
    env = ready
    world = env.world
    world.write_save("Hades", "v1")
    env.controller.sync_now()
    b = world.switch_pc("B")
    b.register()
    b.service.use_usb(world.drive, "Hades")
    world.write_save("Hades", "v2")
    world.write_save("Hades", "new", name="b-only.sav")
    b.sync()
    world.switch_pc("A")
    env.controller.sync_now()
    assert world.read_save("Hades", "b-only.sav") == "new"
    env.controller.recover("Hades")
    assert world.read_save("Hades") == "v1"
    assert world.read_save("Hades", "b-only.sav") is None
    versions = world.usb_versions("Hades")
    env.controller.sync_now()
    assert rows_by_title(env)["Hades"].state == S.FIRST_SYNC
    assert world.usb_versions("Hades") == versions, "nothing uploaded automatically"


# 3 — no re-registration during an operation; results keep their USB identity
def test_register_refused_while_busy_and_usb_id_pinned(ready, make_usb):
    env = ready
    other, _ = make_usb("other", serial="OTHER")
    env.controller.busy = True
    assert env.controller.register_usb(other) is None
    env.controller.busy = False
    old_id = env.controller.config.get("usb_id")
    world = env.world
    world.write_save("Hades", "v1")
    service = env.controller.service(world.drive)
    real = service.engine.usb_backup

    def switch_usb_meanwhile(*a, **k):
        out = real(*a, **k)
        env.controller.config.update(usb_id="SOMETHING-ELSE")
        return out
    service.engine.usb_backup = switch_usb_meanwhile
    service.sync(world.drive)
    assert env.controller.registry.get("hades")["baseline_usb_id"] == old_id


# 4 — shutdown waits for the running job instead of failing at once
def test_shutdown_sync_cancels_and_waits_for_background_job(tmp_path, qtbot):
    from conftest import Env
    env = Env(tmp_path, qtbot, synchronous=False)
    try:
        env.register()
        env.controller.config.update(sync_on_usb_connect=False)
        env.controller.start()
        env.connect()
        env.world.write_save("Hades", "v1")
        release = threading.Event()
        env.ludusavi.hooks.append(lambda argv: (release.wait(0.5), None)[1])
        assert env.controller.sync_now()
        assert env.controller.busy
        started = time.monotonic()
        finished = env.controller.shutdown_sync(20)
        assert time.monotonic() - started < 20
        assert env.controller._worker is None or not env.controller._worker.is_alive()
        assert finished is True
        assert env.world.usb_versions("Hades"), "the shutdown sync did the work"
    finally:
        env.ludusavi.hooks.clear()
        env.controller.stop()


def test_shutdown_protocol_syncs_even_if_block_reason_fails():
    class NoBlock:
        def block(self, hwnd, reason):
            return False

        def unblock(self, hwnd):
            raise AssertionError("nothing to unblock")
    ran = []
    protocol = ShutdownProtocol(NoBlock())
    protocol.callback = lambda d: ran.append(d) or True
    protocol.precheck = lambda: True
    protocol.on_message(WM_QUERYENDSESSION, 0)
    assert protocol.on_message(WM_ENDSESSION, 1) == (True, 0)
    assert ran


# 5 — a decision made while busy runs when the job ends, and only if still valid
def test_queued_decision_runs_after_sync_and_is_revalidated(ready, qtbot):
    env = ready
    _conflict(env)
    env.controller.sync_now()
    env.controller.busy = True
    env.window.dispatch({"Hades": "use_pc"})
    assert env.window.queue
    env.controller.busy = False
    env.controller.busyChanged.emit(False)
    qtbot.waitUntil(lambda: not env.window.queue, timeout=3000)
    assert rows_by_title(env)["Hades"].state == S.SYNCED
    env.window.queue.append((env.controller.use_usb, "Hades"))
    env.window._next()
    assert "no longer applies" in env.window.status.text()


# 6 — a game in trial mode does not pop the window up on every cycle
def test_trial_game_does_not_reopen_window(ready):
    env = ready
    _conflict(env)
    env.controller.sync_now()
    env.controller.trial_start("Hades")
    env.window.hide()
    env.dialogs.clear()
    env.notifier.sent.clear()
    env.controller.sync_now(user=False)
    assert not env.window.isVisible()
    assert env.dialogs == []
    assert not any("Conflict" in t for t, _b in env.notifier.sent)


# 7 — refused actions are reported; a USB-connect sync refused while busy runs later
def test_deferred_usb_connect_sync(ready):
    env = ready
    env.controller.config.update(sync_on_usb_connect=True)
    env.detector.remove(env.world.drive)
    env.world.write_save("Hades", "v1")
    env.controller.busy = True
    env.detector.connect(env.world.drive)
    assert env.controller._deferred_sync
    env.controller.busy = False
    env.controller._finish(lambda r: None, None)
    assert rows_by_title(env)["Hades"].state == S.SYNCED


def test_game_detail_buttons_disabled_while_busy(ready):
    from savesync.ui.game_detail import GameDetailDialog
    env = ready
    env.world.write_save("Hades", "v1")
    env.controller.sync_now()
    env.controller.busy = True
    dialog = GameDetailDialog(env.controller, "Hades", load_versions=False)
    assert not dialog.sync_button.isEnabled() and not dialog.restore_button.isEnabled()
    env.controller.busy = False


# 9 — one notification per cycle, most severe first
def test_single_notification_per_cycle(ready):
    env = ready
    env.world.write_save("Hades", "v1")
    env.world.write_save("Celeste", "c")
    env.process.running.add("Celeste")
    env.ludusavi.fail_paths.add(env.world.save_path("Hades"))
    env.notifier.sent.clear()
    env.controller.sync_now(user=False)
    assert len(env.notifier.sent) == 1
    title, body = env.notifier.sent[0]
    assert "running" in body and "failed" in body and "synchronized" in body


# 10 — the versions list never runs next to a job, and never updates the manifest
def test_usb_versions_is_light(ready):
    env = ready
    env.world.write_save("Hades", "v1")
    env.controller.sync_now()
    env.ludusavi.calls.clear()
    assert env.controller.usb_versions("Hades")
    assert all("--no-manifest-update" in c for c in env.ludusavi.calls)
    env.controller.busy = True
    assert env.controller.usb_versions("Hades") == []
    env.controller.busy = False


# 11 — a moved portable folder rewrites the Run entry
def test_startup_entry_is_refreshed():
    class Key(dict):
        def get(self, name):
            return dict.get(self, name)

        def set(self, name, value):
            self[name] = value

        def delete(self, name):
            self.pop(name, None)
    key = Key({VALUE_NAME: '"D:\\old\\SaveSync.exe" --background'})
    manager = WindowsStartupManager(command='"E:\\new\\SaveSync.exe" --background', run_key=key)
    assert manager.refresh() is True and key[VALUE_NAME].startswith('"E:')
    assert manager.refresh() is False
    key.clear()
    assert manager.refresh() is False and VALUE_NAME not in key, "disabled stays disabled"
