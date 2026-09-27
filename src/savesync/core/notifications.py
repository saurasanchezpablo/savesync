"""What to tell the user after a cycle (plan §21): aggregated, never one
notification per game."""
from __future__ import annotations

from .messages import text
from .state import GameState, Outcome, SyncReport

MAX_NAMES = 3


def _names(games) -> str:
    titles = [g.title for g in games]
    shown = ", ".join(titles[:MAX_NAMES])
    extra = len(titles) - MAX_NAMES
    return shown + (" and %d more" % extra if extra > 0 else "")


def summarize(report: SyncReport) -> list:
    """[(preference key, title, message)] for one finished cycle."""
    out = []
    if report.errors:
        out.append(("error", "Save Sync — problem", "\n".join(text(e) for e in report.errors[:3])))
    conflicts = report.by_state(GameState.CONFLICT)
    if conflicts:
        out.append(("conflict", "⚠ Conflict detected",
                    "%d game%s changed on both the PC and the USB: %s" % (
                        len(conflicts), "" if len(conflicts) == 1 else "s", _names(conflicts))))
    running = report.by_state(GameState.RUNNING)
    if running:
        out.append(("game_running", "Game running",
                    "%s %s running, so %s saves were left untouched." % (
                        _names(running), "is" if len(running) == 1 else "are",
                        "its" if len(running) == 1 else "their")))
    failed = [g for g in report.games if g.outcome in (Outcome.FAILED, Outcome.ROLLED_BACK)]
    if failed:
        out.append(("error", "Synchronization error",
                    "\n".join(text(g.message) for g in failed[:3])
                    + ("\n…and %d more" % (len(failed) - 3) if len(failed) > 3 else "")))
    if report.games and not report.analyzed_only:
        synced = len(report.synchronized)
        attention = len(report.attention)
        lines = ["✓ %d game%s synchronized" % (synced, "" if synced == 1 else "s")]
        if attention:
            lines.append("⚠ %d require%s attention" % (attention, "s" if attention == 1 else ""))
        if not report.completed:
            lines.append("The synchronization did not finish; the rest stays pending.")
        out.append(("sync_completed", "Save Sync", "\n".join(lines)))
    return out
