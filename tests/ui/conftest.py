"""The complete application stack with substituted infrastructure (plan §26.6):

    UI → AppController → fake USB detector → FakeLudusavi runner → fake save tree
"""
import os

import pytest
from PySide6.QtWidgets import QMessageBox

from savesync.app import AppController
from savesync.core.engine import LudusaviEngine
from savesync.core.paths import AppPaths
from savesync.platform.factory import Platform
from savesync.platform.fake import (FakeFileWatcher, FakeProcessChecker,
                                    FakeRemovableMediaDetector, FakeShutdownIntegration,
                                    FakeSingleInstance, FakeStartupManager, RecordingNotifier)
from support import FakeLudusavi
from world import World


class Env:
    def __init__(self, tmp_path, qtbot, synchronous=True):
        self.world = World(tmp_path)
        self.qtbot = qtbot
        self.paths = AppPaths(os.path.join(self.world.root, "appdata"))
        self.ludusavi = FakeLudusavi(self.world.games)
        self.detector = FakeRemovableMediaDetector()
        self.process = FakeProcessChecker()
        self.watcher = FakeFileWatcher()
        self.shutdown = FakeShutdownIntegration()
        self.startup = FakeStartupManager()
        self.platform = Platform(self.detector, self.process, self.startup, self.shutdown,
                                 self.watcher, FakeSingleInstance("ui-test-%d" % id(self)))
        self.notifier = RecordingNotifier()
        self.engines = []
        self.controller = AppController(self.paths, self.platform, self.notifier,
                                        engine_factory=self._engine, synchronous=synchronous,
                                        home=self.world.home)
        self.window = None
        self.tray = None
        self.dialogs = []   # every dialog the window tried to exec
        self.answers = {}   # dialog class name -> callable(dialog) -> result

    def _engine(self, drive):
        engine = LudusaviEngine(["ludusavi"], self.paths.safety, runner=self.ludusavi,
                                lock_path=self.paths.lock)
        self.engines.append(engine)
        return engine

    def build_ui(self):
        from savesync.ui.main_window import MainWindow
        from savesync.ui.tray import TrayIcon
        self.window = MainWindow(self.controller)
        self.qtbot.addWidget(self.window)
        self.window.dialog_exec = self._exec
        self.tray = TrayIcon(self.controller, self.window)
        return self.window

    def _exec(self, dialog):
        self.dialogs.append(dialog)
        answer = self.answers.get(type(dialog).__name__)
        return answer(dialog) if answer else 0

    def register(self):
        self.controller.register_usb(self.world.drive)

    def connect(self):
        self.detector.connect(self.world.drive)
        self.controller.refresh_drives()

    def dialogs_of(self, name):
        return [d for d in self.dialogs if type(d).__name__ == name]


@pytest.fixture
def env(tmp_path, qtbot, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    e = Env(tmp_path, qtbot)
    yield e
    e.controller.stop()


@pytest.fixture
def ready(env):
    """Registered USB connected, UI built, controller started."""
    env.build_ui()
    env.register()
    env.controller.config.update(sync_on_usb_connect=False)
    env.controller.start()
    env.connect()
    return env
