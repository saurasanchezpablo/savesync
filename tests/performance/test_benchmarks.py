"""Performance (plan §29): 10 / 50 / 100 / 200+ games, stages measured separately.

    pytest tests/performance -v                              # simulated Ludusavi
    pytest tests/performance -v --ludusavi-path=…            # + real Ludusavi
    pytest tests/performance --benchmark-only --benchmark-json=bench.json

Per-stage timings come from SyncReport.timings (USB listing, local scan,
classification, safety snapshot, registry); fingerprinting, USB detection and UI
model/render work are measured on their own.
"""
import os
import time

import pytest

from savesync.core import usb as usbmod
from savesync.core.engine import LudusaviEngine
from savesync.core.fingerprint import Fingerprint
from savesync.core.log import EventLog
from savesync.core.paths import AppPaths
from savesync.core.registry import Config, Registry
from savesync.core.state import Outcome
from savesync.core.sync import SyncService
from savesync.platform.fake import FakeProcessChecker, fake_drive
from support import FakeLudusavi, write

pytestmark = pytest.mark.benchmark
SIZES = [10, 50, 100, 200]
# three minutes minus a safety margin (plan §24)
SHUTDOWN_TARGET = 180


class Library:
    def __init__(self, root, count, runner_factory, files_per_game=3, size=16 * 1024):
        self.root = str(root)
        self.games = {}
        for i in range(count):
            title = "Game %03d" % i
            folder = os.path.join(self.root, "pc", "Game%03d" % i)
            for j in range(files_per_game):
                write(os.path.join(folder, "slot%d.sav" % j), os.urandom(size))
            self.games[title] = folder
        usb_root = os.path.join(self.root, "usb")
        os.makedirs(usb_root)
        self.drive = fake_drive(usb_root)
        identity = usbmod.initialize(self.drive)
        self.paths = AppPaths(os.path.join(self.root, "app")).ensure()
        self.config = Config(self.paths.config)
        self.config.update(**usbmod.registration_fields(self.drive, identity))
        self.engine = runner_factory(self)
        self.service = SyncService(Registry(self.paths.registry), self.config, self.engine,
                                   FakeProcessChecker(), EventLog(self.paths.events))

    def touch(self, count):
        for title in list(self.games)[:count]:
            write(os.path.join(self.games[title], "slot0.sav"), os.urandom(16 * 1024))


def _fake(lib):
    fake = FakeLudusavi({t: [p] for t, p in lib.games.items()})
    return LudusaviEngine(["ludusavi"], lib.paths.safety, runner=fake, lock_path=lib.paths.lock)


def _real_factory(exe):
    def factory(lib):
        config_dir = os.path.join(lib.root, "ludusavi-config")
        os.makedirs(config_dir)
        lines = ["manifest:", "  enable: false", "customGames:"]
        for title, path in lib.games.items():
            lines += ['  - name: "%s"' % title, "    files:", '      - "%s"' % path]
        with open(os.path.join(config_dir, "config.yaml"), "w") as fh:
            fh.write("\n".join(lines) + "\n")
        return LudusaviEngine([exe], lib.paths.safety, lock_path=lib.paths.lock,
                              config_dir=config_dir)
    return factory


def _report(label, count, seconds, timings):
    stages = ", ".join("%s %.2fs" % (k, v) for k, v in sorted(timings.items()))
    print("\n[%s] %d games: %.2fs (%s)" % (label, count, seconds, stages))


@pytest.mark.parametrize("count", SIZES + [400])
def test_cycle_overhead_simulated_ludusavi(tmp_path, benchmark, count):
    """Save Sync's own cost around Ludusavi: steady state (nothing changed)."""
    lib = Library(tmp_path, count, _fake)
    first = lib.service.sync(lib.drive)
    assert first.count(Outcome.BACKED_UP) == count
    report = benchmark.pedantic(lambda: lib.service.sync(lib.drive), rounds=3, iterations=1)
    assert report.success
    _report("simulated steady", count, report.finished - report.started, report.timings)


