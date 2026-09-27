"""Trial mode window (plan §15): test the USB version, test the PC version,
then keep one — or return to the original."""
from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QGridLayout, QLabel, QMessageBox, QPushButton,
                               QVBoxLayout)

from ..core.messages import text
from ..core.state import Outcome
from .resources import YELLOW, iso_text


class TrialDialog(QDialog):
    def __init__(self, controller, title: str, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.title = title
        self.setWindowTitle("Trial mode — %s" % title)
        layout = QVBoxLayout(self)
        banner = QLabel("⚠ Trial mode")
        banner.setStyleSheet("background: %s; color: black; font-weight: bold; padding: 6px;"
                             % YELLOW)
        layout.addWidget(banner)
        self.name = QLabel(title)
        self.name.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(self.name)
        self.side = QLabel()
        self.side.setWordWrap(True)
        layout.addWidget(self.side)
        hint = QLabel("Launch the game to check the version on the PC, then come back here. "
                      "Both versions are kept until you choose.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        grid = QGridLayout()
        self.test_usb = QPushButton("Test USB version")
        self.test_pc = QPushButton("Test PC version")
        self.keep_usb = QPushButton("Keep USB")
        self.keep_pc = QPushButton("Keep PC")
        self.cancel_trial = QPushButton("Cancel trial / return to original")
        grid.addWidget(self.test_usb, 0, 0)
        grid.addWidget(self.test_pc, 0, 1)
        grid.addWidget(self.keep_usb, 1, 0)
        grid.addWidget(self.keep_pc, 1, 1)
        grid.addWidget(self.cancel_trial, 2, 0, 1, 2)
        layout.addLayout(grid)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.test_usb.clicked.connect(lambda: self._run(controller.trial_switch, title, "usb"))
        self.test_pc.clicked.connect(lambda: self._run(controller.trial_switch, title, "pc"))
        self.keep_usb.clicked.connect(lambda: self._keep("usb"))
        self.keep_pc.clicked.connect(lambda: self._keep("pc"))
        self.cancel_trial.clicked.connect(lambda: self._run(controller.trial_cancel, title))
        controller.actionFinished.connect(self._on_action)
        controller.busyChanged.connect(lambda busy: self._refresh())
        self._refresh()

    def trial(self):
        row = self.controller.row(self.title)
        return row.trial if row else None

    def _refresh(self) -> None:
        trial = self.trial()
        busy = self.controller.busy
        if not trial:
            self.side.setText("No trial is active.")
            for b in (self.test_usb, self.test_pc, self.keep_usb, self.keep_pc, self.cancel_trial):
                b.setEnabled(False)
            return
        side = trial.get("side")
        if side == "starting":
            self.side.setText("The trial did not finish starting. Cancel it to return to "
                              "the original PC state.")
            for b in (self.test_usb, self.test_pc, self.keep_usb, self.keep_pc):
                b.setEnabled(False)
            self.cancel_trial.setEnabled(not busy)
            return
        self.side.setText("Now on the PC: the %s version.\nUSB version: %s" % (
            "USB" if side == "usb" else "original PC", iso_text(trial.get("usb_when"))))
        self.test_usb.setEnabled(not busy and side != "usb")
        self.test_pc.setEnabled(not busy and side != "pc")
        self.keep_usb.setEnabled(not busy)
        self.keep_pc.setEnabled(not busy and self.controller.drive is not None)
        self.cancel_trial.setEnabled(not busy)

    def _run(self, action, *args) -> None:
        self.status.setText("Working…")
        action(*args)
        self._refresh()

    def _keep(self, side: str) -> None:
        label = "USB" if side == "usb" else "PC"
        answer = QMessageBox.question(self, "Finish trial",
                                      "Keep the %s version of %s?" % (label, self.title))
        if answer == QMessageBox.StandardButton.Yes:
            self._run(self.controller.trial_keep, self.title, side)

    def _on_action(self, name: str, result) -> None:
        if not name.startswith("trial"):
            return
        self.status.setText(text(result.message))
        self._refresh()
        if name in ("trial_keep", "trial_cancel") and result.outcome not in (
                Outcome.FAILED, Outcome.SKIPPED):
            self.accept()

    def done(self, code):
        try:
            self.controller.actionFinished.disconnect(self._on_action)
        except (RuntimeError, TypeError):
            pass
        super().done(code)
