# Derived from decky-nonsteam-sync (py_modules/sdsync/saves.py),
# Copyright (c) 2026, JoseArkadio — BSD-3-Clause, see LICENSE-UPSTREAM.
"""Ludusavi adapter (plan §10.3).

Ludusavi is the discovery, backup, restore, versioning and validation engine;
this module only drives its CLI and refuses to read anything it cannot verify as
success. Behaviors below marked MEASURED were observed on Ludusavi 0.31.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from .messages import msg

LOCAL_TIMEOUT = 600
# Ludusavi checks for a manifest update on every call and, without network,
# aborts the whole command (measured upstream). This flag keeps updating when
# possible but stops a failed check from invalidating the work. It is global, so
# it goes before the subcommand.
MANIFEST_FLAG = ["--try-manifest-update"]
MAPPING_FILE = "mapping.yaml"
# Comments Save Sync writes on USB backups (`backups edit --comment`). They travel
# with the USB, so every PC sees which versions were validated and which ones
# were left behind by a failed or interrupted backup.
COMMENT_VALIDATED = "savesync:validated"
COMMENT_INCOMPLETE = "savesync:incomplete"
# Every safety snapshot lives in its own fresh directory, so one full backup is
# all it needs.
SAFETY_FULL_LIMIT = "1"
CHANGES_PRESENT = ("New", "Different", "Same")

_CREATE_NO_WINDOW = 0x08000000  # never flash a console from a tray application


class SubprocessRunner:
    """(code, stdout, stderr) — code -1 = did not start / did not finish.

    stdin=DEVNULL is mandatory: a Ludusavi waiting for confirmation on an
    inherited stdin hung for 11 minutes upstream, holding the sync lock the whole
    time. Output is decoded as UTF-8 explicitly: on Windows the locale code page
    would garble every non-ASCII game title in the JSON.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._current = None
        self._cancelled = False

    def __call__(self, argv: list, timeout: float = LOCAL_TIMEOUT):
        if self._cancelled:
            return -1, "", "cancelled before start: %s" % " ".join(argv)
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    **kwargs)
        except (FileNotFoundError, OSError) as exc:
            return -1, "", "could not start %s: %s" % (argv[0], exc)
        with self._lock:
            self._current = proc
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return -1, "", "timeout after %ss: %s" % (timeout, " ".join(argv))
        finally:
            with self._lock:
                self._current = None
        if self._cancelled:
            return -1, "", "cancelled: %s" % " ".join(argv)
        return (proc.returncode, out.decode("utf-8", errors="replace"),
                err.decode("utf-8", errors="replace"))

    def cancel(self) -> None:
        """Stop the running command (USB removed, shutdown deadline). Later calls
        fail immediately until `reset()`."""
        with self._lock:
            self._cancelled = True
            if self._current is not None:
                try:
                    self._current.kill()
                except OSError:
                    pass

    def reset(self) -> None:
        with self._lock:
            self._cancelled = False


# --- one synchronization at a time -------------------------------------------

class SyncLocked(Exception):
    """The synchronization lock is held elsewhere. Carries a `msg()` for the UI."""

    def __init__(self, problem):
        self.msg = problem if isinstance(problem, dict) else msg(
            "sync_lock_takeover_failed", detail=str(problem))
        super().__init__(self.msg["message"])


def _pid_alive(pid: int) -> bool:
    # os.kill(pid, 0) is NOT a probe on Windows: it terminates the process.
    try:
        import psutil
    except ImportError:  # pragma: no cover
        if sys.platform == "win32":
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    return psutil.pid_exists(pid)


def _lock_holder(path: str):
    """A message when a live process holds the lock; None when it can be taken
    over (unreadable, or left by a dead process — otherwise one killed process
    would block synchronization forever)."""
    try:
        with open(path, encoding="utf-8") as handle:
            pid_text, _, when = handle.read().strip().partition(" ")
        pid = int(pid_text)
    except (OSError, ValueError):
        return None
    if pid != os.getpid() and not _pid_alive(pid):
        return None
    return msg("sync_locked", pid=pid, when=when or "?")


