from PySide6.QtWidgets import QSystemTrayIcon

from savesync.app import TrayState
from savesync.core.state import GameState as S


def test_tray_menu_has_the_plan_entries(ready):
    labels = [a.text() for a in ready.tray.menu.actions() if not a.isSeparator()]
    assert labels == ["Save Sync", "Sync Now", "View Status", "Conflicts", "Settings", "Exit"]
    assert not ready.tray.header.isEnabled()


def test_four_visual_states(env):
    env.build_ui()
    env.register()
    env.controller.config.update(sync_on_usb_connect=False)
    env.controller.start()
    assert env.tray.state == TrayState.WHITE          # USB absent
    env.connect()
    assert env.tray.state == TrayState.GREEN          # connected + synchronized
    env.world.write_save("Hades", "v1")
    env.controller.sync_now()
    env.watcher.emit(env.world.game_dir("Hades"))
    assert env.tray.state == TrayState.YELLOW         # pending changes
    env.controller.registry.upsert({"title": "Celeste", "state": S.CONFLICT.value})
    env.tray.update_state()
    assert env.tray.state == TrayState.RED            # conflict / error
    assert env.tray.toolTip().startswith("Save Sync")


def test_each_state_has_its_own_icon(ready):
    from savesync.ui.resources import tray_icon
    keys = {tray_icon(s).cacheKey() for s in ("green", "yellow", "red", "white")}
    assert len(keys) == 4


def test_menu_actions(ready, qtbot):
    env = ready
    env.world.write_save("Hades", "v1")
    env.tray.sync_action.trigger()
    assert env.controller.rows()[0].state == S.SYNCED, "Sync Now ran the orchestrator"
    env.tray.status_action.trigger()
    assert env.window.isVisible()
    env.window.hide()
    env.tray.conflicts_action.trigger()
    assert env.window.isVisible() and env.window.tabs.tabText(env.window.tabs.currentIndex()).startswith("Conflicts")
    env.answers["SettingsDialog"] = lambda d: 0
    env.tray.settings_action.trigger()
    assert env.dialogs_of("SettingsDialog")


def test_left_click_opens_main_window(ready):
    ready.window.hide()
    ready.tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
    assert ready.window.isVisible()


def test_sync_now_disabled_without_usb(env):
    env.build_ui()
    env.register()
    env.controller.start()
    assert not env.tray.sync_action.isEnabled()
    assert not env.window.sync_button.isEnabled()


def test_exit_quits(ready, monkeypatch):
    from PySide6.QtWidgets import QApplication
    quit_calls = []
    monkeypatch.setattr(QApplication.instance(), "quit", lambda: quit_calls.append(1))
    ready.tray.exit_action.trigger()
    assert quit_calls == [1]
