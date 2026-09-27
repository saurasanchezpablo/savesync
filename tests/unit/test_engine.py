"""Ludusavi adapter. Ported from upstream tests/test_saves.py (FakeRunner pattern,
real-output fixture) and extended for the USB model."""
import json
import os
import sys
import threading

import pytest

from savesync.core import engine as eng
from savesync.core.engine import (COMMENT_INCOMPLETE, COMMENT_VALIDATED, BackupInfo,
                                  LudusaviEngine, SubprocessRunner, SyncLocked,
                                  apply_redirects, backup_dir_name, latest_effective,
                                  redirects_yaml, sync_lock, target_path_problem,
                                  when_of_folder)
from support import FakeLudusavi, FakeRunner, read, write

_FIXTURE = os.path.join(os.path.dirname(__file__), "..", "fixtures", "ludusavi-real-output.txt")


def _real(section: str) -> str:
    """Verbatim output measured on Ludusavi 0.31 — never retyped from memory."""
    chunks, name = {}, None
    with open(_FIXTURE, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("===") and line.strip().endswith("==="):
                name = line.strip().strip("=")
                chunks[name] = ""
            elif name is not None:
                chunks[name] += line
    return chunks[section]


def _engine(replies=None, tmp_path=None, **kw):
    runner = FakeRunner(replies or {})
    safety = str(tmp_path / "safety") if tmp_path else "/tmp/savesync-test-safety"
    return LudusaviEngine(["ludusavi"], safety, runner=runner,
                          lock_path=str(tmp_path / "sync.lock") if tmp_path else None, **kw), runner


# --- argv ------------------------------------------------------------------------

def test_every_call_tolerates_manifest_update_failures_and_uses_its_config(tmp_path):
    engine, runner = _engine({"backups": (0, _real("BACKUPS_STDOUT"), "")}, tmp_path,
                             config_dir=str(tmp_path / "cfg"))
    engine.backups("E:/SaveSync/backups", ["Game A"])
    assert runner.calls[0][:4] == ["ludusavi", "--config", str(tmp_path / "cfg"),
                                   "--try-manifest-update"]


def test_config_dir_is_made_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    engine, _ = _engine({}, tmp_path, config_dir="relative")
    assert os.path.isabs(engine.config_dir)


def test_title_starting_with_dash_is_not_an_option(tmp_path):
    engine, runner = _engine({}, tmp_path)
    engine.scan("E:/b", ["-KLAUS-"])
    assert runner.calls[0][-2:] == ["--", "-KLAUS-"]


def test_blank_titles_never_widen_to_the_whole_library(tmp_path):
    engine, runner = _engine({}, tmp_path)
    assert engine.scan("E:/b", ["", "  "]) == {}
    assert engine.backups("E:/b", [None]) == {}
    assert engine.restore_from(" ", "E:/b").ok is False
    assert engine.usb_backup("", "E:/b").ok is False
    assert engine.safety_backup_many([""], "S") == {}
    assert runner.calls == []


# --- parsing real output -------------------------------------------------------

def test_scan_parses_real_preview(tmp_path):
    engine, _ = _engine({"backup --preview": (0, _real("BACKUP_PREVIEW_DIFFERENT_STDOUT"), "")},
                        tmp_path)
    games = engine.scan("C:/FX/usb")
    game = games["Game A"]
    assert game.change == "Different" and game.present and not game.failed
    assert [os.path.basename(p) for p in game.paths] == ["slot 1.sav", "slot 2.sav"]
    assert engine.get_save_paths("Game A") == game.paths


def test_scan_same_is_reported(tmp_path):
    engine, _ = _engine({"backup --preview": (0, _real("BACKUP_PREVIEW_SAME_STDOUT"), "")}, tmp_path)
    assert engine.scan("C:/FX/usb", ["Game A"])["Game A"].same_as_target


def test_scan_failure_is_none_never_empty(tmp_path):
    for reply in [(1, "", "boom"), (0, "not json", ""), (-1, "", "timeout"), (0, "[]", "")]:
        engine, _ = _engine({"backup --preview": reply}, tmp_path)
        assert engine.scan("E:/b") is None, reply


def test_backups_parse_lock_and_comment(tmp_path):
    engine, _ = _engine({"backups": (0, _real("BACKUPS_LOCKED_STDOUT"), "")}, tmp_path)
    backups = engine.backups("C:/FX/usb", ["Game A"])["Game A"]
    assert backups[-1].locked and backups[-1].validated
    assert not backups[0].locked
    assert latest_effective(backups).name.endswith("-diff")


def test_usb_when_many_distinguishes_missing_from_unknown(tmp_path):
    engine, _ = _engine({"backups": (0, _real("BACKUPS_STDOUT"), "")}, tmp_path)
    whens = engine.usb_when_many(["Game A", "Other"], "C:/FX/usb")
    assert whens["Game A"].startswith("2026-")
    assert whens["Other"] is None
    broken, _ = _engine({"backups": (1, "", "I/O error")}, tmp_path)
    assert broken.usb_when_many(["Game A"], "C:/FX/usb") is None


def test_corrupt_mapping_looks_empty_to_ludusavi(tmp_path):
    """MEASURED: why the orchestrator must cross-check the USB folders itself."""
    engine, _ = _engine({"backups": (0, _real("BACKUPS_CORRUPT_MAPPING_STDOUT"), "")}, tmp_path)
    assert engine.backups("C:/FX/usb-corrupt", ["Game A"]) == {}


def test_unknown_title_is_dropped_and_call_repeated(tmp_path):
    def backups_reply(argv):
        if "Nope" in argv:
            return 1, _real("UNKNOWN_GAME_STDOUT"), _real("UNKNOWN_GAME_STDERR")
        return 0, _real("BACKUPS_STDOUT"), ""
    engine, runner = _engine({"backups": backups_reply}, tmp_path)
    listing = engine.backups("C:/FX/usb", ["Game A", "Nope"])
    assert "Game A" in listing
    assert engine.unknown_titles == {"Nope"}
    assert len(runner.calls) == 2


def test_all_unknown_titles_are_an_empty_answer_not_a_failure(tmp_path):
    engine, _ = _engine({"backup --preview": (1, _real("UNKNOWN_GAME_STDOUT"), "")}, tmp_path)
    assert engine.scan("E:/b", ["Nope"]) == {}
    assert "Nope" in engine.unknown_titles


def test_restore_with_failed_entry_is_not_success(tmp_path):
    failed = json.loads(_real("BACKUP_FAILED_ENTRY_STDOUT"))
    engine, _ = _engine({"restore": (1, json.dumps(failed), "")}, tmp_path)
    result = engine.restore_from("Game A", "C:/FX/usb")
    assert not result.ok and "failed" in result.problem


def test_restore_of_real_output_succeeds(tmp_path):
    engine, _ = _engine({"restore": (0, _real("RESTORE_STDOUT"), "")}, tmp_path)
    result = engine.restore_from("Game A", "C:/FX/usb", backup="backup-x")
    assert result.ok
    assert result.created == [] and len(result.files) == 2


def test_restore_without_files_is_not_success(tmp_path):
    no_files = {"games": {"Game A": {"decision": "Processed", "change": "Same",
                                     "files": {}, "registry": {}}}}
    for reply in [(0, json.dumps(no_files), ""), (0, '{"games": {}}', ""), (0, "{}", "")]:
        engine, _ = _engine({"restore": reply}, tmp_path)
        assert not engine.restore_from("Game A", "C:/FX/usb").ok


def test_usb_preview_returns_targets(tmp_path):
    engine, runner = _engine({"restore --preview": (0, _real("RESTORE_PREVIEW_STDOUT"), "")}, tmp_path)
    preview = engine.usb_preview("Game A", "C:/FX/usb", backup="b1")
    assert len(preview["files"]) == 2
    assert "--backup" in runner.calls[0] and "b1" in runner.calls[0]


# --- runner ---------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="uses sh")
def test_runner_handles_timeout_missing_binary_and_stdin():
    runner = SubprocessRunner()
    code, _out, err = runner(["sh", "-c", "sleep 5"], timeout=0.3)
    assert code == -1 and "timeout" in err
    code, _out, err = runner(["/nonexistent/ludusavi"], timeout=5)
    assert code == -1 and "could not start" in err
    # stdin is /dev/null: a command waiting for input finishes instead of hanging
    code, out, _ = runner(["sh", "-c", "read x; echo got:$x"], timeout=5)
    assert code == 0 and out.strip() == "got:"


@pytest.mark.skipif(sys.platform == "win32", reason="uses sh")
def test_runner_decodes_utf8_titles():
    code, out, _ = SubprocessRunner()(["sh", "-c", "printf 'Marvel T\\305\\215kon'"], timeout=5)
    assert out == "Marvel Tōkon"


@pytest.mark.skipif(sys.platform == "win32", reason="uses sh")
def test_runner_cancel_stops_the_running_command():
    runner = SubprocessRunner()
    timer = threading.Timer(0.3, runner.cancel)
    timer.start()
    code, _o, err = runner(["sh", "-c", "sleep 10"], timeout=20)
    assert code == -1 and "cancel" in err
    assert runner(["true"], timeout=5)[0] == -1, "cancelled runner refuses new work"
    runner.reset()
    assert runner(["true"], timeout=5)[0] == 0


# --- USB backup protocol (safety rule 7) ------------------------------------------

@pytest.fixture
def game_env(tmp_path, app_paths):
    save = write(str(tmp_path / "pc" / "Hades" / "slot.sav"), "v1")
    fake = FakeLudusavi({"Hades": [os.path.dirname(save)]})
    engine = LudusaviEngine(["ludusavi"], app_paths.safety, runner=fake, lock_path=app_paths.lock)
    usb = str(tmp_path / "usb" / "SaveSync" / "backups")
    os.makedirs(usb)
    return engine, fake, usb, save


def _versions(fake, usb, title="Hades"):
    return fake.read_mapping(usb, title)["backups"]


def test_first_backup_is_locked_and_validated(game_env):
    engine, fake, usb, _save = game_env
    result = engine.usb_backup("Hades", usb)
    assert result.ok and result.changed and result.when
    (only,) = _versions(fake, usb)
    assert only["locked"] and only["comment"] == COMMENT_VALIDATED


def test_new_version_takes_over_the_lock(game_env):
    engine, fake, usb, save = game_env
    first = engine.usb_backup("Hades", usb)
    write(save, "v2")
    second = engine.usb_backup("Hades", usb)
    assert second.ok and second.when != first.when
    versions = {v["when"]: v for v in _versions(fake, usb)}
    assert versions[second.when]["locked"]
    assert not versions[first.when]["locked"], "the old protection is released"


def test_unchanged_backup_creates_no_version_and_keeps_identity(game_env):
    engine, fake, usb, _save = game_env
    first = engine.usb_backup("Hades", usb)
    again = engine.usb_backup("Hades", usb)
    assert again.ok and not again.changed and again.when == first.when
    assert len(_versions(fake, usb)) == 1


def test_failed_backups_never_prune_the_validated_version(game_env):
    """Rule 7. MEASURED hazard: failed runs still become versions and count toward
    retention; three of them pruned both good versions."""
    engine, fake, usb, save = game_env
    good = engine.usb_backup("Hades", usb, full_limit=2)
    extra = write(os.path.join(os.path.dirname(save), "broken.sav"), "x")
    fake.fail_paths.add(extra)
    for i in range(4):
        write(save, "attempt %d" % i)
        result = engine.usb_backup("Hades", usb, full_limit=2)
        assert not result.ok and "failed" in result.problem
    versions = _versions(fake, usb)
    whens = [v["when"] for v in versions]
    assert good.when in whens
    kept = next(v for v in versions if v["when"] == good.when)
    assert kept["locked"] and kept["comment"] == COMMENT_VALIDATED
    others = [v for v in versions if v["when"] != good.when]
    assert others and all(v["comment"] == COMMENT_INCOMPLETE for v in others)


def test_incomplete_versions_are_never_the_effective_latest(game_env):
    engine, fake, usb, save = game_env
    good = engine.usb_backup("Hades", usb)
    bad = write(os.path.join(os.path.dirname(save), "b.sav"), "x")
    fake.fail_paths.add(bad)
    write(save, "v2")
    engine.usb_backup("Hades", usb)
    assert engine.usb_when_many(["Hades"], usb) == {"Hades": good.when}


def test_differential_is_disabled_after_an_incomplete_version(game_env):
    engine, fake, usb, save = game_env
    engine.usb_backup("Hades", usb)
    bad = write(os.path.join(os.path.dirname(save), "b.sav"), "x")
    fake.fail_paths.add(bad)
    write(save, "v2")
    engine.usb_backup("Hades", usb, differential_limit=1)
    fake.fail_paths.clear()
    write(save, "v3")
    fake.calls.clear()
    assert engine.usb_backup("Hades", usb, differential_limit=1).ok
    backup_call = next(c for c in fake.calls if "backup" in c and "--force" in c)
    assert backup_call[backup_call.index("--differential-limit") + 1] == "0"


def test_unchanged_data_revalidates_an_incomplete_newest_version(game_env):
    """Ludusavi never writes a new version of unchanged data, so a version that
    turned out to match the live data must not stay "incomplete" forever."""
    engine, fake, usb, _save = game_env
    engine.usb_backup("Hades", usb)
    newest = _versions(fake, usb)[-1]
    newest["comment"] = COMMENT_INCOMPLETE
    newest["locked"] = False
    data = fake.read_mapping(usb, "Hades")
    data["backups"][-1] = newest
    fake._write_mapping(usb, "Hades", data)
    result = engine.usb_backup("Hades", usb)
    assert result.ok and result.when == newest["when"]
    assert _versions(fake, usb)[-1]["comment"] == COMMENT_VALIDATED


def test_backup_refuses_when_the_previous_version_cannot_be_protected(game_env):
    engine, fake, usb, save = game_env
    engine.usb_backup("Hades", usb)
    data = fake.read_mapping(usb, "Hades")
    data["backups"][-1]["locked"] = False
    fake._write_mapping(usb, "Hades", data)
    fake.hooks.append(lambda argv: (1, "", "lock failed") if "edit" in argv else None)
    write(save, "v2")
    result = engine.usb_backup("Hades", usb)
    assert not result.ok and "protect" in result.problem
    assert len(_versions(fake, usb)) == 1, "no backup was attempted"


def test_backup_with_unreadable_usb_listing_fails(game_env):
    engine, fake, usb, _save = game_env
    fake.unreadable_usb = True
    assert not engine.usb_backup("Hades", usb).ok


def test_backup_of_game_without_saves_is_nothing(game_env, tmp_path):
    engine, fake, usb, save = game_env
    os.remove(save)
    result = engine.usb_backup("Hades", usb)
    assert not result.ok and result.nothing


# --- restore / safety ----------------------------------------------------------------

def test_restore_reports_created_files_and_keeps_extra_ones(game_env):
    engine, fake, usb, save = game_env
    engine.usb_backup("Hades", usb)
    os.remove(save)
    extra = write(os.path.join(os.path.dirname(save), "extra.sav"), "mine")
    result = engine.usb_restore("Hades", usb)
    assert result.ok and result.created == [save]
    assert read(save) == "v1" and os.path.exists(extra)


def test_safety_contract_true_none_false(tmp_path, app_paths):
    a = write(str(tmp_path / "pc" / "A" / "a.sav"), "a")
    b = write(str(tmp_path / "pc" / "B" / "b.sav"), "b")
    fake = FakeLudusavi({"A": [os.path.dirname(a)], "B": [os.path.dirname(b)],
                         "Empty": [str(tmp_path / "pc" / "none")]})
    engine = LudusaviEngine(["ludusavi"], app_paths.safety, runner=fake)
    fake.fail_paths.add(b)
    snap = str(tmp_path / "snap")
    result = engine.safety_backup_many(["A", "B", "Empty"], snap)
    assert result == {"A": True, "B": False, "Empty": None}
    assert engine.safety_backup("A", str(tmp_path / "snap2")) is True


def test_safety_batch_failure_falls_back_to_single_calls(tmp_path, app_paths):
    calls = []

    def reply(argv):
        calls.append(argv)
        titles = argv[argv.index("0") + 1:]
        if len(titles) > 1:
            return 1, "", "batch failed"
        return 0, json.dumps({"games": {titles[0]: {"decision": "Processed",
                                                    "files": {"/x": {"change": "New"}}}}}), ""
    engine = LudusaviEngine(["ludusavi"], app_paths.safety, runner=FakeRunner({"backup": reply}))
    assert engine.safety_backup_many(["A", "B"], str(tmp_path / "s")) == {"A": True, "B": True}
    assert len(calls) == 3


# --- lock ------------------------------------------------------------------------------

def test_second_simultaneous_sync_is_refused(tmp_path):
    path = str(tmp_path / "sync.lock")
    with sync_lock(path):
        with pytest.raises(SyncLocked) as info:
            with sync_lock(path):
                pass
        assert info.value.msg["code"] == "sync_locked"
    with sync_lock(path):
        pass  # released


def test_two_threads_never_hold_the_lock_together(tmp_path):
    path = str(tmp_path / "sync.lock")
    inside, refused, lock = [], [], threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            with sync_lock(path):
                with lock:
                    inside.append(1)
                    assert len(inside) == 1
                threading.Event().wait(0.05)
                with lock:
                    inside.pop()
        except SyncLocked:
            refused.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert refused, "concurrent attempts must be refused, not queued silently"


def test_lock_of_a_dead_process_is_taken_over(tmp_path, monkeypatch):
    path = tmp_path / "sync.lock"
    path.write_text("999999 2026-01-01 00:00:00")
    monkeypatch.setattr(eng, "_pid_alive", lambda pid: False)
    with sync_lock(str(path)):
        assert path.read_text().startswith(str(os.getpid()))


def test_liveness_probe_uses_psutil_not_os_kill(monkeypatch):
    """os.kill(pid, 0) terminates the process on Windows — the probe must go
    through psutil, which uses OpenProcess there."""
    import psutil
    asked = []
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: asked.append(pid) or True)
    monkeypatch.setattr(os, "kill", lambda *a: pytest.fail("os.kill used as a probe"))
    assert eng._pid_alive(4242) is True
    assert asked == [4242]