@contextlib.contextmanager
def sync_lock(path: str):
    """File lock: one synchronization at a time per PC, across threads and
    processes. A held lock raises SyncLocked — never a silent skip."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError:
        holder = _lock_holder(path)
        if holder:
            raise SyncLocked(holder)
        try:
            os.unlink(path)
            fd = os.open(path, flags, 0o644)
        except OSError as exc:
            raise SyncLocked(msg("sync_lock_takeover_failed", detail=str(exc)))
    with os.fdopen(fd, "w") as handle:
        handle.write("%d %s" % (os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S")))
    try:
        yield
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# --- helpers ------------------------------------------------------------------

def _blank(title) -> bool:
    """Ludusavi without a game filter works on the whole library — an empty title
    never widens an operation to everything."""
    return not (title or "").strip()


def _filter(games) -> list:
    return [t.strip() for t in (games or []) if not _blank(t)]


def _args_with_titles(args: list, titles: list) -> list:
    # A title starting with "-" would be parsed as an option.
    if any(t.startswith("-") for t in titles):
        return args + ["--"] + titles
    return args + titles


# Characters Ludusavi does not put in a backup folder name. MEASURED upstream for
# the colon: "The Binding of Isaac: Rebirth" became "The Binding of Isaac_ Rebirth".
_UNSAFE_IN_DIR = ':/\\*?"<>|'


def backup_dir_name(title: str) -> str:
    text = (title or "").strip()
    for char in _UNSAFE_IN_DIR:
        text = text.replace(char, "_")
    return text


def when_of_folder(folder: str) -> str:
    """`backup-20260820T231843Z` → `2026-08-20T23:18:43Z`. Unknown shapes are
    returned unchanged rather than guessed."""
    stamp = (folder or "")[len("backup-"):]
    if stamp.endswith("-diff"):
        stamp = stamp[:-len("-diff")]
    if len(stamp) != 16 or stamp[8] != "T" or stamp[-1] != "Z":
        return folder or ""
    return "%s-%s-%sT%s:%s:%sZ" % (stamp[0:4], stamp[4:6], stamp[6:8],
                                   stamp[9:11], stamp[11:13], stamp[13:15])


def target_path_problem(path: str, home: str | None = None):
    """Why restoring to `path` is not safe on this PC, or None.

    Ludusavi restores to the absolute paths recorded on the source PC. A path is
    accepted when some meaningful part of it exists here: a missing drive, or a
    profile folder of a user that does not exist on this PC, is not a valid local
    save path (plan §12.7, §13).
    """
    candidate = os.path.abspath(str(path))
    ancestor = candidate
    while not os.path.exists(ancestor):
        parent = os.path.dirname(ancestor)
        if parent == ancestor:
            return "no part of %s exists on this PC" % candidate
        ancestor = parent
    if os.path.dirname(ancestor) == ancestor:
        return "only the drive root %s exists" % ancestor
    home_parent = os.path.dirname(os.path.abspath(home or os.path.expanduser("~")))
    if os.path.normcase(ancestor) == os.path.normcase(home_parent) \
            and os.path.normcase(candidate) != os.path.normcase(ancestor):
        return "%s belongs to a user profile that does not exist on this PC" % candidate
    return None


@dataclass(frozen=True)
class BackupInfo:
    name: str
    when: str
    locked: bool = False
    comment: str = ""
    os: str = ""

    @property
    def incomplete(self) -> bool:
        return self.comment == COMMENT_INCOMPLETE

    @property
    def validated(self) -> bool:
        return self.comment == COMMENT_VALIDATED


def latest_effective(backups):
    """Newest backup that was not left behind by a failed operation. `when` is
    ISO-8601 UTC, so text order is time order — no date parsing."""
    usable = [b for b in backups or [] if b.when and not b.incomplete]
    return max(usable, key=lambda b: b.when) if usable else None


def latest_any(backups):
    usable = [b for b in backups or [] if b.when]
    return max(usable, key=lambda b: b.when) if usable else None


@dataclass
class LocalGame:
    """One game as a backup preview sees it on this PC."""
    title: str
    change: str = ""
    decision: str = ""
    files: dict = field(default_factory=dict)
    registry: dict = field(default_factory=dict)
    registry_dump: str | None = None
    failed: bool = False

    @property
    def paths(self) -> list:
        # "Removed" = in the backup but no longer on this PC: not a local file
        return sorted(p for p, info in self.files.items()
                      if not (info or {}).get("ignored")
                      and (info or {}).get("change") != "Removed")

    @property
    def registry_keys(self) -> list:
        return sorted(k for k, info in self.registry.items()
                      if not (info or {}).get("ignored")
                      and (info or {}).get("change") != "Removed")

    @property
    def present(self) -> bool:
        return bool(self.paths or self.registry_keys)

    @property
    def same_as_target(self) -> bool:
        return self.change == "Same"


@dataclass
class OpResult:
    ok: bool
    files: dict = field(default_factory=dict)
    registry: dict = field(default_factory=dict)
    when: str = ""
    backup_name: str = ""
    changed: bool = False
    nothing: bool = False       # Ludusavi found nothing to act on
    created: list = field(default_factory=list)   # restore: files that did not exist
    problem: str = ""


def _validate_game(game, errors) -> str:
    """Empty string when Ludusavi confirms the game was processed without a single
    failed entry; otherwise the reason. Exit code 0 alone proves nothing (upstream
    measured "No saves found" with code 0), and a failed entry exits 1 with the
    reason only in the JSON (MEASURED) — so the JSON is always what decides."""
    if not game:
        return "Ludusavi did not report the game"
    if game.get("decision") != "Processed":
        return "Ludusavi decision was %s" % (game.get("decision") or "missing")
    files = game.get("files") or {}
    registry = game.get("registry") or {}
    if not files and not registry:
        return "Ludusavi reported no files"
    failed = [p for p, info in files.items() if (info or {}).get("failed")]
    failed += [k for k, info in registry.items() if (info or {}).get("failed")]
    if failed:
        first = failed[0]
        detail = ((files.get(first) or registry.get(first) or {}).get("error") or {})
        return "%d entr%s failed (%s: %s)" % (
            len(failed), "y" if len(failed) == 1 else "ies", first,
            detail.get("message") if isinstance(detail, dict) else detail)
    if (errors or {}).get("someGamesFailed"):
        return "Ludusavi reported that some games failed"
    return ""


class LudusaviEngine:
    def __init__(self, command: list, safety_path: str, runner=None, lock_path=None,
                 config_dir: str | None = None):
        self.command = list(command or ["ludusavi"])
        self.safety_path = os.path.abspath(safety_path)
        self.runner = runner or SubprocessRunner()
        # Ludusavi panics on a relative --config (MEASURED), so it is made absolute.
        self.config_dir = os.path.abspath(config_dir) if config_dir else None
        self.lock_path = lock_path or os.path.join(
            os.path.dirname(self.safety_path) or ".", "sync.lock")
        self.last_stderr = ""
        self.last_problem = ""
        # titles Ludusavi's database does not know, learned from the last calls
        self.unknown_titles = set()
        self._scan_cache = {}
        # time.monotonic() value after which no Ludusavi call may still run
        self.deadline = None

    # --- plumbing ---

    def lock(self):
        return sync_lock(self.lock_path)

    def cancel(self) -> None:
        cancel = getattr(self.runner, "cancel", None)
        if cancel:
            cancel()

    def reset(self) -> None:
        reset = getattr(self.runner, "reset", None)
        if reset:
            reset()

    def _argv(self, args: list) -> list:
        prefix = list(self.command)
        if self.config_dir:
            prefix += ["--config", self.config_dir]
        return prefix + MANIFEST_FLAG + args

    def _timeout(self, timeout: float) -> float:
        """A call never outlives the cycle's deadline (shutdown sync, plan §24)."""
        if self.deadline is None:
            return timeout
        return max(1.0, min(timeout, self.deadline - time.monotonic()))

    def _api(self, args: list, timeout: float = LOCAL_TIMEOUT):
        """(code, data|None). None = don't know."""
        code, out, err = self.runner(self._argv(args), self._timeout(timeout))
        self.last_stderr = err or ""
        for stream in (out, err):
            try:
                data = json.loads(stream)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, dict):
                self._note_unknown(data)
                return code, data
        if code != 0:
            self.last_problem = (err or out or "exit code %s" % code).strip()[-400:]
        return code, None

    def _call_known(self, call, wanted):
        """`call(titles) -> (code, data)`, repeated once without unknown titles.

        MEASURED: a list with one title Ludusavi's database does not know fails
        entirely — exit 1 and NO data even for the known titles. Those titles are
        remembered in `unknown_titles` and the call is made again without them.
        """
        code, data = call(wanted)
        if not wanted or data is None:
            return code, data
        unknown = set((data.get("errors") or {}).get("unknownGames") or [])
        if not unknown:
            return code, data
        rest = [t for t in wanted if t not in unknown]
        if not rest:
            return 0, {"games": {}, "errors": data.get("errors")}
        if len(rest) == len(wanted):
            return code, data
        return call(rest)

    def _note_unknown(self, data: dict) -> None:
        unknown = ((data.get("errors") or {}).get("unknownGames")) or []
        self.unknown_titles.update(str(t) for t in unknown if t)

    def version(self):
        """(ok, text). Used to tell "Ludusavi missing" apart from other failures."""
        code, out, err = self.runner(list(self.command) + ["--version"], 30)
        text = (out or err or "").strip()
        return code == 0 and bool(text), text

    # --- reading state ---

    def scan(self, path: str, titles=None, dump_registry: bool = True,
             timeout: float = LOCAL_TIMEOUT):
        """Backup preview against the backups in `path`: which games have saves on
        this PC, their files, and how they compare with the newest backup there.
        {title: LocalGame}, or None when the scan itself failed.

        `titles=None` scans the whole library (discovery, plan §12.2).
        """
        wanted = _filter(titles) if titles is not None else None
        if titles is not None and not wanted:
            return {}
        args = ["backup", "--preview", "--api", "--no-cloud-sync", "--path", path]
        if dump_registry:
            args.append("--dump-registry")
        code, data = self._call_known(
            lambda batch: self._api(_args_with_titles(args, batch or []), timeout), wanted)
        if code != 0 or data is None or "games" not in data:
            self.last_problem = (self.last_stderr.strip()[-400:] or self.last_problem
                                 or "the scan returned no data")
            return None
        out = {}
        for title, game in (data.get("games") or {}).items():
            game = game or {}
            files = game.get("files") or {}
            registry = game.get("registry") or {}
            out[title] = LocalGame(
                title=title, change=str(game.get("change") or ""),
                decision=str(game.get("decision") or ""), files=files,
                registry=registry,
                registry_dump=((game.get("dump") or {}).get("registry")),
                failed=any((i or {}).get("failed") for i in files.values())
                or any((i or {}).get("failed") for i in registry.values()))
        self._scan_cache.update(out)
        return out

    def backups(self, path: str, titles=None):
        """{title: [BackupInfo]} for the backups stored in `path`, or None."""
        wanted = _filter(titles) if titles is not None else None
        if titles is not None and not wanted:
            return {}
        code, data = self._call_known(
            lambda batch: self._api(_args_with_titles(["backups", "--api", "--path", path],
                                                      batch or [])), wanted)
        if code != 0 or data is None or "games" not in data:
            return None
        out = {}
        for title, entry in (data.get("games") or {}).items():
            out[title] = [BackupInfo(name=str(b.get("name") or ""),
                                     when=str(b.get("when") or ""),
                                     locked=bool(b.get("locked")),
                                     comment=str(b.get("comment") or ""),
                                     os=str(b.get("os") or ""))
                          for b in (entry or {}).get("backups") or []]
        return out

    def usb_when_many(self, titles, usb_path: str):
        """{title: `when` of the newest usable USB backup, or None when the USB has
        none}. None for the whole dict = don't know (never "the USB is empty")."""
        listing = self.backups(usb_path, titles)
        if listing is None:
            return None
        out = {}
        for title in _filter(titles):
            newest = latest_effective(listing.get(title))
            out[title] = newest.when if newest else None
        return out

    def usb_preview(self, title: str, usb_path: str, backup: str | None = None):
        """Restore preview: the files a USB → PC restore would write, keyed by their
        target path on this PC. None = unknown."""
        if _blank(title):
            return None
        args = ["restore", "--preview", "--api", "--no-cloud-sync", "--path", usb_path]
        if backup:
            args += ["--backup", backup]
        code, data = self._api(_args_with_titles(args, [title.strip()]))
        if data is None:
            return None
        game = (data.get("games") or {}).get(title.strip())
        if not game:
            return None
        return {"files": game.get("files") or {}, "registry": game.get("registry") or {},
                "change": game.get("change")}

    def get_save_paths(self, title: str) -> list:
        game = self._scan_cache.get((title or "").strip())
        return game.paths if game else []

    def discover_games(self, usb_path: str):
        """(local scan, USB listing) — the union of both is the game list, so a game
        backed up from another PC appears here too (plan §12.2)."""
        return self.scan(usb_path), self.backups(usb_path)

    # --- writing ---

    def edit_backup(self, title: str, path: str, backup: str, lock=None,
                    comment: str | None = None) -> bool:
        args = ["backups", "edit", "--path", path, "--backup", backup]
        if lock is True:
            args.append("--lock")
        elif lock is False:
            args.append("--unlock")
        if comment is not None:
            args += ["--comment", comment]
        code, _out, err = self.runner(self._argv(_args_with_titles(args, [title.strip()])),
                                      self._timeout(LOCAL_TIMEOUT))
        self.last_stderr = err or ""
        return code == 0

    def usb_backup(self, title: str, usb_path: str, full_limit: int = 2,
                   differential_limit: int = 1) -> OpResult:
        """PC → USB with Ludusavi's retention, never losing the previous version
        before the new one is validated (safety rule 7).

        MEASURED: a failed backup (a file that could not be read) still becomes a
        version AND counts toward retention — three failures pruned both good
        versions. So the last validated version is locked first (locked versions
        survive retention), the new one is locked only after validation, and a
        version left by a failed run is commented as incomplete so that no PC
        restores it or mistakes it for the newest save.
        """
        if _blank(title):
            return OpResult(False, problem="empty title")
        title = title.strip()
        before = self.backups(usb_path, [title])
        if before is None:
            return OpResult(False, problem="the USB backups could not be listed: %s"
                            % (self.last_problem or self.last_stderr.strip() or "unknown"))
        previous_list = before.get(title) or []
        previous = latest_effective(previous_list)
        newest_any = latest_any(previous_list)
        if previous is not None and not previous.locked:
            if not self.edit_backup(title, usb_path, previous.name, lock=True,
                                    comment=previous.comment or COMMENT_VALIDATED):
                return OpResult(False, problem="could not protect the previous USB version")
        diff_limit = differential_limit
        if newest_any is not None and newest_any.incomplete:
            # never build a differential on top of a version a failed run left
            diff_limit = 0
        args = ["backup", "--force", "--api", "--no-cloud-sync", "--path", usb_path,
                "--full-limit", str(max(2, int(full_limit))),
                "--differential-limit", str(max(0, int(diff_limit)))]
        code, data = self._api(_args_with_titles(args, [title]))
        after = self.backups(usb_path, [title])
        known = {b.name for b in previous_list}
        created = [b for b in (after or {}).get(title, []) if b.name not in known]
        new = latest_any(created)

        problem = ""
        game = None
        if data is None or "games" not in data:
            problem = "Ludusavi failed: %s" % (self.last_stderr.strip()[-300:]
                                               or self.last_problem or "exit code %s" % code)
        else:
            game = (data.get("games") or {}).get(title)
            if game is None:
                if title in self.unknown_titles:
                    problem = "Ludusavi does not know this game"
                elif new is None and code == 0:
                    return OpResult(False, nothing=True, problem="Ludusavi found no save")
                else:
                    problem = "Ludusavi did not report the game (exit code %s)" % code
            else:
                problem = _validate_game(game, data.get("errors")) or (
                    "Ludusavi exited with code %s" % code if code != 0 else "")
        if not problem and after is None:
            problem = "the USB backups could not be listed after the backup"
        if not problem and new is None and (game or {}).get("change") != "Same":
            problem = "no new USB version appeared"

        if problem:
            if new is not None:
                self.edit_backup(title, usb_path, new.name, comment=COMMENT_INCOMPLETE)
            self.last_problem = problem
            return OpResult(False, files=(game or {}).get("files") or {}, problem=problem)

        result = OpResult(True, files=game.get("files") or {},
                          registry=game.get("registry") or {},
                          changed=new is not None)
        # Without a new version Ludusavi reported "Same" against the NEWEST version
        # on the USB — even one a failed run left behind. Its content then equals the
        # live data, so it is re-validated instead of staying "incomplete" forever
        # (Ludusavi never writes a new version of unchanged data).
        adopted = new if new is not None else newest_any
        if adopted is None:
            return OpResult(False, problem="no USB version exists after the backup")
        if new is not None or not adopted.validated or not adopted.locked:
            locked = self.edit_backup(title, usb_path, adopted.name, lock=True,
                                      comment=COMMENT_VALIDATED)
        else:
            locked = True
        if locked and previous is not None and previous.name != adopted.name \
                and (previous.validated or not previous.locked):
            # release the old protection only once the new version holds it
            self.edit_backup(title, usb_path, previous.name, lock=False)
        result.when, result.backup_name = adopted.when, adopted.name
        return result

    def restore_from(self, title: str, path: str, backup: str | None = None) -> OpResult:
        """Restore `title` from the backups in `path`. Success only when Ludusavi
        confirms the game was processed with files and no failed entry: it exits 0
        even for "No saves found" (measured upstream)."""
        if _blank(title) or not (path or "").strip():
            return OpResult(False, problem="empty title or path")
        title = title.strip()
        args = ["restore", "--force", "--api", "--no-cloud-sync", "--path", path]
        if backup:
            args += ["--backup", backup]
        code, data = self._api(_args_with_titles(args, [title]))
        if data is None:
            return OpResult(False, problem="Ludusavi failed: %s" % (
                self.last_stderr.strip()[-300:] or self.last_problem or "exit code %s" % code))
        game = (data.get("games") or {}).get(title)
        problem = _validate_game(game, data.get("errors")) or (
            "Ludusavi exited with code %s" % code if code != 0 else "")
        files = (game or {}).get("files") or {}
        created = sorted(p for p, info in files.items()
                         if (info or {}).get("change") == "New")
        if problem:
            return OpResult(False, files=files, created=created, problem=problem)
        return OpResult(True, files=files, registry=(game or {}).get("registry") or {},
                        created=created, backup_name=backup or "")

    def usb_restore(self, title: str, usb_path: str, backup: str | None = None) -> OpResult:
        return self.restore_from(title, usb_path, backup)

    def safety_backup(self, title: str, snapshot_dir: str):
        if _blank(title):
            return False
        return self.safety_backup_many([title], snapshot_dir).get(title.strip(), False)

    def safety_backup_many(self, titles, snapshot_dir: str) -> dict:
        """{title: True/None/False}:
          True  = copied (processed, files, no failed entry),
          None  = nothing to protect (exit 0 and no entry: no local saves),
          False = failure.
        A restore may continue on True and None; only False blocks it. Callers
        MUST test `is False`, not falsiness.
        """
        wanted = _filter(titles)
        if not wanted or not (snapshot_dir or "").strip():
            return {}
        return self._batch_or_single(
            wanted, lambda batch: self._snapshot_call(batch, snapshot_dir), False)

    def _snapshot_call(self, wanted, snapshot_dir):
        code, data = self._api(_args_with_titles(
            ["backup", "--force", "--api", "--no-cloud-sync", "--path", snapshot_dir,
             "--full-limit", SAFETY_FULL_LIMIT, "--differential-limit", "0"], wanted))
        if code != 0 or data is None or "games" not in data:
            return None
        games = data.get("games") or {}
        out = {}
        for title in wanted:
            game = games.get(title)
            if game is None:
                out[title] = None
                continue
            out[title] = not _validate_game(game, {})
        return out

    def _batch_or_single(self, wanted, call, on_fail):
        """One batch call, and after its failure one call per game — so one bad
        title cannot turn every game into "don't know" (measured upstream)."""
        out = call(wanted)
        if out is not None:
            return out
        merged = {}
        for title in wanted:
            single = call([title])
            merged.update(single if single is not None else {title: on_fail})
        return merged


