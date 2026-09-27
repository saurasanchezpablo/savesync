"""Trial mode — "Explore both" (plan §15).

    save current PC state → test USB version → (play) → test PC version → choose

Both versions are kept as local safety snapshots, so switching back and forth
needs no USB and never loses either side. Switching away from a side first
checkpoints it (progress made while testing) and switching back restores that
checkpoint. Only the final choice (Keep USB / Keep PC) moves the baseline;
Cancel returns to the pristine original PC state and leaves the conflict as it
was.

The trial is recorded in the registry BEFORE the USB version touches the PC, so
a crash at any point leaves a trial the user can cancel — never an unrecorded
overwrite that the next cycle would mistake for a synchronized state.
"""
from __future__ import annotations

import os

from .engine import latest_effective
from .messages import msg, text
from .registry import title_key
from .state import GameResult, GameState, Outcome

SIDES = ("usb", "pc")
STARTING = "starting"
MIXED = "mixed"  # a switch failed halfway: the PC holds parts of both versions


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

    def _save(self, title, trial) -> None:
        self.registry.write({"title": title, "trial": trial})

    def _result(self, title, outcome, message, state=None):
        rec = self.registry.get(title_key(title)) or {}
        return GameResult(title_key(title), title,
                          state or GameState.parse(rec.get("state")), outcome, message)

    def _guard(self, title):
        """None when files of `title` may be touched now (rule 4)."""
        if self.service._is_running(title, fresh=True):
            return msg("not_resolvable_running", title=title)
        return None

    def _fail(self, title, detail):
        m = msg("trial_failed", title=title, detail=detail)
        self.service._event("trial", m, game=title, result="failed", error=str(detail))
        return self._result(title, Outcome.FAILED, m)

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
            return self._start(drive, title)

    def _start(self, drive, title):
        usb = drive.backups_dir
        versions = (self.engine.backups(usb, [title]) or {}).get(title) or []
        version = latest_effective(versions)
        if version is None:
            return self._fail(title, "the USB has no usable version")
        scan = self.engine.scan(usb, [title])
        if scan is None:
            return self._fail(title, "the PC saves could not be scanned")
        before = scan.get(title)
        files_pc = before.paths if before is not None and before.present else []
        preview = self.engine.usb_preview(title, usb, backup=version.name)
        if preview is None:
            return self._fail(title, "the restore preview failed")
        files_usb = sorted(preview["files"])
        will_create = sorted(p for p, i in preview["files"].items()
                             if (i or {}).get("change") == "New")
        original = self.service.safety.new_snapshot("trial-original")
        saved = self.engine.safety_backup(title, original)
        if saved is False:
            return self._fail(title, text(msg("safety_backup_failed", title=title)))
        trial = {"started": self.service.clock(), "side": STARTING,
                 "usb_backup": version.name, "usb_when": version.when,
                 "original": original, "original_saved": saved is True, "usb_copy": "",
                 "files_pc": files_pc, "files_usb": files_usb, "fp_usb": "",
                 "latest": {}, "latest_files": {}}
        self._save(title, trial)  # recorded before anything on the PC changes
        try:
            restored = self.engine.usb_restore(title, usb, backup=version.name)
            if not restored.ok:
                raise RuntimeError(restored.problem)
            files_usb = sorted(restored.files)
            if not self.service.remove_extra_files(files_pc, files_usb):
                raise RuntimeError("could not remove files of the PC version")
            usb_copy = self.service.safety.new_snapshot("trial-usb")
            if self.engine.safety_backup(title, usb_copy) is not True:
                raise RuntimeError("the USB version could not be kept locally")
            after = (self.engine.scan(usb_copy, [title]) or {}).get(title)
            fp_usb = self.service._compute_fp(files_usb, self.service._extra(after))
        except Exception as exc:
            self._go_back(title, original, saved, will_create)
            self._save(title, None)
            return self._fail(title, str(exc))
        trial.update(side="usb", usb_copy=usb_copy, files_usb=files_usb, fp_usb=fp_usb or "")
        self._save(title, trial)
        m = msg("trial_started", title=title)
        self.service._event("trial", m, game=title, source="USB", destination="PC",
                            result="started")
        return self._result(title, Outcome.RESTORED, m)

    def _go_back(self, title, original, saved, created):
        self.engine.reset()
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
        if side == "usb" and not trial.get("usb_copy"):
            return self._fail(title, "the trial did not finish starting; cancel it")
        blocked = self._guard(title)
        if blocked:
            return self._result(title, Outcome.SKIPPED, blocked)
        with self.engine.lock():
            problem = self._apply(title, trial, side)
        self._save(title, trial)  # checkpoints are recorded even if the switch failed
        if problem:
            return self._fail(title, problem)
        m = msg("trial_testing", title=title, side=side.upper())
        self.service._event("trial", m, game=title, source=side.upper(), destination="PC",
                            result="switched")
        return self._result(title, Outcome.RESTORED, m)

    def _checkpoint(self, title, trial):
        """Snapshot the side being left (it may hold progress made while testing)."""
        leaving = trial["side"]
        if leaving not in SIDES:
            return ""
        checkpoint = self.service.safety.new_snapshot("trial-%s-progress" % leaving)
        saved = self.engine.safety_backup(title, checkpoint)
        if saved is False:
            return text(msg("safety_backup_failed", title=title))
        if saved is True:
            local = (self.engine.scan(checkpoint, [title]) or {}).get(title)
            trial.setdefault("latest", {})[leaving] = checkpoint
            trial.setdefault("latest_files", {})[leaving] = local.paths if local else []
        return ""

    def _apply(self, title, trial, side, pristine: bool = False) -> str:
        """Put version `side` on the PC (updates `trial` in place). Returns a
        problem or ""."""
        problem = self._checkpoint(title, trial)
        if problem:
            return problem
        latest = {} if pristine else (trial.get("latest") or {})
        latest_files = {} if pristine else (trial.get("latest_files") or {})
        if latest.get(side):
            source, target_files = latest[side], latest_files.get(side) or []
        elif side == "usb":
            source, target_files = trial["usb_copy"], trial["files_usb"]
        elif trial.get("original_saved"):
            source, target_files = trial["original"], trial["files_pc"]
        else:
            source, target_files = None, []  # the PC had no saves before the trial
        if source:
            restored = self.engine.restore_from(title, source)
            if not restored.ok:
                # the PC may now hold part of each version: neither side may be
                # kept (or checkpointed) until one is applied completely
                trial["side"] = MIXED
                return restored.problem
        # files that belong only to the other side would make a mix of both
        known = set(trial["files_pc"]) | set(trial["files_usb"])
        for files in (trial.get("latest_files") or {}).values():
            known |= set(files)
        keep = {os.path.normcase(p) for p in target_files}
        extra = [p for p in known if os.path.normcase(p) not in keep]
        if not self.service._remove_created(extra):
            trial["side"] = MIXED
            return "could not remove files of the other version"
        trial["side"] = side
        return ""

    # --- finishing ------------------------------------------------------------------

    def keep(self, drive, title: str, side: str) -> GameResult:
        if side not in SIDES:
            raise ValueError(side)
        trial = self.active(title)
        if not trial:
            return self._result(title, Outcome.SKIPPED, msg("trial_failed", title=title,
                                                            detail="no trial is active"))
        if not trial.get("usb_copy"):
            return self._fail(title, "the trial did not finish starting; cancel it")
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
            self._save(title, trial)
            if problem:
                return self._fail(title, problem)
        if side == "usb":
            # baseline = the pristine USB version; progress made while testing
            # it is an ordinary local change that the next cycle uploads
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
        self._save(title, None)
        result = self.service.use_pc(drive, title)
        if result.outcome != Outcome.BACKED_UP:
            self._save(title, trial)
            return result
        m = msg("trial_kept", title=title, side="PC")
        self.service._event("trial", m, game=title, source="PC", destination="USB",
                            result="success")
        return GameResult(result.key, title, GameState.SYNCED, Outcome.BACKED_UP, m)

    def cancel(self, title: str) -> GameResult:
        """Return to the ORIGINAL PC state; the baseline and conflict stay as they
        were. Progress made while testing stays in its (expiring) checkpoint."""
        trial = self.active(title)
        if not trial:
            return self._result(title, Outcome.NOTHING, msg("trial_cancelled", title=title))
        blocked = self._guard(title)
        if blocked:
            return self._result(title, Outcome.SKIPPED, blocked)
        with self.engine.lock():
            problem = self._apply(title, trial, "pc", pristine=True)
        if problem:
            self._save(title, trial)
            return self._fail(title, problem)
        self._save(title, None)
        m = msg("trial_cancelled", title=title)
        self.service._event("trial", m, game=title, result="cancelled")
        return self._result(title, Outcome.ROLLED_BACK, m)
