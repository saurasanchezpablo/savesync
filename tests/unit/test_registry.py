"""Registry and config stores. Upstream tests/test_registry.py ported to the USB
fields; thread-safety and corruption handling kept."""
import json
import threading

from savesync.core.registry import (CONFIG_FIELDS, FIELDS, MIN_FULL_LIMIT, Config, Registry,
                                    title_key)


def test_title_key_normalizes():
    assert title_key("The Witcher 3: Wild Hunt") == "the-witcher-3-wild-hunt"
    assert title_key("Baldur's Gate 3") == "baldurs-gate-3"
    assert title_key("Łódź Story") == "lodz-story"
    assert title_key("  ") == ""


def test_title_without_latin_letters_gets_a_stable_non_empty_key():
    key = title_key("原神")
    assert key.startswith("t-") and key == title_key("原神")


def test_upsert_creates_blank_record_with_every_field(tmp_path):
    reg = Registry(str(tmp_path / "games.json"))
    rec = reg.upsert({"title": "Hades"})
    assert rec["title_key"] == "hades"
    for field in FIELDS:
        assert field in rec
    assert rec["save_paths"] == [] and rec["conflict"] is False


def test_mutable_defaults_are_not_shared(tmp_path):
    reg = Registry(str(tmp_path / "games.json"))
    a = reg.upsert({"title": "A"})
    a["save_paths"].append("x")
    assert reg.upsert({"title": "B"})["save_paths"] == []


def test_upsert_keeps_existing_fields(tmp_path):
    reg = Registry(str(tmp_path / "games.json"))
    reg.upsert({"title": "Hades", "last_synced_backup": "W1"})
    reg.upsert({"title": "Hades", "dirty": True})
    assert reg.get("hades")["last_synced_backup"] == "W1"


def test_set_fields_update_many_remove(tmp_path):
    reg = Registry(str(tmp_path / "games.json"))
    reg.upsert({"title": "Hades"})
    reg.set_fields("hades", excluded=True)
    assert reg.get("hades")["excluded"] is True
    reg.update_many({"hades": {"dirty": True}, "celeste": {"title": "Celeste", "state": "synced"},
                     "untitled": {"state": "x"}})
    assert reg.get("hades")["dirty"] is True
    assert reg.get("celeste")["state"] == "synced"
    assert reg.get("untitled") is None, "a record without a title is never created"
    assert reg.remove("hades") and not reg.remove("hades")


def test_corrupt_file_is_set_aside_not_silently_erased(tmp_path):
    path = tmp_path / "games.json"
    path.write_text("{ not json")
    reg = Registry(str(path))
    assert reg.all() == []
    assert (tmp_path / "games.json.broken").read_text() == "{ not json"
    reg.upsert({"title": "Hades"})
    assert json.loads(path.read_text())["hades"]["title"] == "Hades"


def test_concurrent_writers_do_not_lose_updates(tmp_path):
    reg = Registry(str(tmp_path / "games.json"))
    for i in range(20):
        reg.upsert({"title": "Game %d" % i})

    def worker(i):
        for _ in range(10):
            reg.set_fields("game-%d" % i, dirty=True, last_sync_ts=i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = json.loads((tmp_path / "games.json").read_text())
    assert len(data) == 20
    assert all(data["game-%d" % i]["last_sync_ts"] == i for i in range(20))


def test_config_defaults_and_persistence(tmp_path):
    cfg = Config(str(tmp_path / "config.json"))
    loaded = cfg.load()
    for key in CONFIG_FIELDS:
        assert key in loaded
    assert loaded["full_limit"] == 2 and loaded["differential_limit"] == 1
    assert loaded["sync_on_shutdown"] is False and loaded["sync_on_usb_connect"] is True
    cfg.update(usb_id="U1", notifications={"usb_connected": False})
    again = Config(str(tmp_path / "config.json")).load()
    assert again["usb_id"] == "U1"
    assert again["notifications"]["usb_connected"] is False
    assert again["notifications"]["conflict"] is True, "partial update keeps other keys"


def test_config_clamps_retention_that_would_break_rule_7(tmp_path):
    cfg = Config(str(tmp_path / "config.json"))
    cfg.update(full_limit=1, differential_limit=-3)
    loaded = cfg.load()
    assert loaded["full_limit"] == MIN_FULL_LIMIT
    assert loaded["differential_limit"] == 0
    cfg.update(full_limit="nonsense", ludusavi_command="C:/x/ludusavi.exe")
    loaded = cfg.load()
    assert loaded["full_limit"] == 2
    assert loaded["ludusavi_command"] == ["C:/x/ludusavi.exe"]