# --- Save Sync's private Ludusavi configuration -------------------------------

def standard_ludusavi_dir() -> str:
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "ludusavi")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                        "ludusavi")


def prepare_config_dir(config_dir: str, usb_ludusavi_dir: str | None = None,
                       seed_dir: str | None = None) -> None:
    """Make the private configuration usable before the first Ludusavi call.

    A fresh private directory is seeded once from the user's own Ludusavi
    configuration (roots, custom games), and a missing game database is copied from
    the USB so an offline PC can still identify games.
    """
    os.makedirs(config_dir, exist_ok=True)
    seed = seed_dir if seed_dir is not None else standard_ludusavi_dir()
    config_file = os.path.join(config_dir, "config.yaml")
    if not os.path.exists(config_file) and seed and os.path.isfile(os.path.join(seed, "config.yaml")):
        with contextlib.suppress(OSError):
            shutil.copy2(os.path.join(seed, "config.yaml"), config_file)
    manifest = os.path.join(config_dir, "manifest.yaml")
    if not os.path.exists(manifest):
        for source in (seed, usb_ludusavi_dir):
            candidate = os.path.join(source, "manifest.yaml") if source else ""
            if candidate and os.path.isfile(candidate):
                with contextlib.suppress(OSError):
                    shutil.copy2(candidate, manifest)
                break


