"""Application core and lifecycle (plan §5, §19, §23, §24).

`AppController` is the one object the UI talks to. It wires the removable-media
detector, the save-path watcher, the Ludusavi engine, the orchestrator, trial
mode, notifications and shutdown synchronization. Long operations run on one
worker thread; results come back to the Qt thread through signals.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from . import __version__
from .core import engine as engmod
from .core import usb as usbmod
from .core.engine import LudusaviEngine
from .core.log import EventLog
from .core.messages import msg, text
from .core.notifications import summarize
from .core.paths import AppPaths
from .core.registry import Config, Registry, title_key
from .core.safety import SafetyStore, human_size
from .core.state import GameResult, GameState, Outcome, SyncReport
from .core.sync import CancelToken, SyncService
from .core.trial import TrialManager
from .metadata.interfaces import CompositeMetadataProvider
from .metadata.ludusavi import LudusaviMetadataProvider
from .metadata.playnite import PlayniteMetadataProvider
from .platform.windows.process import ProcessHints

# How often "Wait and synchronize" looks at one specific game (user-requested,
# for that game only — not a permanent process monitor).
WAIT_POLL_MS = 5000
WAIT_MAX_SECONDS = 6 * 3600


@dataclass
class GameRow:
    key: str
    title: str
    state: GameState
    message: str = ""
    last_sync_ts: float | None = None
    usb_when: str = ""
    dirty: bool = False
    conflict: bool = False
    trial: dict | None = None
    excluded: bool = False
    error: str = ""
    save_paths: tuple = ()
    has_recovery: bool = False
    pc_has_save: bool = False
    usb_has_backup: bool = False
    restore_problem: str = ""
    platform: str | None = None
    cover: str | None = None

    @property
    def display_state(self) -> GameState:
        """A save the watcher saw change is a pending PC → USB, until a sync
        validates it."""
        if self.dirty and self.state == GameState.SYNCED:
            return GameState.LOCAL_NEWER
        return self.state


class UsbStatus:
    ABSENT = "absent"
    REGISTERED = "registered"
    UNKNOWN = "unknown"
    UNREGISTERED = "unregistered"
    UNINITIALIZED = "uninitialized"
    CORRUPT = "corrupt"


class TrayState:
    GREEN = "green"     # USB connected + synchronized
    YELLOW = "yellow"   # USB connected + pending changes
    RED = "red"         # conflict / error
    WHITE = "white"     # USB absent


class AppController(QObject):
    usbChanged = Signal()
    busyChanged = Signal(bool)
    syncStarted = Signal(str)
    syncProgress = Signal(str, int, int, str)
    syncFinished = Signal(object)          # SyncReport
    actionFinished = Signal(str, object)   # action name, GameResult
    gamesChanged = Signal()
    criticalCondition = Signal(str, object)  # kind, payload: the window may open
    runningGames = Signal(object)          # [title] after a user-started sync
    message = Signal(str)                  # status-bar text
    _drivesSignal = Signal(object)
    _watchSignal = Signal(str)
    _doneSignal = Signal(object, object)

    def __init__(self, paths: AppPaths, platform, notifier, engine_factory=None,
                 metadata=None, synchronous: bool = False, clock=time.time,
                 home: str | None = None):
        super().__init__()
        self.home = home  # user profile used to validate restore targets (None = real one)
        self.paths = paths.ensure()
        self.platform = platform
        self.notifier = notifier
        self.clock = clock
        self.synchronous = synchronous
        self.registry = Registry(paths.registry)
        self.config = Config(paths.config)
        self.log = EventLog(self.config.get("log_path") or paths.events)
        self.safety = SafetyStore(paths.safety, clock=clock)
        self._engine_factory = engine_factory or self._default_engine
        self._manifest = LudusaviMetadataProvider(self._manifest_path())
        self.metadata = metadata or self._default_metadata()
        self.drives = []
        self.drive = None
        self.usb_status = UsbStatus.ABSENT
        self.other_drive = None
        self.busy = False
        self.last_report: SyncReport | None = None
        self.last_problem: dict | None = None
        self._cancel = None
        self._engine = None
        self._waiting = {}
        self._skipped = set()
        self._started = False
        self._drivesSignal.connect(self._on_drives)
        self._watchSignal.connect(self._on_watch)
        self._doneSignal.connect(self._on_done)
        platform.process.hints = self._process_hints

    # --- construction helpers ------------------------------------------------

    def _ludusavi_config_dir(self) -> str:
        return (self.config.get("ludusavi_config_dir") or "").strip() or self.paths.ludusavi_config

    def _manifest_path(self) -> str:
        return os.path.join(self._ludusavi_config_dir(), "manifest.yaml")

    def _default_metadata(self):
        providers = [self._manifest]
        export = (self.config.get("playnite_export_path") or "").strip()
        if export:
            providers.insert(0, PlayniteMetadataProvider(export))
        return CompositeMetadataProvider(providers)

    def _default_engine(self, drive):
        """Engine for this drive, or raise with a user message."""
        cfg = self.config.load()
        command, source = usbmod.resolve_ludusavi(cfg, drive)
        if command is None:
            raise LookupError(text(msg("ludusavi_missing", detail=source)))
        config_dir = self._ludusavi_config_dir()
        if not (cfg.get("ludusavi_config_dir") or "").strip():
            engmod.prepare_config_dir(config_dir, drive.ludusavi_dir if drive else None)
        redirects = [tuple(r) for r in cfg.get("redirects") or []]
        applied = [tuple(r) for r in cfg.get("applied_redirects") or []]
        if redirects != applied and engmod.apply_redirects(config_dir, redirects, applied):
            self.config.update(applied_redirects=[list(r) for r in redirects])
        return LudusaviEngine(command, self.paths.safety, lock_path=self.paths.lock,
                              config_dir=config_dir)

    def _after_ludusavi(self, engine, drive, report) -> None:
        """Game-database housekeeping after a cycle with the real Ludusavi."""
        config_dir = getattr(engine, "config_dir", None)
        if not config_dir or self._engine_factory != self._default_engine:
            return
        if not engmod.has_manifest(config_dir) and not engmod.manifest_disabled(config_dir):
            # without its database Ludusavi recognizes no game at all — that must
            # not look like "nothing to synchronize"
            if not any(e.get("code") == "ludusavi_no_manifest" for e in report.errors):
                report.errors.append(msg("ludusavi_no_manifest"))
            return
        if engmod.share_manifest(config_dir, drive.ludusavi_dir):
            self.log.add("manifest", "Copied Ludusavi's game database to the USB for offline PCs.")
        self._manifest.manifest_path = self._manifest_path()

    def _process_hints(self, title: str) -> ProcessHints:
        hints = self._manifest.process_hints(title)
        rec = self.registry.get(title_key(title)) or {}
        names = {n.strip() for n in rec.get("process_names") or [] if n and n.strip()}
        return hints.merge(ProcessHints(exe_names=names))

    def service(self, drive=None) -> SyncService:
        engine = self._engine_factory(drive if drive is not None else self.drive)
        self._engine = engine
        return SyncService(self.registry, self.config, engine, self.platform.process,
                           self.log, safety=self.safety, clock=self.clock, home=self.home)

    # --- lifecycle --------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        count, freed = self.safety.cleanup(keep=self._protected())
        if count:
            self.log.add("cleanup", msg("safety_cleaned", count=count, size=human_size(freed)))
        cfg = self.config.load()
        if cfg.get("shutdown_incomplete"):
            self._notify("shutdown_incomplete", "Save Sync", text(msg("shutdown_incomplete")))
            self.config.update(shutdown_incomplete=False)
        self.platform.media.start_monitoring(self._drivesSignal.emit)
        self._apply_drives(self.platform.media.get_removable_drives(), initial=True)
        self.rewatch()
        self.apply_shutdown_setting()
        if cfg.get("first_run") or not (cfg.get("usb_id") or "").strip():
            self.criticalCondition.emit("setup", None)

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        for timer, _deadline in self._waiting.values():
            timer.stop()
        self._waiting.clear()
        self.cancel_sync("exit")
        try:
            self.platform.media.stop_monitoring()
        finally:
            self.platform.watcher.stop()
            self.platform.shutdown.unregister_handler()

    def _protected(self):
        keep = set()
        for rec in self.registry.all():
            trial = rec.get("trial") or {}
            keep.update(p for p in (trial.get("original"), trial.get("usb_copy")) if p)
            pending = rec.get("pending_op") or {}
            if pending.get("failed") and pending.get("snapshot"):
                keep.add(pending["snapshot"])
        return keep

    # --- USB ---------------------------------------------------------------------

    @Slot(object)
    def _on_drives(self, drives) -> None:
        self._apply_drives(drives)

    def _apply_drives(self, drives, initial: bool = False) -> None:
        self.drives = list(drives or [])
        cfg = self.config.load()
        previous = self.drive
        registered = usbmod.find_registered(self.drives, cfg)
        self.other_drive = None
        if registered is not None:
            self.drive, self.usb_status = registered, UsbStatus.REGISTERED
        else:
            self.drive = None
            self.usb_status = UsbStatus.ABSENT
            for drive in self.drives:
                match = usbmod.classify(drive, cfg)[0]
                if match in (usbmod.UsbMatch.UNKNOWN, usbmod.UsbMatch.CORRUPT,
                             usbmod.UsbMatch.UNREGISTERED):
                    self.other_drive = drive
                    self.usb_status = {usbmod.UsbMatch.UNKNOWN: UsbStatus.UNKNOWN,
                                       usbmod.UsbMatch.CORRUPT: UsbStatus.CORRUPT,
                                       usbmod.UsbMatch.UNREGISTERED: UsbStatus.UNREGISTERED}[match]
                    break
        self.usbChanged.emit()
        if registered is not None and (previous is None or
                                       previous.volume_serial != registered.volume_serial
                                       or previous.drive_letter != registered.drive_letter):
            if previous is None:
                m = msg("usb_connected", label=registered.label or registered.drive_letter)
                self.log.add("usb", m, result="connected", destination=registered.drive_letter)
                self._notify("usb_connected", "Save Sync", text(m))
                self._skipped.clear()
                if cfg.get("sync_on_usb_connect"):
                    self.sync_now(user=False)
        elif registered is None and previous is not None:
            self._on_usb_removed(previous)
        if registered is None and self.usb_status == UsbStatus.UNKNOWN and not initial:
            label = self.other_drive.label or self.other_drive.drive_letter
            self.message.emit(text(msg("usb_unknown", label=label)))

    def _on_usb_removed(self, drive) -> None:
        if self.busy:
            self.cancel_sync("usb_removed")
        pending = [r for r in self.rows() if r.display_state == GameState.LOCAL_NEWER]
        m = msg("usb_removed_pending") if pending else msg(
            "usb_removed", label=drive.label or drive.drive_letter)
        self.log.add("usb", m, result="removed", source=drive.drive_letter)
        if pending:
            self._notify("usb_removed_pending", "⚠ USB removed with pending changes", text(m))

    def register_usb(self, drive) -> dict:
        """Make `drive` the Save Sync USB (initializing its structure when needed)."""
        identity = usbmod.initialize(drive)
        self.config.update(first_run=False, **usbmod.registration_fields(drive, identity))
        m = msg("usb_registered", label=drive.label or drive.drive_letter)
        self.log.add("usb", m, result="registered")
        self._apply_drives(self.platform.media.get_removable_drives() or self.drives)
        return m

    def refresh_drives(self) -> None:
        self._apply_drives(self.platform.media.get_removable_drives())

    # --- worker ---------------------------------------------------------------------

    def _submit(self, work, done) -> bool:
        if self.busy:
            self.message.emit(text(msg("sync_locked", pid=os.getpid(), when="now")))
            return False
        self.busy = True
        self.busyChanged.emit(True)
        if self.synchronous:
            self._finish(done, self._run_safely(work))
            return True

        def runner():
            self._doneSignal.emit(done, self._run_safely(work))
        threading.Thread(target=runner, name="savesync-worker", daemon=True).start()
        return True

    @staticmethod
    def _run_safely(work):
        try:
            return work()
        except Exception as exc:  # never let the worker die silently
            return exc

    @Slot(object, object)
    def _on_done(self, done, result) -> None:
        self._finish(done, result)

    def _finish(self, done, result) -> None:
        self.busy = False
        self._cancel = None
        self.busyChanged.emit(False)
        try:
            done(result)
        finally:
            self.gamesChanged.emit()
            self.rewatch()

    def cancel_sync(self, reason: str = "cancelled") -> None:
        token = self._cancel
        if token is not None:
            token.cancel(reason)
        if self._engine is not None:
            self._engine.cancel()

    # --- synchronization ------------------------------------------------------------

    def sync_now(self, titles=None, user: bool = True, deadline: float | None = None,
                 on_done=None) -> bool:
        """`Sync Now`: runs the orchestrator directly — never Ludusavi's GUI."""
        drive = self.drive
        if drive is None:
            problem = msg("usb_absent") if self.usb_status == UsbStatus.ABSENT else msg(
                "usb_unknown", label=(self.other_drive.label if self.other_drive else "USB"))
            self.message.emit(text(problem))
            if user:
                self.criticalCondition.emit("usb", problem)
            return False
        token = CancelToken()

        def work():
            service = self.service(drive)
            self._cancel = token
            report = service.sync(drive, titles=titles, cancel=token, deadline=deadline,
                                  on_progress=lambda *a: self.syncProgress.emit(*a))
            self._after_ludusavi(service.engine, drive, report)
            return report

        def done(result):
            if isinstance(result, Exception):
                report = SyncReport(errors=[msg("ludusavi_failed", operation="synchronizing",
                                                detail=str(result))], completed=False)
            else:
                report = result
            self._after_sync(report, user)
            if on_done is not None:
                on_done(report)

        self.syncStarted.emit("Synchronizing…")
        return self._submit(work, done)

    def _after_sync(self, report: SyncReport, user: bool) -> None:
        self.last_report = report
        self.last_problem = report.errors[0] if report.errors else None
        for kind, title, body in summarize(report):
            self._notify(kind, title, body)
        self.syncFinished.emit(report)
        needs_decision = [g for g in report.games
                          if g.state in (GameState.CONFLICT, GameState.FIRST_SYNC)]
        rollback_failed = [g for g in report.games if g.message
                           and g.message.get("code") == "restore_rollback_failed"]
        if rollback_failed:
            self.criticalCondition.emit("error", rollback_failed)
        elif any(g.state == GameState.FIRST_SYNC for g in needs_decision):
            self.criticalCondition.emit("first_sync", needs_decision)
        elif needs_decision:
            self.criticalCondition.emit("conflict", needs_decision)
        running = [g.title for g in report.by_state(GameState.RUNNING)
                   if g.title not in self._skipped]
        if running and user:
            self.runningGames.emit(running)

    def shutdown_has_work(self) -> bool:
        return self.drive is not None and any(
            r.display_state in (GameState.LOCAL_NEWER, GameState.USB_NEWER) for r in self.rows())

    def shutdown_sync(self, deadline_seconds: float) -> bool:
        """Runs on the Qt thread inside WM_ENDSESSION; bounded by the deadline."""
        drive = self.drive
        if drive is None:
            return True
        deadline = time.monotonic() + max(1.0, float(deadline_seconds))
        token = CancelToken()
        self._cancel = token
        started = time.monotonic()
        try:
            report = self.service(drive).sync(drive, cancel=token, deadline=deadline)
        except Exception as exc:
            report = SyncReport(errors=[msg("ludusavi_failed", operation="shutting down",
                                            detail=str(exc))])
        self.last_report = report
        finished = report.completed and not report.errors
        self.log.add("shutdown", msg("sync_summary", synced=len(report.synchronized),
                                     attention=len(report.attention)),
                     result="success" if finished else "incomplete",
                     duration=time.monotonic() - started)
        if not finished:
            self.config.update(shutdown_incomplete=True)
        return finished

    def apply_shutdown_setting(self) -> None:
        cfg = self.config.load()
        if cfg.get("sync_on_shutdown"):
            self.platform.shutdown.register_handler(self.shutdown_sync, self.shutdown_has_work)
        else:
            self.platform.shutdown.unregister_handler()

    # --- explicit decisions --------------------------------------------------------------

    def _action(self, name: str, work, needs_usb: bool = True) -> bool:
        drive = self.drive
        if needs_usb and drive is None:
            problem = msg("usb_absent")
            self.actionFinished.emit(name, GameResult("", "", GameState.UNKNOWN,
                                                      Outcome.SKIPPED, problem))
            return False

        def done(result):
            if isinstance(result, Exception):
                result = GameResult("", "", GameState.ERROR, Outcome.FAILED,
                                    msg("ludusavi_failed", operation=name, detail=str(result)))
            if result.outcome in (Outcome.FAILED,) and result.message:
                self._notify("error", "Save Sync", text(result.message))
            self.actionFinished.emit(name, result)
        return self._submit(lambda: work(drive), done)

    def use_usb(self, title: str, backup_name: str | None = None) -> bool:
        return self._action("use_usb", lambda d: self.service(d).use_usb(d, title, backup_name))

    def use_pc(self, title: str) -> bool:
        return self._action("use_pc", lambda d: self.service(d).use_pc(d, title))

    def restore_version(self, title: str, backup_name: str) -> bool:
        return self._action("restore_version",
                            lambda d: self.service(d).restore_version(d, title, backup_name))

    def recover(self, title: str) -> bool:
        return self._action("recover", lambda d: self.service(d).recover_safety(title),
                            needs_usb=False)

    def trial_start(self, title: str) -> bool:
        return self._action("trial_start", lambda d: TrialManager(self.service(d)).start(d, title))

    def trial_switch(self, title: str, side: str) -> bool:
        return self._action("trial_switch",
                            lambda d: TrialManager(self.service(d)).switch(title, side),
                            needs_usb=False)

    def trial_keep(self, title: str, side: str) -> bool:
        return self._action("trial_keep",
                            lambda d: TrialManager(self.service(d)).keep(d, title, side),
                            needs_usb=(side == "pc"))

    def trial_cancel(self, title: str) -> bool:
        return self._action("trial_cancel",
                            lambda d: TrialManager(self.service(d)).cancel(title),
                            needs_usb=False)

    def usb_versions(self, title: str) -> list:
        """[BackupInfo] of `title` on the USB, newest first (runs Ludusavi)."""
        if self.drive is None:
            return []
        try:
            listing = self.service(self.drive).engine.backups(self.drive.backups_dir, [title])
        except Exception:
            return []
        return sorted((listing or {}).get(title) or [], key=lambda b: b.when, reverse=True)

    # --- running games -----------------------------------------------------------------

    def skip_game(self, title: str) -> None:
        """"Skip this game" for this USB session."""
        self._skipped.add(title)

    def wait_and_sync(self, title: str) -> None:
        """Poll ONLY this game until it exits, then synchronize it."""
        if title in self._waiting:
            return
        timer = QTimer(self)
        timer.setInterval(WAIT_POLL_MS)
        deadline = time.monotonic() + WAIT_MAX_SECONDS

        def check():
            if time.monotonic() > deadline:
                self._stop_waiting(title)
                return
            if self.busy or self.drive is None:
                return
            if not self.platform.process.is_running(title):
                self._stop_waiting(title)
                self.sync_now(titles=[title], user=True)
        timer.timeout.connect(check)
        self._waiting[title] = (timer, deadline)
        timer.start()

    def _stop_waiting(self, title):
        entry = self._waiting.pop(title, None)
        if entry:
            entry[0].stop()

    def waiting_for(self) -> list:
        return sorted(self._waiting)

    # --- watcher -----------------------------------------------------------------------

    def rewatch(self) -> None:
        paths = []
        for rec in self.registry.all():
            if not rec.get("excluded"):
                paths.extend(rec.get("save_paths") or [])
        try:
            self.platform.watcher.watch(paths, self._watchSignal.emit)
        except Exception:
            pass

    @Slot(str)
    def _on_watch(self, folder: str) -> None:
        folder = os.path.normcase(os.path.abspath(folder))
        changes = {}
        for rec in self.registry.all():
            for path in rec.get("save_paths") or []:
                norm = os.path.normcase(os.path.abspath(path))
                if norm == folder or norm.startswith(folder + os.sep) \
                        or folder.startswith(norm + os.sep):
                    changes[rec["title_key"]] = {"dirty": True}
                    break
        if changes:
            self.registry.update_many(changes)
            self.gamesChanged.emit()

    # --- views ------------------------------------------------------------------------

    def rows(self) -> list:
        out = []
        for rec in self.registry.all():
            message = rec.get("state_message")
            pending = rec.get("pending_op") or {}
            out.append(GameRow(
                key=rec["title_key"], title=rec["title"],
                state=GameState.parse(rec.get("state")),
                message=text(message) if message else "",
                last_sync_ts=rec.get("last_sync_ts"),
                usb_when=rec.get("last_synced_backup") or "",
                dirty=bool(rec.get("dirty")), conflict=bool(rec.get("conflict")),
                trial=rec.get("trial"), excluded=bool(rec.get("excluded")),
                error=rec.get("error") or "",
                save_paths=tuple(rec.get("save_paths") or ()),
                has_recovery=bool(pending.get("snapshot") or rec.get("last_safety")),
                pc_has_save=bool(rec.get("pc_has_save")),
                usb_has_backup=bool(rec.get("usb_has_backup")),
                restore_problem=rec.get("restore_problem") or "",
            ))
        return sorted(out, key=lambda r: r.title.lower())

    def row(self, title: str):
        return next((r for r in self.rows() if r.key == title_key(title)), None)

    def tray_state(self) -> str:
        if self.drive is None:
            return TrayState.WHITE
        rows = [r for r in self.rows() if not r.excluded]
        if self.last_problem is not None or any(
                r.display_state in (GameState.CONFLICT, GameState.ERROR) for r in rows):
            return TrayState.RED
        if any(r.display_state in (GameState.LOCAL_NEWER, GameState.USB_NEWER,
                                   GameState.FIRST_SYNC, GameState.MISSING_LOCAL_PATH,
                                   GameState.RUNNING, GameState.UNKNOWN) for r in rows) \
                or any(r.trial for r in rows):
            return TrayState.YELLOW
        return TrayState.GREEN

    def aggregate(self) -> dict:
        counts = {}
        for row in self.rows():
            if row.excluded:
                continue
            counts[row.display_state] = counts.get(row.display_state, 0) + 1
        return counts

    # --- settings helpers -------------------------------------------------------------

    def set_excluded(self, title: str, excluded: bool) -> None:
        self.registry.upsert({"title": title, "excluded": bool(excluded)})
        self.gamesChanged.emit()

    def set_process_names(self, title: str, names) -> None:
        self.registry.upsert({"title": title, "process_names": [n.strip() for n in names if n.strip()]})

    def add_redirect(self, recorded: str, local: str) -> None:
        """Map a folder recorded on another PC (e.g. C:/Users/alice) to this PC's
        (C:/Users/bob). Bidirectional, source = this PC: restores go to the local
        folder and backups keep recording the original one, so both PCs keep
        reading the same paths from the USB."""
        redirects = [list(r) for r in self.config.get("redirects") or []]
        entry = ["bidirectional", local, recorded]
        if entry not in redirects:
            redirects.append(entry)
        self.config.update(redirects=redirects)

    def safety_usage(self) -> tuple:
        items = self.safety.list()
        return len(items), sum(size for _p, _c, size in items)

    def clean_safety(self) -> tuple:
        count, freed = self.safety.cleanup(keep=self._protected(), max_age_days=0)
        self.log.add("cleanup", msg("safety_cleaned", count=count, size=human_size(freed)),
                     result="success")
        return count, freed

    def cover(self, title: str):
        try:
            return self.metadata.get_cover(title)
        except Exception:
            return None

    def platform_name(self, title: str):
        try:
            return self.metadata.get_platform(title)
        except Exception:
            return None

    def _notify(self, kind: str, title: str, body: str) -> None:
        prefs = self.config.get("notifications") or {}
        if prefs.get(kind, True):
            try:
                self.notifier.notify(title, body)
            except Exception:
                pass


