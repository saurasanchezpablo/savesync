# Derived from decky-nonsteam-sync (py_modules/sdsync/decisions.py),
# Copyright (c) 2026, JoseArkadio — BSD-3-Clause, see LICENSE-UPSTREAM.
"""The decision table (plan §10.2). The order of the checks is binding."""
from __future__ import annotations

from .state import GameState


def decide(local_changed, usb_ahead, local_ahead: bool,
           running: bool = False, first_sync: bool = False,
           local_path_known: bool = True) -> GameState:
    """Classify one game.

    `local_changed` is tri-state: True / False / None ("don't know" — the
    fingerprint or the Ludusavi preview failed). Not knowing is never turned
    into "no changes": a restore from the USB would then overwrite an unknown
    live save. `usb_ahead` accepts None for the same reason.

    `usb_ahead`: the newest USB backup is not the one this PC last synchronized.
    `local_ahead`: this PC holds a state the USB never confirmed receiving
    (an upload that was interrupted). Either one alone is a direction; both
    together are a divergence, i.e. a conflict.

    Precedence: a running game is never touched, whatever else is true; a game
    without a known local save path cannot be restored or compared; a PC with
    no baseline cannot infer which side should win.
    """
    if running:
        return GameState.RUNNING
    if not local_path_known:
        return GameState.MISSING_LOCAL_PATH
    if first_sync:
        return GameState.FIRST_SYNC
    if local_changed is None or usb_ahead is None:
        return GameState.UNKNOWN
    if usb_ahead and local_ahead:
        return GameState.CONFLICT
    if usb_ahead and local_changed:
        return GameState.CONFLICT
    if usb_ahead:
        return GameState.USB_NEWER
    if local_changed:
        return GameState.LOCAL_NEWER
    return GameState.SYNCED