def share_manifest(config_dir: str, usb_ludusavi_dir: str) -> bool:
    """Copy a newer game database onto the USB for PCs without network."""
    source = os.path.join(config_dir, "manifest.yaml")
    target = os.path.join(usb_ludusavi_dir, "manifest.yaml")
    try:
        if not os.path.isfile(source):
            return False
        if os.path.isfile(target) and os.path.getmtime(target) >= os.path.getmtime(source):
            return False
        os.makedirs(usb_ludusavi_dir, exist_ok=True)
        tmp = target + ".tmp"
        shutil.copy2(source, tmp)
        os.replace(tmp, target)
        return True
    except OSError:
        return False


def has_manifest(config_dir: str) -> bool:
    return os.path.isfile(os.path.join(config_dir, "manifest.yaml"))


# --- redirects (restoring to a different user profile) ------------------------

def _yaml_str(value: str) -> str:
    return '"%s"' % str(value).replace("\\", "\\\\").replace('"', '\\"')


def _redirect_items(block: list) -> list:
    items, current = [], None
    for line in block[1:]:
        if line.lstrip().startswith("- "):
            if current:
                items.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
    if current:
        items.append(current)
    return ["".join(item) for item in items]


def _render_redirect(kind: str, source: str, target: str) -> str:
    return "  - kind: %s\n    source: %s\n    target: %s\n" % (
        kind, _yaml_str(source), _yaml_str(target))


