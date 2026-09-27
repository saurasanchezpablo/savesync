import os
import sys

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from savesync.core import usb as usbmod  # noqa: E402
from savesync.core.engine import LudusaviEngine  # noqa: E402
from savesync.core.paths import AppPaths  # noqa: E402
from savesync.core.registry import Config, Registry  # noqa: E402
from savesync.platform.fake import FakeRemovableMediaDetector, fake_drive  # noqa: E402
from support import FakeLudusavi  # noqa: E402


def pytest_addoption(parser):
    parser.addoption("--ludusavi-path", action="store", default=os.environ.get("LUDUSAVI_PATH", ""),
                     help="real Ludusavi executable for tests/integration")


@pytest.fixture
def ludusavi_path(request):
    path = request.config.getoption("--ludusavi-path")
    if not path or not os.path.isfile(path):
        pytest.skip("real Ludusavi not available (use --ludusavi-path or LUDUSAVI_PATH)")
    return os.path.abspath(path)


@pytest.fixture
def app_paths(tmp_path, monkeypatch):
    root = tmp_path / "appdata" / "SaveSync"
    monkeypatch.setenv("SAVESYNC_HOME", str(root))
    return AppPaths(str(root)).ensure()


@pytest.fixture
def registry(app_paths):
    return Registry(app_paths.registry)


@pytest.fixture
def config(app_paths):
    return Config(app_paths.config)


@pytest.fixture
def make_usb(tmp_path):
    """Factory for fake USB roots: `make_usb("E", serial="S1", initialize=True)`."""
    def factory(name="fake_usb", serial="FAKE-0001", label="SAVESYNC", initialize=True):
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        drive = fake_drive(str(root), serial=serial, label=label)
        identity = usbmod.initialize(drive) if initialize else None
        return drive, identity
    return factory


@pytest.fixture
def registered_usb(make_usb, config):
    drive, identity = make_usb()
    config.update(**usbmod.registration_fields(drive, identity))
    return drive


@pytest.fixture
def detector():
    return FakeRemovableMediaDetector()


@pytest.fixture
def local_root(tmp_path):
    root = tmp_path / "pc"
    root.mkdir()
    return root


@pytest.fixture
def fake_ludusavi():
    return FakeLudusavi()


@pytest.fixture
def engine(app_paths, fake_ludusavi):
    return LudusaviEngine(["ludusavi"], app_paths.safety, runner=fake_ludusavi,
                          lock_path=app_paths.lock)
