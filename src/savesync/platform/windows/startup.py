"""Start with Windows (plan §18.4): the per-user Run key, no administrator rights."""
from __future__ import annotations

import os
import sys

from ..interfaces import StartupManager

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "SaveSync"


class WinRegRunKey:
    """HKCU Run key through winreg; replaced by a dict-backed fake in tests."""

    def __init__(self):
        import winreg
        self.winreg = winreg

    def get(self, name: str):
        winreg = self.winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
                return winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            return None

    def set(self, name: str, value: str) -> None:
        winreg = self.winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def delete(self, name: str) -> None:
        winreg = self.winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


def launch_command() -> str:
    """How Windows should start Save Sync at logon: in the background (tray only)."""
    if getattr(sys, "frozen", False):  # PyInstaller build
        return '"%s" --background' % sys.executable
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.isfile(pythonw):
        exe = pythonw  # no console window at logon
    return '"%s" -m savesync --background' % exe


class WindowsStartupManager(StartupManager):
    def __init__(self, command: str | None = None, run_key=None):
        self.command = command or launch_command()
        self.run_key = run_key or WinRegRunKey()

    def is_enabled(self) -> bool:
        return bool(self.run_key.get(VALUE_NAME))

    def enable(self) -> None:
        self.run_key.set(VALUE_NAME, self.command)

    def disable(self) -> None:
        self.run_key.delete(VALUE_NAME)

    def refresh(self) -> bool:
        """Rewrite an enabled entry that points somewhere else (the portable folder
        moved). True when it was rewritten."""
        current = self.run_key.get(VALUE_NAME)
        if current and current != self.command:
            self.run_key.set(VALUE_NAME, self.command)
            return True
        return False
