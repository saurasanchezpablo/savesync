"""Conflict dialog (plan §15, §19.4): nothing happens until the user chooses."""
from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QVBoxLayout)

from .resources import RED, iso_text, when_text

USE_USB, USE_PC, EXPLORE = "use_usb", "use_pc", "explore"


class ConflictDialog(QDialog):
    def __init__(self, row, parent=None):
        super().__init__(parent)
        self.row = row
        self.choice = None
        self.setWindowTitle("Conflict — %s" % row.title)
        layout = QVBoxLayout(self)
        header = QLabel("⚠ CONFLICT")
        header.setStyleSheet("color: %s; font-weight: bold; font-size: 16px;" % RED)
        layout.addWidget(header)
        name = QLabel(row.title)
        name.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(name)
        layout.addWidget(QLabel("PC   → changed since the last synchronization"))
        layout.addWidget(QLabel("USB  → changed since the last synchronization"))
        layout.addWidget(QLabel("Last synchronized: %s (USB version %s)" % (
            when_text(row.last_sync_ts), iso_text(row.usb_when))))
        note = QLabel("Nothing has been overwritten. Before either side is replaced, a safety "
                      "snapshot of the current PC saves is taken, so the choice can be undone.")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QHBoxLayout()
        self.use_usb = QPushButton("Use USB")
        self.use_usb.setToolTip("Replace the PC saves with the USB version (PC saves are snapshotted first).")
        self.use_pc = QPushButton("Use PC")
        self.use_pc.setToolTip("Make the PC saves the newest USB version (older USB versions are kept).")
        self.explore = QPushButton("Explore both")
        self.explore.setToolTip("Trial mode: test each version in the game, then choose. Reversible.")
        for button, choice in ((self.use_usb, USE_USB), (self.use_pc, USE_PC),
                               (self.explore, EXPLORE)):
            button.clicked.connect(lambda _=False, c=choice: self._choose(c))
            buttons.addWidget(button)
        layout.addLayout(buttons)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        close.rejected.connect(self.reject)
        layout.addWidget(close)

    def _choose(self, choice):
        if choice in (USE_USB, USE_PC) and not self._confirm(choice):
            return
        self.choice = choice
        self.accept()

    def _confirm(self, choice) -> bool:
        side, other = ("USB", "PC") if choice == USE_USB else ("PC", "USB")
        answer = QMessageBox.question(
            self, "Confirm", "Keep the %s version of %s?\n\nThe %s version stays recoverable "
            "(safety snapshot / previous USB version)." % (side, self.row.title, other))
        return answer == QMessageBox.StandardButton.Yes