# --- helpers ------------------------------------------------------------------------------

def test_backup_dir_name_and_when():
    assert backup_dir_name("The Binding of Isaac: Rebirth") == "The Binding of Isaac_ Rebirth"
    assert when_of_folder("backup-20260820T231843Z") == "2026-08-20T23:18:43Z"
    assert when_of_folder("backup-20260820T231843Z-diff") == "2026-08-20T23:18:43Z"
    assert when_of_folder("weird") == "weird"


def test_latest_effective_skips_incomplete():
    backups = [BackupInfo("a", "2026-01-01T00:00:00Z", comment=COMMENT_VALIDATED),
               BackupInfo("b", "2026-02-01T00:00:00Z", comment=COMMENT_INCOMPLETE)]
    assert latest_effective(backups).name == "a"
    assert latest_effective([]) is None


def test_target_path_validation(tmp_path):
    home = tmp_path / "Users" / "bob"
    (home / "AppData" / "Roaming").mkdir(parents=True)
    ok = home / "AppData" / "Roaming" / "NewGame" / "save.dat"
    assert target_path_problem(str(ok), home=str(home)) is None
    other_user = tmp_path / "Users" / "alice" / "AppData" / "x.sav"
    assert "user profile" in target_path_problem(str(other_user), home=str(home))
    root = os.path.abspath(os.sep)
    missing_drive = os.path.join(root, "definitely-missing-savesync-root", "x.sav")
    assert target_path_problem(missing_drive, home=str(home))