# --- process entry point ---------------------------------------------------------------

SERVER_NAME = "SaveSync-Activate"


def _signal_existing_instance() -> bool:
    from PySide6.QtNetwork import QLocalSocket
    socket = QLocalSocket()
    socket.connectToServer(SERVER_NAME)
    if not socket.waitForConnected(500):
        return False
    socket.write(b"show\n")
    socket.flush()
    socket.waitForBytesWritten(500)
    socket.disconnectFromServer()
    return True


def _parse(argv):
    parser = argparse.ArgumentParser(prog="savesync", description="Save Sync %s" % __version__)
    parser.add_argument("--background", action="store_true",
                        help="start in the tray without opening the window")
    parser.add_argument("--fake-usb", metavar="DIR",
                        help="use DIR as the USB drive (development / demos)")
    parser.add_argument("--data-dir", metavar="DIR", help="Save Sync state folder")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    if args.data_dir:
        os.environ["SAVESYNC_HOME"] = os.path.abspath(args.data_dir)
    from PySide6.QtNetwork import QLocalServer
    from PySide6.QtWidgets import QApplication

    from .platform.factory import create_platform
    from .platform.windows.notify import TrayNotificationProvider
    from .ui.main_window import MainWindow
    from .ui.resources import app_icon
    from .ui.tray import TrayIcon

    qt = QApplication.instance() or QApplication(sys.argv[:1])
    qt.setApplicationName("Save Sync")
    qt.setQuitOnLastWindowClosed(False)
    qt.setWindowIcon(app_icon())
    paths = AppPaths().ensure()
    config = Config(paths.config)
    platform = create_platform(paths.root, shutdown_deadline=lambda: config.get(
        "shutdown_deadline_seconds"), fake_usb=args.fake_usb)
    if not platform.instance.acquire():
        _signal_existing_instance()
        return 0
    notifier = TrayNotificationProvider()
    controller = AppController(paths, platform, notifier)
    window = MainWindow(controller)
    tray = TrayIcon(controller, window)
    notifier.attach(tray)
    tray.show()

    server = QLocalServer()
    QLocalServer.removeServer(SERVER_NAME)
    server.listen(SERVER_NAME)
    server.newConnection.connect(lambda: (server.nextPendingConnection(), window.bring_to_front()))

    qt.aboutToQuit.connect(controller.stop)
    qt.aboutToQuit.connect(platform.instance.release)
    controller.start()
    if not args.background:
        window.bring_to_front()
    return qt.exec()
