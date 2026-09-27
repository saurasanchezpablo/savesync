from savesync.core.log import EventLog
from savesync.core.messages import msg
from savesync.ui.history import HistoryDialog, entry_cells


def test_history_renders_operations(ready):
    ready.world.write_save("Hades", "v1")
    ready.controller.sync_now()
    dialog = HistoryDialog(ready.controller.log)
    rows = [[dialog.table.item(r, c).text() for c in range(dialog.table.columnCount())]
            for r in range(dialog.table.rowCount())]
    backup = next(r for r in rows if r[3] == "BACKUP")
    assert backup[1] == "Hades" and backup[2] == "PC → USB" and backup[4] == "SUCCESS"
    assert backup[5].endswith(" s")


def test_history_filter_and_live_update(tmp_path, qtbot):
    log = EventLog(str(tmp_path / "events.jsonl"))
    log.add("restore", msg("restore_done", title="Cyberpunk 2077"), game="Cyberpunk 2077",
            source="USB", destination="PC", result="success", duration=1.8)
    log.add("backup", msg("backup_done", title="Hades"), game="Hades", source="PC",
            destination="USB", result="success")
    dialog = HistoryDialog(log)
    qtbot.addWidget(dialog)
    assert dialog.table.rowCount() == 2
    dialog.search.setText("cyber")
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 2).text() == "USB → PC" and dialog.table.item(0, 5).text() == "1.8 s"
    dialog.search.setText("")
    log.add("backup", "later", game="Celeste", result="failed", error="disk full")
    assert dialog.table.rowCount() == 3
    assert dialog.table.item(0, 6).text() == "disk full"


def test_entry_cells_tolerate_damaged_entries():
    assert entry_cells({})[0] == "?"
    assert entry_cells({"timestamp": "x", "duration": None})[5] == ""
