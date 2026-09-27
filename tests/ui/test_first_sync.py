import os

import pytest

from savesync.core.state import GameState as S
from savesync.ui.first_sync import EXPLORE, SKIP, USE_PC, USE_USB, FirstSyncWizard
from tests_ui_helpers import rows_by_title


@pytest.fixture
def second_pc(ready):
    """PC A put Hades (and Celeste) on the USB; this env now acts as PC B."""
    env, world = ready, ready.world
    a = world.pc("A")
    a.register()
    world.write_save("Hades", "A-hades")
    world.write_save("Celeste", "A-celeste")
    a.sync()
    world.switch_pc("B")
    world.write_save("Hades", "B-hades")   # B has its own Hades save; no Celeste
    env.controller.config.update(sync_on_usb_connect=False)
    env.answers["FirstSyncWizard"] = lambda d: 0
    env.controller.sync_now()
    rows = rows_by_title(env)
    assert rows["Hades"].state == S.FIRST_SYNC and rows["Celeste"].state == S.FIRST_SYNC
    env.dialogs.clear()
    return env


def test_wizard_shows_both_sides_and_requires_a_choice(second_pc):
    wizard = FirstSyncWizard(second_pc.controller.rows())
    table = {wizard.table.item(i, 0).text(): (wizard.table.item(i, 1).text(),
                                               wizard.table.item(i, 2).text())
             for i in range(wizard.table.rowCount())}
    assert table == {"Celeste": ("existing backup", "no save"),
                     "Hades": ("existing backup", "existing save")}
    assert wizard.choices() == {}, "nothing is decided by default"
    celeste = wizard.combos[[r.title for r in wizard.rows].index("Celeste")]
    options = [celeste.itemData(i) for i in range(celeste.count())]
    assert USE_PC not in options and EXPLORE not in options


def test_cancel_changes_nothing(second_pc):
    second_pc.window.open_first_sync()
    assert second_pc.world.read_save("Hades") == "B-hades"
    assert second_pc.world.read_save("Celeste") is None


def test_use_usb_for_all(second_pc):
    env = second_pc

    def use_usb(dialog):
        dialog.all_usb.click()
        return 1
    env.answers["FirstSyncWizard"] = use_usb
    env.window.open_first_sync()
    assert env.world.read_save("Hades") == "A-hades"
    assert env.world.read_save("Celeste") == "A-celeste"
    rows = rows_by_title(env)
    assert rows["Hades"].state == S.SYNCED and rows["Celeste"].state == S.SYNCED
    assert env.controller.registry.get("hades")["last_synced_backup"], "baseline created"


def test_use_pc_for_one_and_usb_for_other(second_pc):
    env = second_pc

    def choose(dialog):
        dialog.set_choice("Hades", USE_PC)
        dialog.set_choice("Celeste", USE_USB)
        return 1
    env.answers["FirstSyncWizard"] = choose
    env.window.open_first_sync()
    assert env.world.read_save("Hades") == "B-hades"
    assert env.world.read_save("Celeste") == "A-celeste"
    env.controller.sync_now()
    assert all(r.state == S.SYNCED for r in env.controller.rows())


def test_explore_both_from_wizard(second_pc):
    env = second_pc
    env.answers["FirstSyncWizard"] = lambda d: (d.set_choice("Hades", EXPLORE), 1)[1]
    env.answers["TrialDialog"] = lambda d: d.keep_pc.click()
    env.window.open_first_sync()
    assert env.dialogs_of("TrialDialog")
    assert env.world.read_save("Hades") == "B-hades"
    assert rows_by_title(env)["Hades"].state == S.SYNCED


def test_skip_keeps_game_pending(second_pc):
    env = second_pc
    env.answers["FirstSyncWizard"] = lambda d: (d.set_choice("Celeste", USE_USB), 1)[1]
    env.window.open_first_sync()
    rows = rows_by_title(env)
    assert rows["Hades"].state == S.FIRST_SYNC
    assert rows["Celeste"].state == S.SYNCED
