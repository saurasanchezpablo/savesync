"""System tray (plan §19.1): four visual states, context menu, left-click opens
the main window."""
from __future__ import annotations

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .resources import TRAY_TOOLTIPS, tray_icon


class TrayIcon(QSystemTrayIcon):
    def __init__(self, controller, window, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.window = window
        self.state = None
        menu = QMenu()
        self.header = QAction("Save Sync", menu)
        self.header.setEnabled(False)
        self.sync_action = QAction("Sync Now", menu)
        self.status_action = QAction("View Status", menu)
        self.conflicts_action = QAction("Conflicts", menu)
        self.settings_action = QAction("Settings", menu)
        self.exit_action = QAction("Exit", menu)
        menu.addAction(self.header)
        menu.addSeparator()
        for action in (self.sync_action, self.status_action, self.conflicts_action,
                       self.settings_action):
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self.exit_action)
        self.menu = menu
        self.setContextMenu(menu)
        self.sync_action.triggered.connect(lambda: controller.sync_now(user=True))
        self.status_action.triggered.connect(window.bring_to_front)
        self.conflicts_action.triggered.connect(window.show_conflicts)
        self.settings_action.triggered.connect(lambda: (window.bring_to_front(), window.open_settings()))
        self.exit_action.triggered.connect(self.exit)
        self.activated.connect(self._on_activated)
        for signal in (controller.usbChanged, controller.gamesChanged, controller.syncFinished,
                       controller.busyChanged):
            signal.connect(lambda *_: self.update_state())
        self.update_state()

    def update_state(self) -> str:
        state = self.controller.tray_state()
        if state != self.state:
            self.state = state
            self.setIcon(tray_icon(state))
        tooltip = TRAY_TOOLTIPS[state]
        if self.controller.busy:
            tooltip = "Save Sync — synchronizing…"
        self.setToolTip(tooltip)
        self.sync_action.setEnabled(self.controller.drive is not None and not self.controller.busy)
        return state

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.window.bring_to_front()

    def exit(self) -> None:
        self.hide()
        QApplication.instance().quit()