def redirects_yaml(config_text: str, wanted, previous=()) -> str:
    """config.yaml with Save Sync's redirects in place of the ones it wrote before.

    Text-based on purpose (no YAML dependency): `redirects` is a top-level key, so
    its block ends at the first unindented line. Entries Save Sync did not write
    (`previous`) belong to the user and survive untouched.
    """
    lines = config_text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.startswith("redirects:")), None)
    ours_before = {_render_redirect(*item) for item in previous}
    if start is None:
        head, tail, keep = lines, [], []
        if head and not head[-1].endswith("\n"):
            head = head + ["\n"]
    else:
        end = start + 1
        while end < len(lines) and (not lines[end].strip() or lines[end][:1].isspace()):
            end += 1
        head, tail = lines[:start], lines[end:]
        keep = [item for item in _redirect_items(lines[start:end]) if item not in ours_before]
    ours = [_render_redirect(kind, source, target) for kind, source, target in wanted
            if source and target]
    body = keep + [item for item in ours if item not in keep]
    block = "redirects:\n" + "".join(body) if body else "redirects: []\n"
    return "".join(head) + block + "".join(tail)


def apply_redirects(config_dir: str, wanted, previous=()) -> bool:
    wanted = list(wanted)
    path = os.path.join(config_dir, "config.yaml")
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        text = ""
    except OSError:
        return False
    if not text and not wanted:
        return True
    fresh = redirects_yaml(text, wanted, previous)
    if fresh == text:
        return True
    tmp = path + ".savesync-new"
    try:
        os.makedirs(config_dir, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(fresh)
        os.replace(tmp, path)
    except OSError:
        return False
    return True
