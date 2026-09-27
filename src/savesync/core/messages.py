# Structure derived from decky-nonsteam-sync (py_modules/sdsync/messages.py),
# Copyright (c) 2026, JoseArkadio — BSD-3-Clause, see LICENSE-UPSTREAM.
"""Human-facing messages: a stable code, parameters, and an English sentence.

The code is what tests and the UI key on; the sentence is what the log and the
UI show. Every sentence must be a complete sentence, not an abbreviation,
because it is the only thing a user reading the history sees.
"""
from __future__ import annotations

import string

CODES = {
    # --- USB ---
    "usb_connected": "USB {label} connected.",
    "usb_removed": "USB {label} removed.",
    "usb_removed_pending": ("USB removed with pending changes. The PC saves are newer;"
                            " they will be synchronized when you reconnect the USB."),
    "usb_unknown": ("Unknown USB {label}: it is not the registered Save Sync drive,"
                    " so nothing was read from or written to it."),
    "usb_identity_corrupt": ("The Save Sync marker on {label} is unreadable ({detail}),"
                             " so the drive is not trusted."),
    "usb_not_registered": "No Save Sync USB is registered yet. Choose one in Settings.",
    "usb_absent": "The registered USB is not connected.",
    "usb_registered": "{label} is now the registered Save Sync USB.",
    "usb_removed_during_sync": ("The USB was removed during synchronization. The operation"
                                " was stopped and nothing was marked as synchronized."),
    "usb_backups_unreadable": ("The backups on the USB could not be read ({detail}),"
                               " so no game was changed."),
    "usb_backup_corrupt": ("{title}: the backup index on the USB is unreadable ({path})."
                           " Nothing was changed; repair or remove it with Ludusavi."),
    # --- Ludusavi ---
    "ludusavi_missing": ("Ludusavi was not found ({detail}). Put ludusavi.exe in"
                         " SaveSync\\ludusavi on the USB or set its path in Settings."),
    "ludusavi_failed": "Ludusavi failed while {operation}: {detail}",
    "ludusavi_unknown_title": ("{title}: Ludusavi does not know this game, so its saves"
                               " cannot be synchronized."),
    "ludusavi_no_manifest": ("Ludusavi has no game database yet. Connect to the internet"
                             " once so it can download it."),
    "local_scan_failed": ("The local save scan failed ({detail}), so no game was"
                          " changed."),
    # --- per-game states ---
    "state_synced": "{title} is synchronized.",
    "state_local_newer": "{title}: the PC save is newer than the USB.",
    "state_usb_newer": "{title}: the USB save is newer than the PC.",
    "state_conflict": "{title}: both the PC and the USB changed since the last sync.",
    "state_missing_local_path": ("{title}: game found on USB, but no local save path was"
                                 " found."),
    "state_local_saves_missing": ("{title}: the local saves disappeared since the last"
                                  " sync. Nothing was changed."),
    "state_running": ("{title} is currently running, so its saves were left untouched."),
    "state_unknown": ("{title}: the state could not be determined, so nothing was"
                      " changed."),
    "state_first_sync": ("{title}: no previously known state exists on this PC. Choose"
                         " which version to keep."),
    "state_trial": "{title} is in trial mode.",
    "state_excluded": "{title} is excluded from synchronization.",
    # --- operations ---
    "backup_done": "{title}: PC → USB backup completed.",
    "backup_same": "{title}: the USB already had this save.",
    "backup_failed": "{title}: PC → USB backup failed: {detail}",
    "backup_nothing": "{title}: Ludusavi found no save to back up.",
    "restore_done": "{title}: USB → PC restore completed.",
    "restore_failed": "{title}: USB → PC restore failed: {detail}",
    "restore_rolled_back": ("{title}: the restore failed ({detail}); the previous PC"
                            " state was recovered from the safety snapshot."),
    "restore_rollback_failed": ("{title}: the restore failed ({detail}) and the automatic"
                                " recovery also failed. The safety snapshot is kept in"
                                " {snapshot}."),
    "restore_path_invalid": ("{title}: the USB backup would be restored to {path}, which"
                             " does not exist on this PC."),
    "safety_backup_failed": ("{title}: the safety snapshot of the PC saves failed, so"
                             " the restore was not attempted."),
    "baseline_created": "{title}: baseline recorded (PC and USB are identical).",
    "validation_failed": "{title}: the result could not be validated: {detail}",
    "incomplete_operation": ("{title}: a previous {kind} was interrupted and was not"
                             " counted as synchronized. Recover the previous PC state"
                             " or choose Use USB."),
    "game_running_now": "{title} started running; its saves were left untouched.",
    "not_resolvable_running": "{title} is running. Close the game first.",
    "resolved_use_usb": "{title}: kept the USB version.",
    "resolved_use_pc": "{title}: kept the PC version.",
    "version_restored": "{title}: restored the USB version from {when}.",
    "safety_recovered": "{title}: recovered the PC saves from the safety snapshot.",
    # --- trial mode ---
    "trial_started": "{title}: trial mode started — testing the USB version.",
    "trial_testing": "{title}: trial mode — now testing the {side} version.",
    "trial_kept": "{title}: trial finished — kept the {side} version.",
    "trial_cancelled": "{title}: trial cancelled — the original PC state was restored.",
    "trial_failed": "{title}: trial step failed: {detail}",
    "trial_active": "{title} is in trial mode. Finish or cancel the trial first.",
    # --- cycle ---
    "busy": "Save Sync is busy with another operation. Try again when it finishes.",
    "sync_locked": "A synchronization is already running (process {pid}, since {when}).",
    "sync_lock_takeover_failed": "Could not take the synchronization lock: {detail}",
    "sync_summary": "{synced} synchronized · {attention} require attention.",
    "sync_cancelled": "Synchronization stopped before it finished; nothing pending was marked as synchronized.",
    "sync_deadline": "Synchronization stopped at its time limit; the rest stays pending.",
    "shutdown_incomplete": ("The shutdown synchronization did not finish. Some games are"
                            " still pending."),
    "safety_cleaned": "Removed {count} safety snapshot(s) ({size}).",
    "nothing_to_do": "Nothing to synchronize.",
}


class _Defaulting(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def msg(code: str, **params) -> dict:
    """{code, params, message}. An unknown code or missing parameter never raises:
    a broken message must not become a second failure while reporting the first."""
    template = CODES.get(code)
    if template is None:
        text = code if not params else "%s %s" % (code, params)
    else:
        text = string.Formatter().vformat(template, (), _Defaulting(params))
    return {"code": code, "params": params, "message": text}


def text(message) -> str:
    if isinstance(message, dict):
        return str(message.get("message") or message.get("code") or "")
    return "" if message is None else str(message)
