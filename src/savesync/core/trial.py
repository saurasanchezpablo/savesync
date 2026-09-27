"""Trial mode — "Explore both" (plan §15).

    save current PC state → test USB version → (play) → test PC version → choose

Both versions are kept as local safety snapshots, so switching back and forth
needs no USB and never loses either side. Before every switch the current state
is snapshotted too, in case the user made progress while testing. Only the final
choice (Keep USB / Keep PC) moves the baseline; Cancel returns to the original
PC state and leaves the conflict as it was.
"""
from __future__ import annotations

import os

from .engine import latest_effective
from .messages import msg, text
from .registry import title_key
from .state import GameResult, GameState, Outcome

SIDES = ("usb", "pc")


class TrialManager:
    def __init__(self, service):
        self.service = service

    @property
    def engine(self):
        return self.service.engine

    @property
    def registry(self):
        return self.service.registry

    def active(self, title: str):
        rec = self.registry.get(title_key(title)) or {}
        return rec.get("trial")

    def _result(self, title, outcome, message, state=None):
        rec = self.registry.get(title_key(title)) or {}
        return GameResult(title_key(title), title,
                          state or GameState.parse(rec.get("state")), outcome, message)

    def _guard(self, title):
        """None when files of `title` may be touched now (rule 4)."""
        self.service._begin_processes()
        try:
            if self.service._is_running(title):
                return msg("not_resolvable_running", title=title)
        finally:
            self.service._end_processes()
        return None

    def _scan_paths(self, drive_or_path, title):
        scan = self.engine.scan(drive_or_path, [title])
        local = (scan or {}).get(title)
        return local

    # --- start ----------------------------------------------------------------

    def start(self, drive, title: str) -> GameResult:
        problem = self.service._check_drive(drive)
        if problem is not None:
            return self._result(title, Outcome.SKIPPED, problem)
        if self.active(title):
            return self._result(title, Outcome.SKIPPED, msg("trial_active", title=title))
        blocked = self._guard(title)
        if blocked:
            return self._result(title, Outcome.SKIPPED, blocked)
        with self.engine.lock():
            versions = (self.engine.backups(drive.backups_dir, [title]) or {}).get(title) or []
            version = latest_effective(versions)
            if version is None:
                return self._fail(title, "the USB has no usable version")
            before = self._scan_paths(drive.backups_dir, title)
            files_pc = before.paths if before is not None and before.present else []
            original = self.service.safety.new_snapshot("trial-original")
            saved = self.engine.safety_backup(title, original)
            if saved is False:
                return self._fail(title, text(msg("safety_backup_failed", title=title)))
            restored = self.engine.usb_restore(title, drive.backups_dir, backup=version.name)
            if not restored.ok:
                self._go_back(title, original, saved, restored.created)
                return self._fail(title, restored.problem)
            # the USB version exactly: PC-only files would make a mix of both
            files_usb = sorted(restored.files)
            if not self.service.remove_extra_files(files_pc, files_usb):
                self._go_back(title, original, saved, restored.created)
                return self._fail(title, "could not remove files of the PC version")
            after = self._scan_paths(drive.backups_dir, title)
            usb_copy = self.service.safety.new_snapshot("trial-usb")
            if self.engine.safety_backup(title, usb_copy) is not True:
                self._go_back(title, original, saved, restored.created)
                return self._fail(title, "the USB version could not be kept locally")
            fp_usb = self.service._compute_fp(files_usb, self.service._extra(after))
            trial = {"started": self.service.clock(), "side": "usb",
                     "usb_backup": version.name, "usb_when": version.when,
                     "original": original, "original_saved": saved is True,
                     "usb_copy": usb_copy, "files_pc": files_pc, "files_usb": files_usb,
                     "fp_usb": fp_usb or ""}
            self.registry.write({"title": title, "trial": trial})
        m = msg("trial_started", title=title)
        self.service._event("trial", m, game=title, source="USB", destination="PC",
                            result="started")
        return self._result(title, Outcome.RESTORED, m)

    def _fail(self, title, detail):
        m = msg("trial_failed", title=title, detail=detail)
        self.service._event("trial", m, game=title, result="failed", error=str(detail))
        return self._result(title, Outcome.FAILED, m)

    def _go_back(self, title, original, saved, created):
        if saved is True:
            if self.engine.restore_from(title, original).ok:
                self.service._remove_created(created)
        else:
            self.service._remove_created(created)

    # --- switching (local only) -------------------------------------------------

    def switch(self, title: str, side: str) -> GameResult:
        if side not in SIDES:
            raise ValueError(side)
        trial = self.active(title)
        if not trial:
            return self._result(title, Outcome.SKIPPED, msg("trial_failed", title=title,
                                                            detail="no trial is active"))
        if trial["side"] == side:
            return self._result(title, Outcome.NOTHING,
                                msg("trial_testing", title=title, side=side.upper()))
        blocked = self._guard(title)
        if blocked:
            return self._result(title, Outcome.SKIPPED, blocked)
        with self.engine.lock():
            problem = self._apply(title, trial, side)
        if problem:
            return self._fail(title, problem)
        trial["side"] = side
        self.registry.write({"title": title, "trial": trial})
        m = msg("trial_testing", title=title, side=side.upper())
        self.service._event("trial", m, game=title, source=side.upper(), destination="PC",
                            result="switched")
        return self._result(title, Outcome.RESTORED, m)

    def _apply(self, title, trial, side) -> str:
        """Put version `side` on the PC. Returns a problem or ""."""
        # the state being left may contain progress made while testing
        checkpoint = self.service.safety.new_snapshot("trial-switch")
        if self.engine.safety_backup(title, checkpoint) is False:
            return text(msg("safety_backup_failed", title=title))
        if side == "usb":
            restored = self.engine.restore_from(title, trial["usb_copy"])
            if not restored.ok:
                return restored.problem
            target_files = trial["files_usb"]
        elif trial.get("original_saved"):
            restored = self.engine.restore_from(title, trial["original"])
            if not restored.ok:
                return restored.problem
            target_files = trial["files_pc"]
        else:
            target_files = []  # the PC had no saves before the trial
        # files that belong to the other version only would make a mix of both
        keep = {os.path.normcase(p) for p in target_files}
        extra = [p for p in set(trial["files_pc"]) | set(trial["files_usb"])
                 if os.path.normcase(p) not in keep]
        if not self.service._remove_created(extra):
            return "could not remove files of the other version"
        return ""

    # --- finishing ------------------------------------------------------------------

    def keep(self, drive, title: str, side: str) -> GameResult:
        if side not in SIDES:
            raise ValueError(side)
        trial = self.active(title)
        if not trial:
            return self._result(title, Outcome.SKIPPED, msg("trial_failed", title=title,
                                                            detail="no trial is active"))
        if side == "pc":
            problem = self.service._check_drive(drive)
            if problem is not None:
                return self._result(title, Outcome.SKIPPED, problem)
        blocked = self._guard(title)
        if blocked:
            return self._result(title, Outcome.SKIPPED, blocked)
        if trial["side"] != side:
            with self.engine.lock():
                problem = self._apply(title, trial, side)
            if problem:
                return self._fail(title, problem)
            trial["side"] = side
            self.registry.write({"title": title, "trial": trial})
        if side == "usb":
            paths = trial["files_usb"]
            fields = self.service._baseline_fields(trial["usb_when"], trial["fp_usb"], paths)
            fields.update(title=title, trial=None)
            self.registry.write(fields)
            m = msg("trial_kept", title=title, side="USB")
            self.service._event("trial", m, game=title, source="USB", destination="PC",
                                result="success")
            return self._result(title, Outcome.RESTORED, m, GameState.SYNCED)
        # Keep PC: the PC version must become the newest USB version. The trial
        # stays recorded until that upload validated.
        self.registry.write({"title": title, "trial": None})
        result = self.service.use_pc(drive, title)
        if result.outcome != Outcome.BACKED_UP:
            self.registry.write({"title": title, "trial": trial})
            return result
        m = msg("trial_kept", title=title, side="PC")
        self.service._event("trial", m, game=title, source="PC", destination="USB",
                            result="success")
        return GameResult(result.key, title, GameState.SYNCED, Outcome.BACKED_UP, m)

    def cancel(self, title: str) -> GameResult:
        """Return to the original PC state; the baseline and conflict stay as they were."""
        trial = self.active(title)
        if not trial:
            return self._result(title, Outcome.NOTHING, msg("trial_cancelled", title=title))
        blocked = self._guard(title)
        if blocked:
            return self._result(title, Outcome.SKIPPED, blocked)
        if trial["side"] != "pc":
            with self.engine.lock():
                problem = self._apply(title, trial, "pc")
            if problem:
                return self._fail(title, problem)
        self.registry.write({"title": title, "trial": None})
        m = msg("trial_cancelled", title=title)
        self.service._event("trial", m, game=title, result="cancelled")
        return self._result(title, Outcome.ROLLED_BACK, m)
