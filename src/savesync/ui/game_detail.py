"""Game detail (plan §19.3) and the "Manage" remedy for missing local paths."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QVBoxLayout)

from ..core.state import GameState
from .resources import STATE_COLORS, STATE_LABELS, iso_text, when_text


def pc_status(row) -> str:
    if row.trial:
        return "Trial mode — testing the %s version" % ("USB" if row.trial.get("side") == "usb" else "PC")
    state = row.display_state
    if state == GameState.MISSING_LOCAL_PATH:
        return "No local save path" + (" (%s)" % row.restore_problem if row.restore_problem else "")
    if state in (GameState.LOCAL_NEWER, GameState.CONFLICT):
        return "Changed since the last synchronization"
    if not row.pc_has_save and not row.save_paths:
        return "No save on this PC"
    return "Unchanged" if state in (GameState.SYNCED, GameState.USB_NEWER) else STATE_LABELS[state]


def usb_status(row) -> str:
    state = row.display_state
    if not row.usb_has_backup and state != GameState.SYNCED:
        return "No backup yet"
    if state in (GameState.USB_NEWER, GameState.CONFLICT):
        return "Newer version from another PC"
    return "Has the synchronized version (%s)" % iso_text(row.usb_when) if row.usb_when else "Has a backup"


class GameDetailDialog(QDialog):
    def __init__(self, controller, title: str, parent=None, load_versions: bool = True):
        super().__init__(parent)
        self.controller = controller
        self.title = title
        self.action = None  # what the window asked the controller to do
        self.setWindowTitle(title)
        self.resize(520, 560)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.cover = QLabel()
        self.cover.setFixedSize(96, 128)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self.cover)
        head = QVBoxLayout()
        self.name = QLabel(title)
        self.name.setStyleSheet("font-size: 17px; font-weight: bold;")
        self.name.setWordWrap(True)
        self.platform = QLabel()
        self.state = QLabel()
        head.addWidget(self.name)
        head.addWidget(self.platform)
        head.addWidget(self.state)
        head.addStretch(1)
        top.addLayout(head, 1)
        layout.addLayout(top)
        form = QFormLayout()
        self.pc = QLabel()
        self.usb = QLabel()
        self.last = QLabel()
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        for label in (self.pc, self.usb, self.detail):
            label.setWordWrap(True)
        form.addRow("PC:", self.pc)
        form.addRow("USB:", self.usb)
        form.addRow("Last synchronization:", self.last)
        form.addRow("Details:", self.detail)
        layout.addLayout(form)
        layout.addWidget(QLabel("Available USB versions:"))
        self.versions = QListWidget()
        layout.addWidget(self.versions, 1)
        actions = QHBoxLayout()
        self.sync_button = QPushButton("Synchronize this game")
        self.restore_button = QPushButton("Restore selected version")
        self.resolve_button = QPushButton("Resolve…")
        self.manage_button = QPushButton("Manage…")
        self.recover_button = QPushButton("Recover previous PC state")
        for b in (self.sync_button, self.resolve_button, self.manage_button):
            actions.addWidget(b)
        layout.addLayout(actions)
        more = QHBoxLayout()
        more.addWidget(self.restore_button)
        more.addWidget(self.recover_button)
        layout.addLayout(more)
        self.exclude = QCheckBox("Exclude from automatic synchronization")
        layout.addWidget(self.exclude)
        procs = QHBoxLayout()
        procs.addWidget(QLabel("Process names:"))
        self.process_names = QLineEdit()
        self.process_names.setPlaceholderText("e.g. bg3.exe, bg3_dx11.exe (optional)")
        procs.addWidget(self.process_names, 1)
        layout.addLayout(procs)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)

        self.sync_button.clicked.connect(self._sync)
        self.restore_button.clicked.connect(self._restore_version)
        self.resolve_button.clicked.connect(lambda: self._finish("resolve"))
        self.manage_button.clicked.connect(self._manage)
        self.recover_button.clicked.connect(self._recover)
        self.exclude.toggled.connect(lambda on: controller.set_excluded(title, on))
        self.process_names.editingFinished.connect(
            lambda: controller.set_process_names(title, self.process_names.text().split(",")))
        self.refresh(load_versions)

    def refresh(self, load_versions: bool = True) -> None:
        row = self.controller.row(self.title)
        if row is None:
            self.state.setText("Unknown game")
            return
        state = row.display_state
        self.state.setText("<b style='color:%s'>%s</b>" % (STATE_COLORS[state], STATE_LABELS[state]))
        self.pc.setText(pc_status(row))
        self.usb.setText(usb_status(row))
        self.last.setText(when_text(row.last_sync_ts))
        self.detail.setText(row.message or row.error or "—")
        platform = self.controller.platform_name(self.title)
        self.platform.setText(platform or "")
        cover = self.controller.cover(self.title)
        if cover and os.path.isfile(cover):
            self.cover.setPixmap(QPixmap(cover).scaled(96, 128, Qt.AspectRatioMode.KeepAspectRatio,
                                                       Qt.TransformationMode.SmoothTransformation))
        else:
            self.cover.setText("🎮")
            self.cover.setStyleSheet("font-size: 40px;")
        rec = self.controller.registry.get(row.key) or {}
        self.exclude.blockSignals(True)
        self.exclude.setChecked(row.excluded)
        self.exclude.blockSignals(False)
        self.process_names.setText(", ".join(rec.get("process_names") or []))
        connected = self.controller.drive is not None
        self.resolve_button.setVisible(state in (GameState.CONFLICT, GameState.FIRST_SYNC)
                                       and not row.trial)
        self.resolve_button.setText("Trial mode…" if row.trial else "Resolve…")
        if row.trial:
            self.resolve_button.setVisible(True)
        self.manage_button.setVisible(state == GameState.MISSING_LOCAL_PATH)
        self.sync_button.setEnabled(connected and not row.trial)
        self.recover_button.setEnabled(row.has_recovery and not row.trial)
        if load_versions:
            self.versions.clear()
            for b in self.controller.usb_versions(self.title):
                marks = []
                if b.locked:
                    marks.append("🔒 protected")
                if b.incomplete:
                    marks.append("incomplete")
                if b.when == row.usb_when:
                    marks.append("synchronized")
                item = QListWidgetItem("%s  %s" % (iso_text(b.when), "  ".join(marks)))
                item.setData(Qt.ItemDataRole.UserRole, b.name)
                if b.incomplete:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                self.versions.addItem(item)
        self.restore_button.setEnabled(connected and not row.trial)

    def _finish(self, action):
        self.action = action
        self.accept()

    def _sync(self):
        self.controller.sync_now(titles=[self.title])
        self._finish("sync")

    def _restore_version(self):
        item = self.versions.currentItem()
        if item is None:
            QMessageBox.information(self, "Restore", "Select a USB version first.")
            return
        answer = QMessageBox.question(
            self, "Restore version",
            "Restore the USB version from %s to this PC?\n\nThe current PC saves are kept in a "
            "safety snapshot, and the restored version becomes the newest on the USB."
            % item.text().split("  ")[0])
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.restore_version(self.title, item.data(Qt.ItemDataRole.UserRole))
            self._finish("restore_version")

    def _recover(self):
        answer = QMessageBox.question(self, "Recover",
                                      "Put back the PC saves from the latest safety snapshot?")
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.recover(self.title)
            self._finish("recover")

    def _manage(self):
        ManageDialog(self.controller, self.title, self).exec()
        self.refresh(load_versions=False)


class ManageDialog(QDialog):
    """Game found on USB, no local save path: map the other PC's user folder to
    this one (a Ludusavi restore redirect), or exclude the game."""

    def __init__(self, controller, title: str, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.title = title
        self.setWindowTitle("Manage — %s" % title)
        layout = QVBoxLayout(self)
        row = controller.row(title)
        info = QLabel("Game found on USB.\nNo local save path was found%s." % (
            ": " + row.restore_problem if row and row.restore_problem else ""))
        info.setWordWrap(True)
        layout.addWidget(info)
        explain = QLabel("If the saves were made under another Windows user name, map that "
                         "folder to yours. Save Sync then asks Ludusavi to restore there.")
        explain.setWordWrap(True)
        layout.addWidget(explain)
        form = QFormLayout()
        self.source = QLineEdit()
        self.source.setPlaceholderText("C:/Users/<other user>")
        self.target = QLineEdit(os.path.expanduser("~").replace("\\", "/"))
        form.addRow("From:", self.source)
        form.addRow("To:", self.target)
        layout.addLayout(form)
        if row and row.restore_problem:
            guess = _profile_of(row.restore_problem)
            if guess:
                self.source.setText(guess)
        buttons = QHBoxLayout()
        self.add = QPushButton("Add mapping")
        self.exclude = QPushButton("Exclude this game")
        buttons.addWidget(self.add)
        buttons.addWidget(self.exclude)
        layout.addLayout(buttons)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self.add.clicked.connect(self._add)
        self.exclude.clicked.connect(self._exclude)

    def _add(self):
        source, target = self.source.text().strip(), self.target.text().strip()
        if not source or not target:
            return
        self.controller.add_redirect(recorded=source, local=target)
        QMessageBox.information(self, "Mapping added",
                                "Synchronize again: the game will be offered with the new path.")
        self.accept()

    def _exclude(self):
        self.controller.set_excluded(self.title, True)
        self.accept()


def _profile_of(problem: str):
    """`C:/Users/alice/AppData/x belongs to…` → `C:/Users/alice`."""
    import re
    match = re.search(r"([A-Za-z]:[\\/]Users[\\/][^\\/]+|/home/[^/]+|/Users/[^/]+)", problem or "")
    return match.group(1).replace("\\", "/") if match else None