def test_redirects_keep_user_entries(tmp_path):
    user = "redirects:\n  - kind: restore\n    source: \"/mine\"\n    target: \"/theirs\"\nroots: []\n"
    out = redirects_yaml(user, [("restore", "C:/Users/alice", "C:/Users/bob")])
    assert '"/mine"' in out and '"C:/Users/alice"' in out and out.endswith("roots: []\n")
    replaced = redirects_yaml(out, [("restore", "C:/Users/carol", "C:/Users/bob")],
                              previous=[("restore", "C:/Users/alice", "C:/Users/bob")])
    assert "alice" not in replaced and "carol" in replaced and '"/mine"' in replaced
    assert apply_redirects(str(tmp_path / "cfg"), []) is True
    assert not (tmp_path / "cfg" / "config.yaml").exists()
    assert apply_redirects(str(tmp_path / "cfg"), [("restore", "a", "b")])
    assert '"a"' in read(str(tmp_path / "cfg" / "config.yaml"))


def test_prepare_config_dir_seeds_once_and_copies_manifest(tmp_path):
    seed = tmp_path / "seed"
    write(str(seed / "config.yaml"), "roots: [steam]\n")
    usb_dir = tmp_path / "usb" / "ludusavi"
    write(str(usb_dir / "manifest.yaml"), "Game: {}\n")
    cfg = tmp_path / "cfg"
    eng.prepare_config_dir(str(cfg), str(usb_dir), seed_dir=str(seed))
    assert read(str(cfg / "config.yaml")) == "roots: [steam]\n"
    assert eng.has_manifest(str(cfg))
    write(str(seed / "config.yaml"), "changed")
    eng.prepare_config_dir(str(cfg), str(usb_dir), seed_dir=str(seed))
    assert read(str(cfg / "config.yaml")) == "roots: [steam]\n", "seeded only once"


def test_share_manifest_only_when_newer(tmp_path):
    cfg, usb_dir = tmp_path / "cfg", tmp_path / "usb"
    write(str(cfg / "manifest.yaml"), "new")
    assert eng.share_manifest(str(cfg), str(usb_dir)) is True
    assert eng.share_manifest(str(cfg), str(usb_dir)) is False
