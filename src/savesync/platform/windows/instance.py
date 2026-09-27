"""One Save Sync per user session (plan §18.7), through a Windows named mutex."""
from __future__ import annotations

from ..interfaces import SingleInstanceGuard

ERROR_ALREADY_EXISTS = 183


class Kernel32Mutex:
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ctypes = ctypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel32.CreateMutexW.restype = wintypes.HANDLE
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def create(self, name: str):
        """(handle, already_existed)."""
        handle = self.kernel32.CreateMutexW(None, False, name)
        return handle, self._ctypes.get_last_error() == ERROR_ALREADY_EXISTS

    def close(self, handle) -> None:
        self.kernel32.CloseHandle(handle)


class NamedMutexGuard(SingleInstanceGuard):
    def __init__(self, name: str = "Local\\SaveSync-SingleInstance", api=None):
        self.name = name
        self.api = api or Kernel32Mutex()
        self._handle = None

    def acquire(self) -> bool:
        handle, existed = self.api.create(self.name)
        if not handle:
            return False
        if existed:
            self.api.close(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            self.api.close(self._handle)
            self._handle = None
