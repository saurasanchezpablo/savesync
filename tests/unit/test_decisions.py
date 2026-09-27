"""Decision table (plan §10.2). Ported from upstream tests/test_decisions.py with
cloud renamed to USB, then extended for FIRST_SYNC / MISSING_LOCAL_PATH / UNKNOWN."""
import itertools

import pytest

from savesync.core.decisions import decide
from savesync.core.state import GameState as S

BOOLS = (True, False)
TRI = (True, False, None)


def test_table_from_plan():
    assert decide(local_changed=False, usb_ahead=True, local_ahead=False) == S.USB_NEWER
    assert decide(local_changed=False, usb_ahead=False, local_ahead=False) == S.SYNCED
    assert decide(local_changed=True, usb_ahead=False, local_ahead=False) == S.LOCAL_NEWER
    assert decide(local_changed=True, usb_ahead=True, local_ahead=False) == S.CONFLICT


@pytest.mark.parametrize("local_ahead", BOOLS)
def test_local_changed_with_usb_not_ahead_is_local_newer(local_ahead):
    assert decide(True, False, local_ahead) == S.LOCAL_NEWER


@pytest.mark.parametrize("local_ahead", BOOLS)
def test_nothing_changed_is_synced_whatever_local_ahead(local_ahead):
    assert decide(False, False, local_ahead) == S.SYNCED


def test_divergence_in_both_directions_is_a_conflict():
    # the USB has a version from another PC, and this PC holds one the USB never
    # confirmed — a bare "USB differs" must not mean "USB is newer"
    assert decide(local_changed=False, usb_ahead=True, local_ahead=True) == S.CONFLICT
    assert decide(local_changed=True, usb_ahead=True, local_ahead=True) == S.CONFLICT


@pytest.mark.parametrize("local_changed,usb_ahead,local_ahead,first,known",
                         list(itertools.product(TRI, TRI, BOOLS, BOOLS, BOOLS)))
def test_running_game_is_never_touched(local_changed, usb_ahead, local_ahead, first, known):
    assert decide(local_changed, usb_ahead, local_ahead, running=True,
                  first_sync=first, local_path_known=known) == S.RUNNING


@pytest.mark.parametrize("local_changed,usb_ahead,local_ahead",
                         list(itertools.product(TRI, TRI, BOOLS)))
def test_first_sync_wins_over_every_comparison(local_changed, usb_ahead, local_ahead):
    assert decide(local_changed, usb_ahead, local_ahead, first_sync=True) == S.FIRST_SYNC


@pytest.mark.parametrize("local_changed,usb_ahead,local_ahead,first",
                         list(itertools.product(TRI, TRI, BOOLS, BOOLS)))
def test_missing_local_path_is_never_restored(local_changed, usb_ahead, local_ahead, first):
    assert decide(local_changed, usb_ahead, local_ahead, first_sync=first,
                  local_path_known=False) == S.MISSING_LOCAL_PATH


@pytest.mark.parametrize("usb_ahead,local_ahead", list(itertools.product(BOOLS, BOOLS)))
def test_unknown_local_state_is_unknown_never_synced(usb_ahead, local_ahead):
    # None = the fingerprint or the preview failed. Turning it into "no changes"
    # would let a restore overwrite a live save of unknown content.
    assert decide(None, usb_ahead, local_ahead) == S.UNKNOWN


@pytest.mark.parametrize("local_changed,local_ahead", list(itertools.product(BOOLS, BOOLS)))
def test_unknown_usb_state_is_unknown(local_changed, local_ahead):
    assert decide(local_changed, None, local_ahead) == S.UNKNOWN


def test_running_game_wins_over_unknown_local_state():
    assert decide(local_changed=None, usb_ahead=True, local_ahead=True,
                  running=True) == S.RUNNING


def test_only_usb_newer_restores():
    """Exhaustive: USB → PC happens in exactly one combination."""
    restoring = [combo for combo in itertools.product(TRI, TRI, BOOLS, BOOLS, BOOLS, BOOLS)
                 if decide(*combo[:3], running=combo[3], first_sync=combo[4],
                           local_path_known=combo[5]) == S.USB_NEWER]
    assert restoring == [(False, True, False, False, False, True)]


def test_unknown_is_never_success():
    for combo in itertools.product(TRI, TRI, BOOLS):
        if None in combo[:2]:
            assert decide(*combo) not in (S.SYNCED, S.USB_NEWER, S.LOCAL_NEWER)