@pytest.mark.parametrize("count", SIZES)
def test_fingerprint_cost(tmp_path, benchmark, count):
    lib = Library(tmp_path, count, _fake)
    paths = []
    for folder in lib.games.values():
        paths.extend(os.path.join(folder, n) for n in os.listdir(folder))
    fp = Fingerprint()
    base = fp.compute(paths)
    changed = benchmark(lambda: fp.has_changed(base, paths))
    assert changed is False


@pytest.mark.parametrize("count", [200])
def test_usb_detection_cost(tmp_path, benchmark, count):
    lib = Library(tmp_path, 1, _fake)
    drives = [fake_drive(str(tmp_path / ("d%d" % i)), serial="S%d" % i) for i in range(8)]
    for d in drives:
        os.makedirs(d.drive_letter)
    drives.append(lib.drive)
    cfg = lib.config.load()
    found = benchmark(lambda: usbmod.find_registered(drives, cfg))
    assert found == lib.drive


@pytest.mark.parametrize("count", [200, 1000])
def test_ui_model_and_render_cost(qtbot, benchmark, count):
    from PySide6.QtGui import QImage
    from savesync.app import GameRow
    from savesync.core.state import GameState
    from savesync.ui.widgets import GameDelegate, GameFilterProxy, GameListModel
    from PySide6.QtWidgets import QListView
    states = list(GameState)
    rows = [GameRow("g%d" % i, "Game %d" % i, states[i % len(states)], "detail")
            for i in range(count)]
    model = GameListModel()
    proxy = GameFilterProxy()
    proxy.setSourceModel(model)
    view = QListView()
    qtbot.addWidget(view)
    view.setModel(proxy)
    view.setItemDelegate(GameDelegate(view))
    view.resize(700, 600)

    def render():
        model.set_rows(rows)
        proxy.set_states({GameState.CONFLICT})
        proxy.set_states(None)
        image = QImage(700, 600, QImage.Format.Format_ARGB32)
        view.render(image)  # the delegate paints every visible row
        return proxy.rowCount()
    assert benchmark(render) == count


@pytest.mark.ludusavi
@pytest.mark.parametrize("count", SIZES)
def test_real_ludusavi_cycles(tmp_path, ludusavi_path, count):
    """First upload, steady state, and a cycle with 10% of games changed."""
    lib = Library(tmp_path, count, _real_factory(ludusavi_path))
    t0 = time.monotonic()
    first = lib.service.sync(lib.drive)
    first_s = time.monotonic() - t0
    assert first.success, first.errors
    _report("real first upload", count, first_s, first.timings)
    t0 = time.monotonic()
    steady = lib.service.sync(lib.drive)
    _report("real steady", count, time.monotonic() - t0, steady.timings)
    assert steady.success
    lib.touch(max(1, count // 10))
    t0 = time.monotonic()
    changed = lib.service.sync(lib.drive)
    _report("real 10%% changed", count, time.monotonic() - t0, changed.timings)
    assert changed.count(Outcome.BACKED_UP) == max(1, count // 10)


@pytest.mark.ludusavi
def test_shutdown_sync_meets_three_minute_target(tmp_path, ludusavi_path):
    """200 games, 20 changed, with the shutdown deadline armed."""
    lib = Library(tmp_path, 200, _real_factory(ludusavi_path))
    assert lib.service.sync(lib.drive).success
    lib.touch(20)
    deadline_s = lib.config.get("shutdown_deadline_seconds")
    t0 = time.monotonic()
    report = lib.service.sync(lib.drive, deadline=time.monotonic() + deadline_s)
    elapsed = time.monotonic() - t0
    _report("shutdown", 200, elapsed, report.timings)
    assert report.completed and report.count(Outcome.BACKED_UP) == 20
    assert elapsed < SHUTDOWN_TARGET
