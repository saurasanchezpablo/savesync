"""A deterministic world: one fake USB, several simulated PCs.

Every PC keeps its saves at the SAME absolute paths (like two PCs with the same
user name), so `switch_pc()` swaps the live save tree and Save Sync's own state
(registry, config, safety snapshots). The USB stays where it is, as a real
drive moved between PCs would.
"""
from __future__ import annotations

import os
import shutil

from savesync.core import usb as usbmod
from savesync.core.engine import LudusaviEngine
from savesync.core.log import EventLog
from savesync.core.paths import AppPaths
from savesync.core.registry import Config, Registry
from savesync.core.sync import SyncService
from savesync.core.trial import TrialManager
from savesync.platform.fake import FakeProcessChecker, fake_drive
from support import FakeLudusavi, read, write


class PC:
    def __init__(self, world, name):
        self.world = world
        self.name = name
        self.paths = AppPaths(os.path.join(world.root, "appdata-" + name)).ensure()
        self.registry = Registry(self.paths.registry)
        self.config = Config(self.paths.config)
        self.log = EventLog(self.paths.events)
        self.ludusavi = FakeLudusavi(world.games)
        self.engine = LudusaviEngine(["ludusavi"], self.paths.safety, runner=self.ludusavi,
                                     lock_path=self.paths.lock)
        self.process = FakeProcessChecker()
        self.service = SyncService(self.registry, self.config, self.engine, self.process,
                                   self.log, home=world.home)
        self.trials = TrialManager(self.service)

    def register(self, drive=None):
        drive = drive or self.world.drive
        identity = usbmod.initialize(drive)
        self.config.update(**usbmod.registration_fields(drive, identity))

    def sync(self, **kw):
        return self.service.sync(self.world.drive, **kw)

    def record(self, title):
        from savesync.core.registry import title_key
        return self.registry.get(title_key(title))


class World:
    def __init__(self, root, games=("Hades", "Celeste")):
        self.root = str(root)
        self.home = os.path.join(self.root, "live", "Users", "player")
        self.live = self.home
        os.makedirs(self.live, exist_ok=True)
        self.games = {title: [self.game_dir(title)] for title in games}
        usb_root = os.path.join(self.root, "usb")
        os.makedirs(usb_root, exist_ok=True)
        self.drive = fake_drive(usb_root)
        self.pcs = {}
        self.current = None

    def game_dir(self, title):
        return os.path.join(self.home, "AppData", "Roaming", title.replace(" ", "_"))

    def save_path(self, title, name="slot.sav"):
        return os.path.join(self.game_dir(title), name)

    def pc(self, name="A") -> PC:
        if name not in self.pcs:
            self.pcs[name] = PC(self, name)
        if self.current is None:
            self.current = name
        return self.pcs[name]

    def switch_pc(self, name) -> PC:
        """Carry the USB to PC `name`: its own save tree becomes the live one."""
        pc = self.pc(name)
        if self.current == name:
            return pc
        stash = os.path.join(self.root, "stash")
        os.makedirs(stash, exist_ok=True)
        shutil.move(self.live, os.path.join(stash, self.current))
        mine = os.path.join(stash, name)
        if os.path.isdir(mine):
            shutil.move(mine, self.live)
        else:
            os.makedirs(self.live)
        self.current = name
        return pc

    # save helpers
    def write_save(self, title, content, name="slot.sav"):
        return write(self.save_path(title, name), content)

    def read_save(self, title, name="slot.sav"):
        path = self.save_path(title, name)
        return read(path) if os.path.exists(path) else None

    def usb_versions(self, title):
        pc = self.pc(self.current)
        data = pc.ludusavi.read_mapping(self.drive.backups_dir, title)
        return data["backups"] if isinstance(data, dict) else []
