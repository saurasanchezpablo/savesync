import time

from savesync.platform.windows.shutdown import (WM_ENDSESSION, WM_QUERYENDSESSION,
                                                ShutdownProtocol)


class FakeUser32:
    def __init__(self):
        self.calls = []

    def block(self, hwnd, reason):
        self.calls.append(("block", reason))
        return True

    def unblock(self, hwnd):
        self.calls.append(("unblock",))
        return True


def test_without_handler_messages_pass_through():
    protocol = ShutdownProtocol(FakeUser32())
    assert protocol.on_message(WM_QUERYENDSESSION, 0) == (False, 0)


def test_shutdown_with_pending_work_runs_sync_with_deadline():
    api = FakeUser32()
    ran = []
    protocol = ShutdownProtocol(api, deadline_seconds=lambda: 170)
    protocol.callback = lambda deadline: ran.append(deadline) or True
    protocol.precheck = lambda: True
    assert protocol.on_message(WM_QUERYENDSESSION, 0) == (True, 1), "never vetoes"
    assert api.calls == [("block", api.calls[0][1])]
    assert protocol.on_message(WM_ENDSESSION, 1) == (True, 0)
    assert ran == [170] and api.calls[-1] == ("unblock",)
    assert protocol.last_result is True


def test_no_pending_work_means_no_block_and_no_sync():
    api = FakeUser32()
    protocol = ShutdownProtocol(api)
    protocol.callback = lambda deadline: 1 / 0
    protocol.precheck = lambda: False
    protocol.on_message(WM_QUERYENDSESSION, 0)
    assert protocol.on_message(WM_ENDSESSION, 1) == (False, 0)
    assert api.calls == []


def test_cancelled_shutdown_releases_block_without_syncing():
    api = FakeUser32()
    protocol = ShutdownProtocol(api)
    protocol.callback = lambda deadline: 1 / 0
    protocol.precheck = lambda: True
    protocol.on_message(WM_QUERYENDSESSION, 0)
    protocol.on_message(WM_ENDSESSION, 0)  # another app vetoed; session goes on
    assert api.calls[-1] == ("unblock",) and not protocol.armed


def test_failing_sync_still_releases_the_block():
    api = FakeUser32()
    protocol = ShutdownProtocol(api)

    def boom(deadline):
        raise RuntimeError("ludusavi exploded")
    protocol.callback = boom
    protocol.precheck = lambda: True
    protocol.on_message(WM_QUERYENDSESSION, 0)
    protocol.on_message(WM_ENDSESSION, 1)
    assert api.calls[-1] == ("unblock",) and protocol.last_result is False


def test_precheck_errors_do_not_block_shutdown():
    api = FakeUser32()
    protocol = ShutdownProtocol(api)
    protocol.callback = lambda d: True
    protocol.precheck = lambda: 1 / 0
    assert protocol.on_message(WM_QUERYENDSESSION, 0) == (True, 1)
    assert api.calls == []
