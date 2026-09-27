import os

from savesync.core import usb as usbmod
from savesync.ui.settings import SettingsDialog


def test_settings_load_current_values(ready):
    ready.controller.config.update(full_limit=4, differential_limit=2, sync_on_shutdown=True,
                                   notifications={"usb_connected": False})
    dialog = SettingsDialog(ready.controller)
    assert dialog.full_limit.value() == 4 and dialog.diff_limit.value() == 2
    assert dialog.on_shutdown.isChecked() and dialog.on_connect.isChecked() is False
    assert dialog.notifications["usb_connected"].isChecked() is False
    assert dialog.notifications["conflict"].isChecked() is True
    assert "connected" in dialog.usb_current.text()


def test_settings_save(ready):
    dialog = SettingsDialog(ready.controller)
    dialog.full_limit.setValue(3)
    dialog.diff_limit.setValue(0)
    dialog.on_connect.setChecked(True)
    dialog.on_shutdown.setChecked(True)
    dialog.startup.setChecked(True)
    dialog.ludusavi_path.setText("C:/tools/ludusavi.exe")
    dialog.notifications["sync_completed"].setChecked(False)
    dialog.save()
    cfg = ready.controller.config.load()
    assert cfg["full_limit"] == 3 and cfg["differential_limit"] == 0
    assert cfg["sync_on_usb_connect"] and cfg["sync_on_shutdown"]
    assert cfg["ludusavi_path_override"] == "C:/tools/ludusavi.exe"
    assert cfg["notifications"]["sync_completed"] is False
    assert ready.startup.enabled is True
    assert ready.shutdown.callback is not None, "shutdown handler registered only when enabled"


def test_retention_cannot_go_below_two(ready):
    dialog = SettingsDialog(ready.controller)
    dialog.full_limit.setValue(1)
    assert dialog.full_limit.value() == 2


def test_usb_selection_registers_a_new_drive(env, make_usb):
    env.build_ui()
    env.controller.start()
    blank, _ = make_usb("blank", serial="B-1", initialize=False)
    env.detector.connect(blank)
    dialog = SettingsDialog(env.controller)
    assert dialog.usb_combo.count() == 1 and "empty" in dialog.usb_combo.itemText(0)
    dialog.register_selected()
    cfg = env.controller.config.load()
    assert cfg["usb_id"] and cfg["usb_volume_serial"] == "B-1"
    assert os.path.isfile(usbmod.identity_path(blank))
    assert env.controller.drive is not None
    assert "registered" in dialog.usb_combo.itemText(0)


def test_disabled_notification_is_not_sent(ready):
    ready.controller.config.update(notifications={"sync_completed": False})
    ready.world.write_save("Hades", "v1")
    ready.notifier.sent.clear()
    ready.controller.sync_now()
    assert ready.notifier.sent == []


def test_safety_cleanup_shows_size_and_cleans(ready):
    snap = ready.controller.safety.new_snapshot("x")
    with open(os.path.join(snap, "data"), "wb") as fh:
        fh.write(b"x" * 2048)
    dialog = SettingsDialog(ready.controller)
    assert "Safety snapshots: 1" in dialog.safety_label.text()
    assert "KB" in dialog.safety_label.text()
    dialog.clean()
    assert "Safety snapshots: 0" in dialog.safety_label.text()
    assert not os.path.exists(snap)


def test_open_logs_folder(ready, monkeypatch):
    from PySide6.QtGui import QDesktopServices
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url)))
    SettingsDialog(ready.controller).open_logs()
    assert opened and opened[0].toLocalFile() == os.path.dirname(ready.controller.log.path)
