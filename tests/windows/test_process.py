import os
import subprocess
import sys

import pytest

from savesync.metadata.ludusavi import LudusaviMetadataProvider
from savesync.platform.windows.process import ProcessHints, WindowsProcessChecker

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "fixtures", "ludusavi_manifest_excerpt.yaml")


def _checker(processes, hints):
    return WindowsProcessChecker(lambda title: hints.get(title, ProcessHints()),
                                 lister=lambda: processes)


def test_match_by_install_folder():
    checker = _checker([("bg3.exe", r"D:\Steam\steamapps\common\Baldurs Gate 3\bin\bg3.exe")],
                       {"BG3": ProcessHints(install_dirs={"Baldurs Gate 3"})})
    assert checker.is_running("BG3")


def test_match_by_exe_name_case_insensitive():
    checker = _checker([("Hades.EXE", "")], {"Hades": ProcessHints(exe_names={"hades.exe"})})
    assert checker.is_running("Hades")


def test_generic_exe_names_never_match_alone():
    checker = _checker([("Game.exe", r"C:\Other\Game.exe")],
                       {"X": ProcessHints(exe_names={"Game.exe"})})
    assert not checker.is_running("X")


def test_folder_must_match_whole_segment():
    checker = _checker([("a.exe", r"C:\Games\Hades II\a.exe")],
                       {"Hades": ProcessHints(install_dirs={"Hades"})})
    assert not checker.is_running("Hades")


def test_no_hints_is_not_running():
    assert not _checker([("x.exe", "C:/x.exe")], {}).is_running("Unknown")


def test_one_snapshot_per_cycle():
    calls = []

    def lister():
        calls.append(1)
        return [("hades.exe", "")]
    checker = WindowsProcessChecker(lambda t: ProcessHints(exe_names={"hades.exe"}), lister)
    checker.begin_cycle()
    for _ in range(5):
        assert checker.is_running("Hades")
    checker.end_cycle()
    assert len(calls) == 1


def test_manifest_hints(tmp_path):
    provider = LudusaviMetadataProvider(FIXTURE)
    hints = provider.process_hints("Animal Well")
    assert "Animal Well" in hints.install_dirs
    assert "Animal Well.exe" in hints.exe_names
    assert provider.get_ids("Animal Well") == {"steam": "813230"}
    assert provider.get_ids("Alien Breed") == {"gog": "1207663733"}, "a GOG id is not a Steam id"
    assert provider.process_hints("No Such Game").install_dirs == set()


def test_real_process_is_detected():
    """psutil against the real process table: a child process with a known name."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        exe = os.path.basename(sys.executable)
        checker = WindowsProcessChecker(lambda t: ProcessHints(
            install_dirs={os.path.basename(os.path.dirname(sys.executable))}))
        checker.begin_cycle()
        assert checker.is_running("Python")
    finally:
        proc.kill()
        proc.wait()
