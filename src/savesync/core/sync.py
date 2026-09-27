# Orchestration ideas (phases, timing, per-game verdicts, safety before restore)
# derived from decky-nonsteam-sync (py_modules/sdsync/sync.py),
# Copyright (c) 2026, JoseArkadio — BSD-3-Clause, see LICENSE-UPSTREAM.
"""Synchronization orchestrator: USB ↔ PC (plan §12).

One cycle: check the registered USB, take the lock, list the USB, scan the PC
through Ludusavi, classify every game with the decision table, perform only the
safe operations, validate them, and move a game's baseline only after a
validated success. Anything uncertain is left untouched and reported.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

from . import usb as usbmod
from .decisions import decide
from .engine import (MAPPING_FILE, LocalGame, SyncLocked, backup_dir_name,
                     latest_any, latest_effective, target_path_problem)
from .fingerprint import Fingerprint, FingerprintError
from .messages import msg, text
from .registry import title_key
from .safety import SafetyStore
from .state import GameResult, GameState, Outcome, SyncReport

# Below this many seconds left, no new operation starts (shutdown sync).
MIN_OPERATION_SECONDS = 5.0


class CancelToken:
    """Set when the USB disappears or the shutdown deadline passes."""

    def __init__(self):
        self._event = threading.Event()
        self.reason = ""

    def cancel(self, reason: str = "cancelled") -> None:
        self.reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


@dataclass
class GameAnalysis:
    key: str
    title: str
    state: GameState
    message: dict | None = None
    local: LocalGame | None = None
    backups: list = field(default_factory=list)
    effective: object = None          # newest usable USB BackupInfo
    has_baseline: bool = False
    local_changed: object = None
    usb_ahead: object = None
    local_ahead: bool = False
    running: bool = False
    identical: bool = False           # live data equals the newest USB version
    fingerprint: str | None = None
    restore_problem: str | None = None
    skip: bool = False                # trial / unknown title / corrupt: never touched

    @property
    def usb_when(self) -> str:
        return self.effective.when if self.effective is not None else ""


def _mapping_title(path: str):
    """`name:` from a Ludusavi mapping.yaml, read as text (no YAML dependency)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("name:"):
                    value = line[len("name:"):].strip()
                    if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    return value or None
    except OSError:
        return None
    return None


def corrupt_usb_games(backups_dir: str, listing: dict) -> dict:
    """{folder name: (title or None, mapping path)} for USB game folders whose
    index Ludusavi could not read.

    MEASURED: a damaged mapping.yaml makes `backups --api` report nothing, which
    is indistinguishable from "the USB has no backup" — and would let this PC
    upload over it as if it were the first time, and retention then deletes the
    old versions. Keyed by FOLDER: a damaged file often has no readable `name:`,
    and the folder name ("Isaac_ Rebirth") is not the title ("Isaac: Rebirth").
    """
    listed_dirs = {backup_dir_name(t) for t in listing}
    listed = set(listing)
    out = {}
    try:
        entries = os.listdir(backups_dir)
    except OSError:
        return out
    for entry in entries:
        mapping = os.path.join(backups_dir, entry, MAPPING_FILE)
        if not os.path.isfile(mapping):
            continue
        name = _mapping_title(mapping)
        if (name and name in listed) or entry in listed_dirs:
            continue
        out[entry] = (name, mapping)
    return out


def protected_snapshots(registry) -> set:
    """Snapshots that cleanup must never remove: active trials (every version and
    checkpoint) and any recorded unfinished operation."""
    keep = set()
    for rec in registry.all():
        trial = rec.get("trial") or {}
        keep.update(p for p in (trial.get("original"), trial.get("usb_copy")) if p)
        keep.update(p for p in (trial.get("latest") or {}).values() if p)
        pending = rec.get("pending_op") or {}
        if pending.get("snapshot"):
            keep.add(pending["snapshot"])
    return keep


