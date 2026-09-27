import os

from savesync.core.state import GameState as S
from savesync.ui.widgets import ROW_ROLE
from tests_ui_helpers import rows_by_title


def _visible_titles(window):
    proxy = window.proxy
    return [proxy.index(i, 0).data(ROW_ROLE).title for i in range(proxy.rowCount())]


def _seed(env, states):
    for title, state in states.items():
        env.controller.registry.upsert({"title": title, "state": state.value,
                                        "state_message": {"message": "%s detail" % title}})
    env.controller.gamesChanged.emit()


def test_banner_without_registration(env):
    env.build_ui()
    assert env.window.banner.title.text() == "No USB registered"


def test_banner_and_last_sync_when_connected(ready):
    ready.world.write_save("Hades", "v1")
    ready.window.sync_button.click()
    assert ready.window.banner.title.text() == "USB connected — synchronized"
    assert "Last synchronization:" in ready.window.last_sync.text()
    assert "never" not in ready.window.last_sync.text()


def test_game_list_population_and_status(ready):
    ready.world.write_save("Hades", "v1")
    ready.world.write_save("Celeste", "c1")
    ready.window.sync_button.click()
    assert _visible_titles(ready.window) == ["Celeste", "Hades"]
    assert "✓ 2 synchronized" in ready.window.aggregate.text()


def test_categories_filter_the_list(ready):
    _seed(ready, {"A Synced": S.SYNCED, "B Up": S.LOCAL_NEWER, "C Down": S.USB_NEWER,
                  "D Conflict": S.CONFLICT, "E First": S.FIRST_SYNC, "F Missing": S.MISSING_LOCAL_PATH,
                  "G Running": S.RUNNING, "H Unknown": S.UNKNOWN, "I Error": S.ERROR})
    w = ready.window
    expected = {"All": 9, "Synchronized": ["A Synced"], "PC → USB": ["B Up"], "USB → PC": ["C Down"],
                "Conflicts": ["D Conflict"], "Pending": ["E First", "F Missing", "G Running", "H Unknown"],
                "Errors": ["I Error"]}
    for name, want in expected.items():
        w.select_category(name)
        got = _visible_titles(w)
        assert (len(got) == want) if isinstance(want, int) else (got == want), name
        assert w.tabs.tabText(w.tabs.currentIndex()).startswith(name)


def test_search_filter(ready):
    _seed(ready, {"Hades": S.SYNCED, "Hollow Knight": S.SYNCED, "Celeste": S.SYNCED})
    ready.window.search.setText("ho")
    assert _visible_titles(ready.window) == ["Hollow Knight"]


def test_attention_states_are_counted(ready):
    _seed(ready, {"E First": S.FIRST_SYNC, "I Error": S.ERROR, "H Unknown": S.UNKNOWN,
                  "D Conflict": S.CONFLICT})
    text = ready.window.aggregate.text()
    assert "⚠ 4 require attention" in text and "1 conflict" in text
    assert ready.tray.state == "red"


def test_per_game_status_changes_are_rendered(ready):
    ready.world.write_save("Hades", "v1")
    ready.window.sync_button.click()
    ready.world.write_save("Hades", "v2")
    ready.watcher.emit(ready.world.game_dir("Hades"))
    ready.window.select_category("PC → USB")
    assert _visible_titles(ready.window) == ["Hades"]
    ready.window.sync_button.click()
    assert _visible_titles(ready.window) == []
    ready.window.select_category("Synchronized")
    assert _visible_titles(ready.window) == ["Hades"]


def test_missing_local_path_messaging(ready, tmp_path):
    env = ready
    world = env.world
    other = os.path.join(world.root, "live", "Users", "alice", "AppData", "G", "s.sav")
    world.games["Alice Game"] = [os.path.dirname(other)]
    env.ludusavi.games = world.games
    from support import write
    write(other, "x")
    env.controller.sync_now()
    import shutil
    shutil.rmtree(os.path.join(world.root, "live", "Users", "alice"))
    env.controller.registry.remove("alice-game")
    env.controller.sync_now()
    row = rows_by_title(env)["Alice Game"]
    assert row.state == S.MISSING_LOCAL_PATH
    assert "no local save path" in row.message.lower()
    from savesync.ui.game_detail import GameDetailDialog
    detail = GameDetailDialog(env.controller, "Alice Game", env.window)
    assert detail.manage_button.isVisibleTo(detail)
    assert "No local save path" in detail.pc.text()


def test_game_running_messaging(ready):
    ready.world.write_save("Hades", "v1")
    ready.process.running.add("Hades")
    ready.answers["RunningPrompt"] = lambda d: d.continue_button.click()
    ready.window.sync_button.click()
    assert rows_by_title(ready)["Hades"].state == S.RUNNING
    assert "currently running" in rows_by_title(ready)["Hades"].message


def test_error_state_detail(ready):
    ready.world.write_save("Hades", "v1")
    ready.ludusavi.fail_paths.add(ready.world.save_path("Hades"))
    ready.window.sync_button.click()
    row = rows_by_title(ready)["Hades"]
    assert row.state == S.ERROR and "failed" in row.message
    assert ready.window.banner.title.text() == "USB connected — attention needed"


def test_closing_hides_to_tray(ready):
    ready.window.show()
    ready.window.close()
    assert not ready.window.isVisible()


def test_double_click_opens_detail_with_versions(ready):
    ready.world.write_save("Hades", "v1")
    ready.window.sync_button.click()
    ready.world.write_save("Hades", "v2")
    ready.window.sync_button.click()
    ready.window.open_game("Hades")
    (detail,) = ready.dialogs_of("GameDetailDialog")
    assert detail.versions.count() == 2
    assert "synchronized" in detail.versions.item(0).text()
    assert "🔒" in detail.versions.item(0).text()


def test_restore_older_version_from_detail(ready):
    ready.world.write_save("Hades", "v1")
    ready.window.sync_button.click()
    ready.world.write_save("Hades", "v2")
    ready.window.sync_button.click()

    def pick_oldest(dialog):
        dialog.versions.setCurrentRow(dialog.versions.count() - 1)
        dialog.restore_button.click()
    ready.answers["GameDetailDialog"] = pick_oldest
    ready.window.open_game("Hades")
    assert ready.world.read_save("Hades") == "v1"
    assert rows_by_title(ready)["Hades"].state == S.SYNCED
