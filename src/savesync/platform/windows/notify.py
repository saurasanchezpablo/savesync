"""Notifications through the tray icon (plan §18.6).

On Windows 10/11 `QSystemTrayIcon.showMessage` becomes a native toast. The core
only sees the NotificationProvider interface.
"""
from __future__ import annotations

from ..interfaces import NotificationProvider


class TrayNotificationProvider(NotificationProvider):
    def __init__(self, tray_icon=None, timeout_ms: int = 8000):
        self.tray_icon = tray_icon
        self.timeout_ms = timeout_ms
        self.sent = []

    def attach(self, tray_icon) -> None:
        self.tray_icon = tray_icon

    def notify(self, title: str, message: str) -> None:
        self.sent.append((title, message))
        tray = self.tray_icon
        if tray is None:
            return
        try:
            from PySide6.QtWidgets import QSystemTrayIcon
            if QSystemTrayIcon.supportsMessages():
                tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information,
                                 self.timeout_ms)
        except Exception:
            pass
