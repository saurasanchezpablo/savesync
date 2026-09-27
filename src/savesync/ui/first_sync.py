"""First-synchronization wizard (plan §13, §19.5).

A PC without a baseline cannot infer which state should win, so every game
needs an explicit choice. The baseline is created only after each operation
succeeded (the orchestrator does that).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
                               QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..core.state import GameState

SKIP, USE_USB, USE_PC, EXPLORE = "skip", "use_usb", "use_pc", "explore"
CHOICES = [(SKIP, "Decide later"), (USE_USB, "Use USB"), (USE_PC, "Use PC"),
           (EXPLORE, "Explore both")]


class FirstSyncWizard(QDialog):
    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self.setWindowTitle("First synchronization")
        self.rows = [r for r in rows if r.state == GameState.FIRST_SYNC]
        layout = QVBoxLayout(self)
        intro = QLabel("These games exist on this PC and/or the USB, but they have never been "
                       "synchronized here. No previously known state exists, so choose which "
                       "version to keep. Nothing changes until you apply.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = QTableWidget(len(self.rows), 4)
        self.table.setHorizontalHeaderLabels(["Game", "USB", "PC", "Choice"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.combos = []
        for i, row in enumerate(self.rows):
            self.table.setItem(i, 0, QTableWidgetItem(row.title))
            self.table.setItem(i, 1, QTableWidgetItem("existing backup" if row.usb_has_backup
                                                      else "no backup"))
            self.table.setItem(i, 2, QTableWidgetItem("existing save" if row.pc_has_save
                                                      else "no save"))
            combo = QComboBox()
            for value, label in CHOICES:
                if value in (USE_PC, EXPLORE) and not row.pc_has_save:
                    continue  # nothing on the PC to keep or explore
                combo.addItem(label, value)
            self.table.setCellWidget(i, 3, combo)
            self.combos.append(combo)
        layout.addWidget(self.table)
        bulk = QHBoxLayout()
        self.all_usb = QPushButton("Use USB for all")
        self.all_pc = QPushButton("Use PC for all")
        self.all_usb.clicked.connect(lambda: self.set_all(USE_USB))
        self.all_pc.clicked.connect(lambda: self.set_all(USE_PC))
        bulk.addWidget(self.all_usb)
        bulk.addWidget(self.all_pc)
        bulk.addStretch(1)
        layout.addLayout(bulk)
        buttons = QDialogButtonBox()
        self.apply_button = buttons.addButton("Apply", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(640, 420)

    def set_all(self, value: str) -> None:
        for combo in self.combos:
            index = combo.findData(value)
            if index >= 0:
                combo.setCurrentIndex(index)

    def set_choice(self, title: str, value: str) -> bool:
        for row, combo in zip(self.rows, self.combos):
            if row.title == title:
                index = combo.findData(value)
                if index >= 0:
                    combo.setCurrentIndex(index)
                    return True
        return False

    def choices(self) -> dict:
        """{title: choice} for every game that got a decision."""
        return {row.title: combo.currentData() for row, combo in zip(self.rows, self.combos)
                if combo.currentData() != SKIP}
