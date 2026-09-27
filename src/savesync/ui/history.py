"""Human-readable event history (plan §19.7); the JSONL log stays the diagnostic source."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLineEdit,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from .resources import GREEN, RED, YELLOW

COLUMNS = ["Time", "Game", "Direction", "Operation", "Result", "Duration", "Details"]
RESULT_COLORS = {"success": GREEN, "failed": RED, "rolled_back": YELLOW, "incomplete": YELLOW,
                 "refused": RED, "attention": YELLOW}


def entry_cells(entry: dict) -> list:
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(entry.get("timestamp"))))
    except (TypeError, ValueError):
        stamp = "?"
    direction = ""
    if entry.get("source") or entry.get("destination"):
        direction = "%s → %s" % (entry.get("source") or "?", entry.get("destination") or "?")
    duration = "%.1f s" % entry["duration"] if entry.get("duration") is not None else ""
    return [stamp, entry.get("game") or "", direction, (entry.get("operation") or "").upper(),
            (entry.get("result") or "").upper(), duration,
            entry.get("error") or entry.get("message") or ""]


class HistoryDialog(QDialog):
    _appended = Signal(dict)

    def __init__(self, log, parent=None, limit: int = 500):
        super().__init__(parent)
        self.log = log
        self.limit = limit
        self.setWindowTitle("Save Sync — History")
        self.resize(900, 480)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by game or text…")
        self.search.textChanged.connect(self.reload)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.reload)
        logs = QPushButton("Open logs folder")
        logs.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(os.path.dirname(self.log.path))))
        top.addWidget(self.search, 1)
        top.addWidget(refresh)
        top.addWidget(logs)
        layout.addLayout(top)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(len(COLUMNS) - 1,
                                                           QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        # the log may be written from the worker thread
        self._appended.connect(lambda _e: self.reload())
        self._listener = self._appended.emit
        log.subscribe(self._listener)
        self.reload()

    def done(self, code):
        self.log.unsubscribe(self._listener)
        super().done(code)

    def reload(self) -> None:
        needle = self.search.text().strip().lower()
        entries = self.log.tail(self.limit)
        rows = []
        for entry in entries:
            cells = entry_cells(entry)
            if needle and not any(needle in c.lower() for c in cells):
                continue
            rows.append((cells, entry.get("result") or ""))
        self.table.setRowCount(len(rows))
        for i, (cells, result) in enumerate(rows):
            for j, value in enumerate(cells):
                item = QTableWidgetItem(value)
                if j == 4 and result in RESULT_COLORS:
                    item.setForeground(QColor(RESULT_COLORS[result]))
                self.table.setItem(i, j, item)
