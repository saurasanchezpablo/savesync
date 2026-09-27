"""Settings (plan §19.6)."""
from __future__ import annotations

import os

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QSpinBox, QVBoxLayout)

from ..core import usb as usbmod
from ..core.registry import MIN_FULL_LIMIT
from ..core.safety import human_size

NOTIFICATION_LABELS = [
    ("usb_connected", "USB connected"),
    ("sync_completed", "Synchronization completed"),
    ("conflict", "Conflict detected"),
    ("error", "Errors"),
    ("usb_removed_pending", "USB removed with pending changes"),
    ("game_running", "Game running during synchronization"),
    ("shutdown_incomplete", "Incomplete shutdown synchronization"),
]


class SettingsDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Save Sync — Settings")
        cfg = controller.config.load()
        layout = QVBoxLayout(self)

        usb_box = QGroupBox("USB drive")
        usb_layout = QVBoxLayout(usb_box)
        self.usb_current = QLabel()
        self.usb_current.setWordWrap(True)
        usb_layout.addWidget(self.usb_current)
        row = QHBoxLayout()
        self.usb_combo = QComboBox()
        self.refresh_button = QPushButton("Refresh")
        self.register_button = QPushButton("Use this drive")
        row.addWidget(self.usb_combo, 1)
        row.addWidget(self.refresh_button)
        row.addWidget(self.register_button)
        usb_layout.addLayout(row)
        layout.addWidget(usb_box)

        lud_box = QGroupBox("Ludusavi")
        lud = QFormLayout(lud_box)
        path_row = QHBoxLayout()
        self.ludusavi_path = QLineEdit(cfg.get("ludusavi_path_override") or "")
        self.ludusavi_path.setPlaceholderText("Automatic: SaveSync\\ludusavi\\ludusavi.exe on the USB")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.ludusavi_path, 1)
        path_row.addWidget(browse)
        lud.addRow("Executable override:", path_row)
        self.full_limit = QSpinBox()
        self.full_limit.setRange(MIN_FULL_LIMIT, 255)
        self.full_limit.setValue(cfg["full_limit"])
        self.full_limit.setToolTip("At least 2: the previous version is never replaced in place.")
        self.diff_limit = QSpinBox()
        self.diff_limit.setRange(0, 255)
        self.diff_limit.setValue(cfg["differential_limit"])
        lud.addRow("Full backups kept on the USB:", self.full_limit)
        lud.addRow("Differential backups per full:", self.diff_limit)
        self.playnite = QLineEdit(cfg.get("playnite_export_path") or "")
        self.playnite.setPlaceholderText("Optional Playnite library export (JSON)")
        lud.addRow("Playnite metadata:", self.playnite)
        layout.addWidget(lud_box)

        behavior = QGroupBox("Behavior")
        beh = QVBoxLayout(behavior)
        self.on_connect = QCheckBox("Synchronize when the USB is connected")
        self.on_connect.setChecked(bool(cfg.get("sync_on_usb_connect")))
        self.on_shutdown = QCheckBox("Synchronize saves when shutting down Windows")
        self.on_shutdown.setChecked(bool(cfg.get("sync_on_shutdown")))
        self.on_shutdown.setToolTip("Only when the USB is connected and there are changes; "
                                    "limited to %d s so shutdown is never blocked for long."
                                    % cfg.get("shutdown_deadline_seconds"))
        self.startup = QCheckBox("Start with Windows")
        startup = controller.platform.startup
        self.startup.setChecked(startup.is_enabled())
        self.startup.setEnabled(getattr(startup, "supported", True))
        for w in (self.on_connect, self.on_shutdown, self.startup):
            beh.addWidget(w)
        layout.addWidget(behavior)

        notif = QGroupBox("Notifications")
        nl = QVBoxLayout(notif)
        prefs = cfg.get("notifications") or {}
        self.notifications = {}
        for key, label in NOTIFICATION_LABELS:
            box = QCheckBox(label)
            box.setChecked(bool(prefs.get(key, True)))
            nl.addWidget(box)
            self.notifications[key] = box
        layout.addWidget(notif)

        maint = QGroupBox("Maintenance")
        ml = QHBoxLayout(maint)
        self.safety_label = QLabel()
        self.clean_button = QPushButton("Clean safety backups")
        self.logs_button = QPushButton("Open logs folder")
        ml.addWidget(self.safety_label, 1)
        ml.addWidget(self.clean_button)
        ml.addWidget(self.logs_button)
        layout.addWidget(maint)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.refresh_button.clicked.connect(self.refresh_drives)
        self.register_button.clicked.connect(self.register_selected)
        self.clean_button.clicked.connect(self.clean)
        self.logs_button.clicked.connect(self.open_logs)
        self.refresh_drives()
        self._update_safety()

    # --- USB ---

    def refresh_drives(self) -> None:
        self.controller.refresh_drives()
        cfg = self.controller.config.load()
        self.usb_combo.clear()
        for drive in self.controller.drives:
            match = usbmod.classify(drive, cfg)[0]
            status = {usbmod.UsbMatch.REGISTERED: "registered",
                      usbmod.UsbMatch.UNREGISTERED: "Save Sync drive",
                      usbmod.UsbMatch.UNINITIALIZED: "empty",
                      usbmod.UsbMatch.UNKNOWN: "unknown",
                      usbmod.UsbMatch.CORRUPT: "damaged marker"}[match]
            self.usb_combo.addItem("%s — %s" % (drive.display_name, status), drive)
        registered = cfg.get("usb_label") or cfg.get("usb_id")
        if self.controller.drive is not None:
            self.usb_current.setText("Registered USB: %s (connected)" % self.controller.drive.display_name)
        elif registered:
            self.usb_current.setText("Registered USB: %s (not connected)" % registered)
        else:
            self.usb_current.setText("No USB registered yet. Connect a drive and choose it below.")
        self.register_button.setEnabled(self.usb_combo.count() > 0 and not self.controller.busy)

    def register_selected(self) -> None:
        drive = self.usb_combo.currentData()
        if drive is None:
            return
        cfg = self.controller.config.load()
        if (cfg.get("usb_id") or "") and usbmod.classify(drive, cfg)[0] != usbmod.UsbMatch.REGISTERED:
            answer = QMessageBox.question(
                self, "Change USB",
                "Use %s as the Save Sync USB instead of the registered one?\n\nGames on this PC "
                "will need a first synchronization with the new drive." % drive.display_name)
            if answer != QMessageBox.StandardButton.Yes:
                return
        if self.controller.register_usb(drive) is None:
            QMessageBox.information(self, "Change USB", "Save Sync is busy with another "
                                    "operation. Try again when it finishes.")
            return
        self.refresh_drives()

    # --- other ---

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Ludusavi executable", "",
                                              "Ludusavi (ludusavi.exe ludusavi);;All files (*)")
        if path:
            self.ludusavi_path.setText(path)

    def _update_safety(self) -> None:
        count, size = self.controller.safety_usage()
        self.safety_label.setText("Safety snapshots: %d (%s)" % (count, human_size(size)))

    def clean(self) -> None:
        count, freed = self.controller.clean_safety()
        self._update_safety()
        QMessageBox.information(self, "Safety backups", "Removed %d snapshot(s), %s." % (
            count, human_size(freed)))

    def open_logs(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(self.controller.log.path)))

    def save(self) -> None:
        startup = self.controller.platform.startup
        try:
            if self.startup.isChecked() and not startup.is_enabled():
                startup.enable()
            elif not self.startup.isChecked() and startup.is_enabled():
                startup.disable()
        except (OSError, NotImplementedError) as exc:
            QMessageBox.warning(self, "Start with Windows", str(exc))
        self.controller.config.update(
            ludusavi_path_override=self.ludusavi_path.text().strip(),
            full_limit=self.full_limit.value(),
            differential_limit=self.diff_limit.value(),
            sync_on_usb_connect=self.on_connect.isChecked(),
            sync_on_shutdown=self.on_shutdown.isChecked(),
            playnite_export_path=self.playnite.text().strip(),
            notifications={k: b.isChecked() for k, b in self.notifications.items()},
        )
        self.controller.apply_shutdown_setting()
        self.accept()