def _iso_seconds(when: str):
    import calendar
    try:
        return calendar.timegm(time.strptime((when or "")[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


class SyncService:
    def __init__(self, registry, config, engine, process_checker=None, log=None,
                 fingerprint: Fingerprint | None = None, safety: SafetyStore | None = None,
                 clock=time.time, home: str | None = None):
        self.registry = registry
        self.config = config
        self.engine = engine
        self.process = process_checker
        self.log = log
        self.fingerprint = fingerprint or Fingerprint()
        self.safety = safety or SafetyStore(engine.safety_path)
        self.clock = clock
        self.home = home
        self.last_report: SyncReport | None = None

    # --- small helpers -------------------------------------------------------

    def _cfg(self) -> dict:
        return self.config.load()

    def _usb_id(self) -> str:
        return (self._cfg().get("usb_id") or "").strip()

    def _event(self, operation, message=None, **fields):
        if self.log is not None:
            self.log.add(operation, message, **fields)

    def _is_running(self, title: str, fresh: bool = False) -> bool:
        """`fresh=True` right before saves are written: the cycle's process
        snapshot may be minutes old by then (rule 4)."""
        if self.process is None:
            return False
        try:
            if fresh:
                return bool(self.process.is_running_now(title))
            return bool(self.process.is_running(title))
        except Exception:
            # not knowing whether a game runs is treated as running: never touch it
            return True

    def _begin_processes(self):
        if self.process is not None:
            try:
                self.process.begin_cycle()
            except Exception:
                pass

    def _end_processes(self):
        if self.process is not None:
            try:
                self.process.end_cycle()
            except Exception:
                pass

    def _check_drive(self, drive):
        """None when `drive` is the registered USB, else a message (rule 3)."""
        if drive is None:
            return msg("usb_absent")
        cfg = self._cfg()
        if not (cfg.get("usb_id") or "").strip():
            return msg("usb_not_registered")
        match, _identity, detail = usbmod.classify(drive, cfg)
        if match == usbmod.UsbMatch.REGISTERED:
            return None
        if match == usbmod.UsbMatch.CORRUPT:
            return msg("usb_identity_corrupt", label=drive.label, detail=detail)
        return msg("usb_unknown", label=drive.label or drive.drive_letter)

    @staticmethod
    def _usb_present(drive) -> bool:
        return drive is not None and os.path.isdir(drive.backups_dir)

    def _extra(self, local: LocalGame | None):
        if local is not None and local.registry_dump:
            return {"registry": local.registry_dump}
        return None

    def _compute_fp(self, paths, extra=None):
        try:
            return self.fingerprint.compute(paths, extra)
        except FingerprintError:
            return None

    def _baseline_fields(self, when: str, fingerprint: str, paths) -> dict:
        now = self.clock()
        return {"last_synced_backup": when, "baseline_usb_id": self._usb_id(),
                "local_fingerprint": fingerprint or "", "save_paths": list(paths or []),
                "conflict": False, "pending_upload": False, "pending_op": None,
                "dirty": False, "error": "", "last_sync_ts": now,
                "state": GameState.SYNCED.value, "state_message": None}

    # --- analysis ------------------------------------------------------------

    def _analyze_all(self, drive, titles, fix_usb: bool, report: SyncReport):
        """{key: GameAnalysis}, or None when the cycle must not act at all."""
        usb_path = drive.backups_dir
        t0 = time.monotonic()
        listing = self.engine.backups(usb_path)
        report.timings["usb_listing"] = round(time.monotonic() - t0, 3)
        if listing is None:
            report.errors.append(msg("usb_backups_unreadable",
                                     detail=self.engine.last_problem or self.engine.last_stderr.strip()
                                     or "unknown"))
            return None
        corrupt = corrupt_usb_games(usb_path, listing)

        records = {r["title_key"]: r for r in self.registry.all()}
        wanted = None
        if titles is not None:
            wanted = {title_key(t) for t in titles}
            scan_titles = [t for t in titles if t]
        t0 = time.monotonic()
        scan = self.engine.scan(usb_path, None if titles is None else scan_titles)
        report.timings["local_scan"] = round(time.monotonic() - t0, 3)
        if scan is None:
            report.errors.append(msg("local_scan_failed",
                                     detail=self.engine.last_problem or "unknown"))
            return None

        usb_id = self._usb_id()
        universe = {}
        for title in list(scan) + list(listing):
            universe.setdefault(title_key(title), title)
        for key, rec in records.items():
            if rec.get("last_synced_backup") and rec.get("baseline_usb_id") == usb_id:
                universe.setdefault(key, rec["title"])
        # a damaged USB index blocks every game whose backup FOLDER it is
        corrupt_by_key = {}
        by_folder = {}
        for key, title in universe.items():
            by_folder.setdefault(backup_dir_name(title), []).append(key)
        for folder, (name, mapping) in corrupt.items():
            keys = by_folder.get(folder) or []
            if not keys:
                title = name or folder
                keys = [title_key(title)]
                universe.setdefault(keys[0], title)
            for key in keys:
                corrupt_by_key[key] = mapping
        if wanted is not None:
            universe = {k: t for k, t in universe.items() if k in wanted}
            for key in wanted - set(universe):
                rec = records.get(key)
                if rec:
                    universe[key] = rec["title"]

        t0 = time.monotonic()
        out = {}
        for key, title in sorted(universe.items(), key=lambda kv: kv[1].lower()):
            rec = records.get(key) or {"title_key": key, "title": title}
            if rec.get("excluded") and titles is None:
                continue
            out[key] = self._analyze_game(key, title, rec, scan.get(title), listing.get(title) or [],
                                          corrupt_by_key.get(key), drive, fix_usb)
        report.timings["classify"] = round(time.monotonic() - t0, 3)
        return out

    def _analyze_game(self, key, title, rec, local, backups, corrupt_path, drive, fix_usb):
        a = GameAnalysis(key=key, title=title, state=GameState.UNKNOWN, local=local,
                         backups=backups)
        if rec.get("trial"):
            a.state, a.skip = GameState.parse(rec.get("state")), True
            a.message = msg("state_trial", title=title)
            return a
        if corrupt_path:
            a.state, a.skip = GameState.ERROR, True
            a.message = msg("usb_backup_corrupt", title=title, path=corrupt_path)
            return a
        if rec.get("ludusavi_unknown") or title in self.engine.unknown_titles:
            a.state, a.skip = GameState.ERROR, True
            a.message = msg("ludusavi_unknown_title", title=title)
            return a

        pending = rec.get("pending_op") or {}
        if pending.get("kind") == "restore":
            # A restore that never finished (process killed, e.g. at shutdown):
            # the PC may hold a mix of both versions. Nothing automatic may use
            # it — the user recovers the snapshot or chooses a side explicitly.
            a.state, a.skip = GameState.ERROR, True
            a.message = msg("incomplete_operation", title=title, kind="restore")
            return a
        # An interrupted backup from THIS PC may have left an unvalidated version
        # on the USB; it is marked incomplete so no PC restores it (rule 5). Only
        # the version this PC created is touched — never another PC's.
        if fix_usb and pending.get("kind") == "backup":
            ours = self._interrupted_version(pending, backups)
            if ours is not None:
                self.engine.edit_backup(title, drive.backups_dir, ours.name,
                                        comment="savesync:incomplete")
                backups = [type(b)(b.name, b.when, b.locked, "savesync:incomplete", b.os)
                           if b is ours else b for b in backups]
                a.backups = backups

        a.effective = effective = latest_effective(backups)
        newest = latest_any(backups)
        usb_id = self._usb_id()
        baseline_when = rec.get("last_synced_backup") if rec.get("baseline_usb_id") == usb_id else ""
        a.has_baseline = bool(baseline_when)
        present = local is not None and local.present
        extra = self._extra(local)
        fp_error = False
        if present:
            a.fingerprint = self._compute_fp(local.paths, extra)
            fp_error = a.fingerprint is None
        # "Same" compares with the NEWEST version; it proves identity with the
        # effective one only when both are the same version.
        a.identical = bool(present and effective is not None and newest is effective
                           and local.same_as_target and not local.failed)
        a.local_ahead = bool(rec.get("pending_upload"))
        a.running = self._is_running(title)

        first_sync = False
        path_known = True
        if not a.has_baseline:
            if present and effective is None:
                a.local_changed, a.usb_ahead = True, False
            elif present and a.identical:
                a.local_changed, a.usb_ahead = False, False
            elif present:
                first_sync = True
            elif effective is not None:
                first_sync = True
                preview = self.engine.usb_preview(title, drive.backups_dir, backup=effective.name)
                if preview is None:
                    a.state = GameState.UNKNOWN
                    a.message = msg("state_unknown", title=title)
                    a.skip = True
                    return a
                problems = [target_path_problem(p, self.home) for p in preview["files"]]
                problems = [p for p in problems if p]
                if not preview["files"] and not preview["registry"]:
                    problems = ["the USB backup lists no file"]
                a.restore_problem = problems[0] if problems else None
                path_known = a.restore_problem is None
            else:
                a.skip = True
                a.state = GameState.UNKNOWN
                a.message = msg("state_unknown", title=title)
                return a
        else:
            if effective is None:
                # the USB lost this game's backup: uploading the PC state again
                # cannot destroy anything on the USB
                a.usb_ahead = False
                a.local_changed = True if present else None
            else:
                a.usb_ahead = effective.when != baseline_when
            if not present:
                path_known = False
            elif effective is not None:
                if local.failed or fp_error:
                    changed = None
                else:
                    changed = self.fingerprint.has_changed(rec.get("local_fingerprint"),
                                                           local.paths, extra)
                # Ludusavi's own comparison with the baseline version is the
                # authority when the newest USB version IS the baseline.
                if not a.usb_ahead and newest is effective and not local.failed \
                        and local.change in ("Same", "Different", "New"):
                    ludusavi_changed = local.change != "Same"
                    changed = ludusavi_changed if changed is None else (
                        True if ludusavi_changed else False)
                a.local_changed = changed

        if a.identical and not a.running:
            a.state = GameState.SYNCED
            a.message = msg("state_synced", title=title)
            return a
        a.state = decide(a.local_changed, a.usb_ahead, a.local_ahead, running=a.running,
                         first_sync=first_sync, local_path_known=path_known)
        a.message = self._state_message(a, present)
        return a

    @staticmethod
    def _interrupted_version(pending: dict, backups):
        """The version an interrupted upload of THIS PC left, or None.

        It is the one the failed upload reported, or else the first version
        appended after the ones known when the upload started, still without a
        Save Sync mark and written within minutes of the start (by this PC's
        clock, which is the one Ludusavi used for it).
        """
        known = {tuple(v) for v in pending.get("known") or []}
        created = pending.get("created")
        fresh = [b for b in backups if (b.name, b.when) not in known]
        for b in fresh:
            if created and b.name == created and not b.validated:
                return None if b.incomplete else b
        if created:
            return None
        started = pending.get("started")
        for b in fresh:
            if b.validated or b.incomplete or b.comment:
                return None
            stamp = _iso_seconds(b.when)
            if started is None or stamp is None or abs(stamp - float(started)) > 600:
                return None
            return b
        return None

    @staticmethod
    def _state_message(a: GameAnalysis, present: bool) -> dict:
        title = a.title
        if a.state == GameState.MISSING_LOCAL_PATH:
            if a.has_baseline and not present:
                return msg("state_local_saves_missing", title=title)
            return msg("state_missing_local_path", title=title)
        return msg("state_" + a.state.value, title=title)

    # --- the cycle -------------------------------------------------------------

    def analyze(self, drive, titles=None) -> SyncReport:
        return self.sync(drive, titles=titles, analyze_only=True)

    def sync(self, drive, titles=None, analyze_only: bool = False,
             cancel: CancelToken | None = None, deadline: float | None = None,
             on_progress=None) -> SyncReport:
        """`deadline` is a time.monotonic() value; no operation starts after it."""
        report = SyncReport(started=self.clock(), analyzed_only=analyze_only)
        cancel = cancel or CancelToken()
        progress = on_progress or (lambda *a: None)
        problem = self._check_drive(drive)
        if problem is not None:
            report.errors.append(problem)
            report.finished = self.clock()
            self._event("sync", problem, result="refused")
            self.last_report = report
            return report
        try:
            with self.engine.lock():
                self.engine.deadline = deadline
                self.engine.reset()
                self._begin_processes()
                try:
                    self._run(drive, titles, analyze_only, cancel, deadline, progress, report)
                finally:
                    self._end_processes()
                    self.engine.deadline = None
        except SyncLocked as exc:
            report.errors.append(exc.msg)
        report.finished = self.clock()
        if not analyze_only:
            self._event("sync", msg("sync_summary", synced=len(report.synchronized),
                                    attention=len(report.attention)),
                        result="success" if report.success else (
                            "incomplete" if not report.completed else "attention"),
                        duration=report.finished - report.started,
                        error="; ".join(text(e) for e in report.errors))
            if report.completed:
                self.config.update(last_sync_ts=report.finished)
        self.last_report = report
        return report

    def _run(self, drive, titles, analyze_only, cancel, deadline, progress, report):
        progress("analyzing", 0, 0, "")
        check = getattr(self.engine, "check_version", None)
        problem = check() if check else None
        if problem:
            report.errors.append(msg("ludusavi_missing", detail=problem))
            return
        analyses = self._analyze_all(drive, titles, not analyze_only, report)
        if analyses is None:
            return
        # unknown titles learned during this cycle are remembered, so the next
        # cycle does not pay the failed batch call again
        updates = {}
        for key, a in analyses.items():
            if a.title in self.engine.unknown_titles:
                updates.setdefault(key, {"title": a.title})["ludusavi_unknown"] = True

        restore_batch = [a for a in analyses.values() if a.state == GameState.USB_NEWER
                         and not analyze_only]
        safety_dir, safety = None, {}
        if restore_batch and not cancel.cancelled:
            safety_dir = self.safety.new_snapshot("restore")
            t0 = time.monotonic()
            safety = self.engine.safety_backup_many([a.title for a in restore_batch], safety_dir)
            report.timings["safety"] = round(time.monotonic() - t0, 3)

        total = len(analyses)
        stopped = None
        for done, (key, a) in enumerate(analyses.items(), 1):
            progress("synchronizing", done, total, a.title)
            if stopped is None and not analyze_only:
                if cancel.cancelled:
                    stopped = msg("usb_removed_during_sync") if cancel.reason == "usb_removed" \
                        else msg("sync_cancelled")
                elif deadline is not None and deadline - time.monotonic() < MIN_OPERATION_SECONDS:
                    stopped = msg("sync_deadline")
                elif not self._usb_present(drive):
                    stopped = msg("usb_removed_during_sync")
            if analyze_only:
                result = GameResult(key, a.title, a.state, Outcome.NOTHING, a.message)
                fields = {"title": a.title, "state": a.state.value, "state_message": a.message}
            elif stopped is not None:
                # nothing pending is marked synchronized: the game keeps its
                # analyzed state (LOCAL_NEWER / USB_NEWER stay pending)
                moving = a.state in (GameState.LOCAL_NEWER, GameState.USB_NEWER)
                result = GameResult(key, a.title, a.state, Outcome.SKIPPED,
                                    stopped if moving else a.message)
                fields = {"title": a.title, "state": a.state.value, "state_message": a.message}
            else:
                started = time.monotonic()
                result, fields = self._act(drive, a, safety_dir, safety.get(a.title, False))
                result.duration = round(time.monotonic() - started, 3)
                report.timings["operations"] = round(
                    report.timings.get("operations", 0.0) + result.duration, 3)
                if result.outcome == Outcome.FAILED and not self._usb_present(drive):
                    stopped = msg("usb_removed_during_sync")
            seen = {"title": a.title, "pc_has_save": bool(a.local is not None and a.local.present),
                    "usb_has_backup": a.effective is not None,
                    "restore_problem": a.restore_problem or ""}
            if a.local is not None and a.local.present:
                seen["save_paths"] = a.local.paths
            entry = updates.setdefault(key, {})
            entry.update(seen)
            entry.update(fields)
            if result.outcome not in (Outcome.NOTHING, Outcome.SKIPPED):
                # persisted at once: a validated result must survive the process
                # being killed later in the cycle (shutdown sync)
                self.registry.update_many({key: dict(entry)})
            report.games.append(result)
        if stopped is not None:
            report.errors.append(stopped)
        report.completed = stopped is None
        t0 = time.monotonic()
        self.registry.update_many(updates)
        report.timings["registry"] = round(time.monotonic() - t0, 3)

    # --- per-game operations -----------------------------------------------------

    def _act(self, drive, a: GameAnalysis, safety_dir, safety_ok):
        base = {"title": a.title, "state": a.state.value, "state_message": a.message}
        if a.skip:
            return GameResult(a.key, a.title, a.state, Outcome.SKIPPED, a.message), base
        if a.state == GameState.SYNCED:
            return self._confirm_synced(a)
        if a.state == GameState.LOCAL_NEWER:
            return self._upload(drive, a)
        if a.state == GameState.USB_NEWER:
            return self._download(drive, a, safety_dir, safety_ok)
        if a.state == GameState.CONFLICT:
            base["conflict"] = True
        return GameResult(a.key, a.title, a.state, Outcome.SKIPPED, a.message), base

    def _confirm_synced(self, a: GameAnalysis):
        rec = self.registry.get(a.key) or {}
        fields = self._baseline_fields(a.usb_when, a.fingerprint, a.local.paths if a.local else [])
        created = not (rec.get("last_synced_backup") == a.usb_when
                       and rec.get("baseline_usb_id") == self._usb_id())
        if not created and not a.fingerprint:
            fields["local_fingerprint"] = rec.get("local_fingerprint") or ""
        fields.update(title=a.title)
        if created:
            message = msg("baseline_created", title=a.title)
            self._event("baseline", message, game=a.title, result="success")
            return GameResult(a.key, a.title, GameState.SYNCED, Outcome.BASELINE, message), fields
        return GameResult(a.key, a.title, GameState.SYNCED, Outcome.NOTHING, a.message), fields

    def _failure(self, a, state, message, extra_fields=None, outcome=Outcome.FAILED):
        fields = {"title": a.title, "state": state.value, "state_message": message,
                  "error": text(message)}
        fields.update(extra_fields or {})
        return GameResult(a.key, a.title, state, outcome, message), fields

    def _upload(self, drive, a: GameAnalysis):
        """PC → USB. The baseline moves only after the engine validated the new
        version; until then the attempt is recorded as pending (rule 5)."""
        title = a.title
        if self._is_running(title, fresh=True):
            m = msg("game_running_now", title=title)
            return self._failure(a, GameState.RUNNING, m, outcome=Outcome.SKIPPED)
        if a.fingerprint is None:
            m = msg("validation_failed", title=title, detail="the PC saves could not be read")
            return self._failure(a, GameState.ERROR, m)
        cfg = self._cfg()
        pending = {"kind": "backup", "started": self.clock(), "prev_when": a.usb_when,
                   "known": [[b.name, b.when] for b in a.backups]}
        self.registry.write({"title": title, "pending_upload": True, "pending_op": pending})
        started = time.monotonic()
        result = self.engine.usb_backup(title, drive.backups_dir, cfg["full_limit"],
                                        cfg["differential_limit"])
        duration = time.monotonic() - started
        if not result.ok:
            m = msg("backup_nothing", title=title) if result.nothing else \
                msg("backup_failed", title=title, detail=result.problem)
            self._event("backup", m, game=title, source="PC", destination="USB",
                        result="failed", duration=duration, error=result.problem)
            if result.backup_name:
                pending["created"] = result.backup_name
            return self._failure(a, GameState.ERROR, m, {"pending_op": pending})
        fields = self._baseline_fields(result.when, a.fingerprint, a.local.paths)
        fields["title"] = title
        m = msg("backup_done" if result.changed else "backup_same", title=title)
        self._event("backup", m, game=title, source="PC", destination="USB",
                    result="success", duration=duration)
        outcome = Outcome.BACKED_UP if result.changed else Outcome.BASELINE
        return GameResult(a.key, title, GameState.SYNCED, outcome, m), fields

    def _download(self, drive, a: GameAnalysis, safety_dir, safety_ok):
        """USB → PC: safety snapshot, restore, validate, baseline (plan §12.5)."""
        title = a.title
        if self._is_running(title, fresh=True):
            m = msg("game_running_now", title=title)
            return self._failure(a, GameState.RUNNING, m, outcome=Outcome.SKIPPED)
        if safety_ok is False or safety_dir is None:
            m = msg("safety_backup_failed", title=title)
            self._event("safety", m, game=title, result="failed")
            return self._failure(a, GameState.ERROR, m)
        return self._restore_validated(drive, a.key, title, a.effective, safety_dir, safety_ok)

    def _restore_validated(self, drive, key, title, version, safety_dir, safety_ok,
                           adopt_when: str | None = None):
        """Restore `version` from the USB with rollback on failure (rule 6).
        Returns (GameResult, registry fields)."""
        started = time.monotonic()
        a = GameAnalysis(key=key, title=title, state=GameState.USB_NEWER)
        scan = self.engine.scan(drive.backups_dir, [title])
        before = (scan or {}).get(title)
        if scan is None or (safety_ok is True and before is None):
            # without the PC's file list, files the USB version lacks could not be
            # removed and the "restored" PC would silently be a mix of both
            return self._failure(a, GameState.ERROR, msg(
                "restore_failed", title=title,
                detail="the PC saves could not be scanned before the restore"))
        # What the restore will create, known BEFORE it runs: a restore killed
        # halfway (deadline, USB pulled) returns no JSON to learn it from.
        preview = self.engine.usb_preview(title, drive.backups_dir, backup=version.name)
        if preview is None:
            return self._failure(a, GameState.ERROR, msg(
                "restore_failed", title=title, detail="the restore preview failed"))
        will_create = sorted(p for p, i in preview["files"].items()
                             if (i or {}).get("change") == "New")
        if safety_ok is None:
            # "no saves to protect" — yet files exist where the version would go
            # (Ludusavi does not attribute them to this game here): overwriting
            # them could not be undone
            unprotected = sorted(p for p, i in preview["files"].items()
                                 if (i or {}).get("change") == "Different")
            if unprotected:
                return self._failure(a, GameState.ERROR, msg(
                    "restore_failed", title=title,
                    detail="files already exist at %s but are not detected as this game's "
                           "saves, so they cannot be protected" % unprotected[0]))
        self.registry.write({"title": title, "last_safety": safety_dir,
                             "pending_op": {"kind": "restore", "started": self.clock(),
                                            "snapshot": safety_dir, "safety_ok": safety_ok,
                                            "target_when": version.when,
                                            "created": will_create}})
        result = self.engine.usb_restore(title, drive.backups_dir, backup=version.name)
        result.created = sorted(set(result.created) | set(will_create))
        problem = result.problem
        local = None
        if result.ok and before is not None and safety_ok is True:
            # Ludusavi never deletes on restore (MEASURED). Save files the USB
            # version does not have would leave a mix of both versions, so they go —
            # the safety snapshot taken above holds them.
            if not self.remove_extra_files(before.paths, result.files):
                problem = "save files that are not part of the USB version could not be removed"
        if result.ok and not problem:
            check = self.engine.scan(drive.backups_dir, [title])
            local = (check or {}).get(title)
            if check is None or local is None or not local.present:
                problem = "the restored saves could not be scanned"
            elif local.failed:
                problem = "the restored saves could not be read"
            else:
                # A restore preview of the same version compares it with the PC
                # through Ludusavi's redirects (a backup preview would compare
                # paths recorded on another PC with this PC's paths — MEASURED
                # "Different" after a correct cross-user restore).
                after = self.engine.usb_preview(title, drive.backups_dir, backup=version.name)
                changes = [(i or {}).get("change") for i in ((after or {}).get("files") or {}).values()]
                changes += [(i or {}).get("change") for i in ((after or {}).get("registry") or {}).values()]
                if after is None or any(c != "Same" for c in changes):
                    problem = "the PC saves do not match the USB version after the restore"
        duration = time.monotonic() - started
        if problem:
            return self._rollback(a, safety_dir, safety_ok, result.created, problem, duration)
        fp = self._compute_fp(local.paths, self._extra(local))
        if fp is None:
            return self._rollback(a, safety_dir, safety_ok, result.created,
                                  "the restored saves could not be fingerprinted", duration)
        fields = self._baseline_fields(adopt_when or version.when, fp, local.paths)
        fields.update(title=title, last_safety=safety_dir)
        m = msg("restore_done", title=title)
        self._event("restore", m, game=title, source="USB", destination="PC",
                    result="success", duration=duration)
        return GameResult(key, title, GameState.SYNCED, Outcome.RESTORED, m), fields

    def _versions(self, drive, title):
        listing = self.engine.backups(drive.backups_dir, [title]) or {}
        return listing.get(title) or []

    def _rollback(self, a, safety_dir, safety_ok, created, problem, duration):
        """Bring the PC back to its state before the failed restore."""
        title = a.title
        recovered = False
        # The rollback is local: a cancelled runner (USB pulled) must not stop it.
        self.engine.reset()
        if safety_ok is True:
            back = self.engine.restore_from(title, safety_dir)
            recovered = back.ok
            if recovered:
                self._remove_created(created, keep=back.files)
        elif safety_ok is None:
            # there were no PC saves before: the previous state is "none", so the
            # files the partial restore created are the only thing to undo
            recovered = self._remove_created(created)
        if recovered:
            m = msg("restore_rolled_back", title=title, detail=problem)
            self._event("restore", m, game=title, source="USB", destination="PC",
                        result="rolled_back", duration=duration, error=problem)
            return self._failure(a, GameState.ERROR, m, {"pending_op": None},
                                 outcome=Outcome.ROLLED_BACK)
        m = msg("restore_rollback_failed", title=title, detail=problem, snapshot=safety_dir)
        self._event("restore", m, game=title, source="USB", destination="PC",
                    result="failed", duration=duration, error=problem)
        return self._failure(a, GameState.ERROR, m, {"pending_op": {
            "kind": "restore", "started": self.clock(), "snapshot": safety_dir,
            "created": list(created or []), "failed": True}})

    @staticmethod
    def remove_extra_files(local_paths, version_files) -> bool:
        """Delete the Ludusavi-identified save files that `version_files` lacks."""
        wanted = {os.path.normcase(os.path.abspath(p)) for p in version_files or {}}
        extra = [p for p in local_paths or []
                 if os.path.normcase(os.path.abspath(p)) not in wanted]
        return SyncService._remove_created(extra)

    @staticmethod
    def _remove_created(created, keep=None) -> bool:
        keep = {os.path.normcase(os.path.abspath(p)) for p in (keep or {})}
        ok = True
        for path in created or []:
            if os.path.normcase(os.path.abspath(path)) in keep:
                continue
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                ok = False
        return ok

    # --- explicit user decisions (conflict, first sync, manage) --------------------

    def _explicit(self, drive, title, action):
        """Common frame: registered USB, lock, not running, no active trial."""
        problem = self._check_drive(drive)
        key = title_key(title)
        if problem is not None:
            return GameResult(key, title, GameState.ERROR, Outcome.SKIPPED, problem)
        rec = self.registry.get(key) or {}
        if rec.get("trial"):
            return GameResult(key, title, GameState.parse(rec.get("state")), Outcome.SKIPPED,
                              msg("trial_active", title=title))
        try:
            with self.engine.lock():
                self.engine.reset()
                self._begin_processes()
                try:
                    if self._is_running(title, fresh=True):
                        m = msg("not_resolvable_running", title=title)
                        return GameResult(key, title, GameState.RUNNING, Outcome.SKIPPED, m)
                    result, fields = action(key, rec)
                finally:
                    self._end_processes()
        except SyncLocked as exc:
            return GameResult(key, title, GameState.parse(rec.get("state")), Outcome.SKIPPED, exc.msg)
        fields.setdefault("title", title)
        self.registry.write(fields)
        return result

    def use_usb(self, drive, title: str, backup_name: str | None = None) -> GameResult:
        """Keep the USB version (conflict "Use USB", first sync). A named older
        version is restored and then uploaded as the newest one, so the next
        cycle does not immediately bring the latest back."""
        def action(key, rec):
            a = GameAnalysis(key=key, title=title, state=GameState.USB_NEWER)
            versions = self._versions(drive, title)
            version = next((b for b in versions if b.name == backup_name), None) \
                if backup_name else latest_effective(versions)
            if version is None:
                return self._failure(a, GameState.ERROR, msg(
                    "restore_failed", title=title, detail="the USB has no usable version"))
            preview = self.engine.usb_preview(title, drive.backups_dir, backup=version.name)
            if preview is None:
                return self._failure(a, GameState.ERROR, msg(
                    "restore_failed", title=title, detail="the restore preview failed"))
            for path in preview["files"]:
                bad = target_path_problem(path, self.home)
                if bad:
                    m = msg("restore_path_invalid", title=title, path=path)
                    return self._failure(a, GameState.MISSING_LOCAL_PATH, m, outcome=Outcome.SKIPPED)
            snapshot = self.safety.new_snapshot("use-usb")
            safety_ok = self.engine.safety_backup(title, snapshot)
            if safety_ok is False:
                return self._failure(a, GameState.ERROR, msg("safety_backup_failed", title=title))
            result, fields = self._restore_validated(drive, key, title, version, snapshot, safety_ok)
            if result.outcome != Outcome.RESTORED:
                return result, fields
            newest = latest_effective(versions)
            if backup_name and newest is not None and newest.name != version.name:
                return self._upload_current(drive, key, title, msg(
                    "version_restored", title=title, when=version.when), Outcome.RESTORED)
            result.message = msg("resolved_use_usb", title=title)
            self._event("resolve", result.message, game=title, source="USB",
                        destination="PC", result="success")
            return result, fields
        return self._explicit(drive, title, action)

    def use_pc(self, drive, title: str) -> GameResult:
        """Keep the PC version: it becomes the newest USB version."""
        def action(key, rec):
            if (rec.get("pending_op") or {}).get("kind") == "restore":
                # the PC may hold a half-restored mix; uploading it would spread it
                a = GameAnalysis(key=key, title=title, state=GameState.ERROR)
                return self._failure(a, GameState.ERROR, msg(
                    "incomplete_operation", title=title, kind="restore"), outcome=Outcome.SKIPPED)
            result, fields = self._upload_current(drive, key, title,
                                                  msg("resolved_use_pc", title=title),
                                                  Outcome.BACKED_UP)
            if result.outcome == Outcome.BACKED_UP:
                self._event("resolve", result.message, game=title, source="PC",
                            destination="USB", result="success")
            return result, fields
        return self._explicit(drive, title, action)

    def restore_version(self, drive, title: str, backup_name: str) -> GameResult:
        return self.use_usb(drive, title, backup_name=backup_name)

    def _upload_current(self, drive, key, title, success_message, outcome):
        a = GameAnalysis(key=key, title=title, state=GameState.LOCAL_NEWER)
        scan = self.engine.scan(drive.backups_dir, [title])
        local = (scan or {}).get(title)
        if local is None or not local.present:
            return self._failure(a, GameState.ERROR, msg("backup_nothing", title=title))
        if local.failed:
            return self._failure(a, GameState.ERROR, msg(
                "backup_failed", title=title, detail="some PC saves could not be read"))
        a.local = local
        a.fingerprint = self._compute_fp(local.paths, self._extra(local))
        versions = self._versions(drive, title)
        a.backups = versions
        a.effective = latest_effective(versions)
        result, fields = self._upload(drive, a)
        if result.outcome in (Outcome.BACKED_UP, Outcome.BASELINE):
            result.outcome = outcome
            result.message = success_message
        return result, fields

    def recover_safety(self, title: str, snapshot: str | None = None) -> GameResult:
        """Put back the PC saves from a local safety snapshot (no USB needed)."""
        key = title_key(title)
        rec = self.registry.get(key) or {}
        pending = rec.get("pending_op") or {}
        snapshot = snapshot or pending.get("snapshot") or rec.get("last_safety")
        a = GameAnalysis(key=key, title=title, state=GameState.ERROR)
        if not snapshot or not os.path.isdir(snapshot):
            m = msg("restore_failed", title=title, detail="no safety snapshot is available")
            return GameResult(key, title, GameState.parse(rec.get("state")), Outcome.FAILED, m)
        try:
            with self.engine.lock():
                self._begin_processes()
                try:
                    if self._is_running(title, fresh=True):
                        return GameResult(key, title, GameState.RUNNING, Outcome.SKIPPED,
                                          msg("not_resolvable_running", title=title))
                    if pending.get("kind") == "restore" and pending.get("safety_ok", True) is None:
                        # the PC had no saves before: undoing the partial restore
                        # means removing what it created
                        from .engine import OpResult
                        back = OpResult(self._remove_created(pending.get("created")),
                                        problem="could not remove the partially restored files")
                    else:
                        back = self.engine.restore_from(title, snapshot)
                finally:
                    self._end_processes()
        except SyncLocked as exc:
            return GameResult(key, title, GameState.parse(rec.get("state")), Outcome.SKIPPED, exc.msg)
        if not back.ok:
            m = msg("restore_failed", title=title, detail=back.problem)
            self._event("recover", m, game=title, source="safety", destination="PC",
                        result="failed", error=back.problem)
            return GameResult(key, title, GameState.ERROR, Outcome.FAILED, m)
        self._remove_created(pending.get("created"), keep=back.files)
        m = msg("safety_recovered", title=title)
        self.registry.write({"title": title, "pending_op": None, "error": "",
                              "state": GameState.UNKNOWN.value, "state_message": m})
        self._event("recover", m, game=title, source="safety", destination="PC", result="success")
        return GameResult(key, title, GameState.UNKNOWN, Outcome.RESTORED, m)

    def protected_snapshots(self) -> set:
        return protected_snapshots(self.registry)
