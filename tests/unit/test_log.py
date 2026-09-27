import json
import pathlib

from savesync.core.log import EventLog, format_entry
from savesync.core.messages import CODES, msg, text


def test_entry_has_every_required_field(tmp_path):
    log = EventLog(str(tmp_path / "events.jsonl"))
    log.add("restore", msg("restore_done", title="Cyberpunk 2077"), game="Cyberpunk 2077",
            source="USB", destination="PC", result="success", duration=1.84)
    entry = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])
    for field in ("timestamp", "operation", "game", "source", "destination", "result",
                  "duration", "error"):
        assert field in entry
    assert entry["code"] == "restore_done"
    assert "Cyberpunk 2077" in entry["message"]


def test_log_is_append_only_and_tail_is_newest_first(tmp_path):
    log = EventLog(str(tmp_path / "events.jsonl"))
    for i in range(5):
        log.add("sync", "entry %d" % i)
    assert [e["message"] for e in log.tail(3)] == ["entry 4", "entry 3", "entry 2"]
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 5


def test_rotation_keeps_history_readable(tmp_path):
    log = EventLog(str(tmp_path / "events.jsonl"), max_bytes=400)
    for i in range(30):
        log.add("sync", "entry %d" % i)
    assert (tmp_path / "events.jsonl.1").exists()
    assert log.tail(1)[0]["message"] == "entry 29"
    assert len(log.tail(1000)) > 1


def test_damaged_lines_and_bad_values_never_break_the_log(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(str(path))
    log.add("sync", "good")
    with open(path, "ab") as fh:
        fh.write(b"\xff\xfe not json\n")
    log.add("sync", {"code": None, "params": None}, error=pathlib.Path("/x"))
    log.add("sync", msg("backup_done", title=pathlib.Path("/weird")))
    assert len(log.tail(10)) == 3
    assert log.tail("nonsense")


def test_listener_errors_are_contained(tmp_path):
    log = EventLog(str(tmp_path / "events.jsonl"))
    seen = []
    log.subscribe(lambda e: 1 / 0)
    log.subscribe(seen.append)
    log.add("sync", "x")
    assert seen and seen[0]["message"] == "x"


def test_unwritable_log_does_not_raise(tmp_path):
    (tmp_path / "blocker").write_text("file, not a directory")
    EventLog(str(tmp_path / "blocker" / "events.jsonl")).add("sync", "x")


def test_format_entry_is_human_readable():
    line = format_entry({"timestamp": 0, "game": "Cyberpunk 2077", "source": "USB",
                         "destination": "PC", "operation": "restore", "result": "success",
                         "duration": 1.8})
    assert "Cyberpunk 2077" in line and "USB → PC" in line
    assert "RESTORE" in line and "SUCCESS" in line and "1.8 s" in line


def test_messages_never_raise():
    assert msg("unknown_code_x")["code"] == "unknown_code_x"
    assert "{title}" in msg("backup_done")["message"]
    for code in CODES:
        assert msg(code)["message"]
    assert text(msg("backup_done", title="A")) == "A: PC → USB backup completed."
    assert text(None) == "" and text("plain") == "plain"
