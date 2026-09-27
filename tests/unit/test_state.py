from savesync.core.state import (ATTENTION_STATES, GameResult, GameState, Outcome,
                                 SyncReport)


def test_every_state_has_the_plan_value():
    assert {s.value for s in GameState} == {
        "synced", "local_newer", "usb_newer", "conflict", "missing_local_path",
        "running", "error", "unknown", "first_sync"}


def test_unreadable_state_parses_as_unknown_never_synced():
    assert GameState.parse("synced") is GameState.SYNCED
    assert GameState.parse("garbage") is GameState.UNKNOWN
    assert GameState.parse(None) is GameState.UNKNOWN


def test_unknown_and_error_need_attention():
    assert GameState.UNKNOWN in ATTENTION_STATES
    assert GameState.ERROR in ATTENTION_STATES
    assert GameState.SYNCED not in ATTENTION_STATES


def test_result_ok_only_for_validated_outcomes():
    assert GameResult("a", "A", GameState.SYNCED, Outcome.BACKED_UP).ok
    assert GameResult("a", "A", GameState.SYNCED, Outcome.RESTORED).ok
    assert not GameResult("a", "A", GameState.ERROR, Outcome.FAILED).ok
    assert not GameResult("a", "A", GameState.UNKNOWN, Outcome.NOTHING).ok
    assert not GameResult("a", "A", GameState.SYNCED, Outcome.ROLLED_BACK).ok


def test_report_success_requires_completion_and_no_attention():
    report = SyncReport(games=[GameResult("a", "A", GameState.SYNCED)], completed=True)
    assert report.success
    report.games.append(GameResult("b", "B", GameState.CONFLICT, Outcome.SKIPPED))
    assert not report.success
    assert [g.key for g in report.attention] == ["b"]
    incomplete = SyncReport(games=[GameResult("a", "A", GameState.SYNCED)], completed=False)
    assert not incomplete.success
