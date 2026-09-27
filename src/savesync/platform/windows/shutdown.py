"""Synchronization during Windows shutdown (plan §18.5, §24).

Only active when the user enabled it. Protocol:

  WM_QUERYENDSESSION  → if there is work (registered USB connected and pending
                        changes), register a shutdown block reason
                        ("Synchronizing game saves…") and allow the shutdown;
  WM_ENDSESSION(TRUE) → run the sync with a hard deadline, then release the block.

Windows shows the reason on its shutdown screen and the user can always choose
"Shut down anyway"; the deadline bounds the work anyway, so shutdown is never
blocked indefinitely. What does not finish stays pending for the next start.
"""
from __future__ import annotations

from ..interfaces import ShutdownIntegration

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
REASON = "Synchronizing game saves with the USB…"


class User32Api:
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.ShutdownBlockReasonCreate.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        self.user32.ShutdownBlockReasonDestroy.argtypes = [wintypes.HWND]

    def block(self, hwnd: int, reason: str) -> bool:
        return bool(self.user32.ShutdownBlockReasonCreate(hwnd, reason))

    def unblock(self, hwnd: int) -> bool:
        return bool(self.user32.ShutdownBlockReasonDestroy(hwnd))


class ShutdownProtocol:
    """Pure message logic, independent of Qt and ctypes."""

    def __init__(self, api, hwnd=lambda: 0, deadline_seconds=lambda: 170):
        self.api = api
        self.hwnd = hwnd
        self.deadline_seconds = deadline_seconds
        self.callback = None
        self.precheck = None
        self.armed = False     # there is work to do at WM_ENDSESSION
        self.blocked = False   # a block reason is registered
        self.last_result = None

    def on_message(self, message: int, wparam: int):
        """(handled, result) for the window procedure."""
        if self.callback is None:
            return False, 0
        if message == WM_QUERYENDSESSION:
            try:
                work = bool(self.precheck()) if self.precheck else True
            except Exception:
                work = False
            if work:
                self.armed = True
                try:
                    self.blocked = bool(self.api.block(self.hwnd(), REASON))
                except Exception:
                    self.blocked = False  # sync anyway; Windows just shows no reason
            return True, 1  # never veto: the user decided to shut down
        if message == WM_ENDSESSION:
            if not self.armed:
                return False, 0
            try:
                if wparam:
                    try:
                        self.last_result = self.callback(self.deadline_seconds())
                    except Exception:
                        self.last_result = False
            finally:
                if self.blocked:
                    self.api.unblock(self.hwnd())
                self.armed = self.blocked = False
            return True, 0
        return False, 0


class WindowsShutdownIntegration(ShutdownIntegration):
    def __init__(self, deadline_seconds=lambda: 170, api=None):
        self.protocol = ShutdownProtocol(api or User32Api(), self._hwnd, deadline_seconds)
        self._widget = None
        self._filter = None

    def _hwnd(self) -> int:
        return int(self._widget.winId()) if self._widget is not None else 0

    def register_handler(self, callback, precheck=None) -> None:
        from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication
        from PySide6.QtWidgets import QWidget

        self.protocol.callback = callback
        self.protocol.precheck = precheck
        if self._filter is not None:
            return
        # WM_QUERYENDSESSION reaches top-level windows only; a tray application
        # may have none visible, so a hidden native window receives it
        self._widget = QWidget()
        self._widget.setWindowTitle("Save Sync shutdown")
        self._widget.winId()
        protocol = self.protocol

        class Filter(QAbstractNativeEventFilter):
            def nativeEventFilter(self, event_type, message):
                if event_type not in (b"windows_generic_MSG", "windows_generic_MSG"):
                    return False, 0
                import ctypes
                from ctypes import wintypes
                msg = wintypes.MSG.from_address(int(message))
                if msg.message not in (WM_QUERYENDSESSION, WM_ENDSESSION):
                    return False, 0
                return protocol.on_message(msg.message, msg.wParam)

        self._filter = Filter()
        QCoreApplication.instance().installNativeEventFilter(self._filter)

    def unregister_handler(self) -> None:
        self.protocol.callback = None
        self.protocol.precheck = None
        if self._filter is not None:
            from PySide6.QtCore import QCoreApplication
            app = QCoreApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(self._filter)
            self._filter = None
        if self._widget is not None:
            self._widget.deleteLater()
            self._widget = None
