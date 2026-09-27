"""Main status window (plan §19.2). A status application, not a Ludusavi clone."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QListView, QMainWindow,
                               QMessageBox, QProgressBar, QPushButton, QTabBar, QToolBar,
                               QVBoxLayout, QWidget)

from ..core.messages import text
from ..core.state import GameState, Outcome
from . import conflict as conflictmod
from . import first_sync as firstmod
from .conflict import ConflictDialog
from .first_sync import FirstSyncWizard
from .game_detail import GameDetailDialog
from .history import HistoryDialog
from .resources import CATEGORIES, GREEN, GREY, RED, STATE_LABELS, YELLOW, app_icon, when_text
from .settings import SettingsDialog
from .trial import TrialDialog
from .widgets import ROW_ROLE, GameDelegate, GameFilterProxy, GameListModel, StatusBanner

WAIT, SKIP, CONTINUE = "wait", "skip", "continue"


class RunningPrompt(QMessageBox):
    """"<game> is currently running." with the three choices of plan §12.8."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Game running")
        self.setIcon(QMessageBox.Icon.Information)
        self.setText("%s is currently running." % title)
        self.setInformativeText("Its saves were left untouched.")
        self.wait_button = self.addButton("Wait and synchronize", QMessageBox.ButtonRole.AcceptRole)
        self.skip_button = self.addButton("Skip this game", QMessageBox.ButtonRole.RejectRole)
        self.continue_button = self.addButton("Continue without synchronizing it",
                                              QMessageBox.ButtonRole.DestructiveRole)
        self.setDefaultButton(self.continue_button)

    def choice(self):
        clicked = self.clickedButton()
        if clicked is self.wait_button:
            return WAIT
        if clicked is self.skip_button:
            return SKIP
        return CONTINUE


class MainWindow(QMainWindow):
    # hooks tests replace to answer dialogs without a human
    dialog_exec = staticmethod(lambda dialog: dialog.exec())

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.queue = []
        self.setWindowTitle("Save Sync")
        self.setWindowIcon(app_icon())
        self.resize(760, 620)

        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        self.history_action = QAction("History", self)
        self.settings_action = QAction("Settings", self)
        self.conflicts_action = QAction("Conflicts", self)
        self.refresh_action = QAction("Refresh", self)
        self.refresh_action.setShortcut(QKeySequence.StandardKey.Refresh)
        for a in (self.conflicts_action, self.history_action, self.settings_action):
            toolbar.addAction(a)

        central = QWidget()
        layout = QVBoxLayout(central)
        self.banner = StatusBanner()
        layout.addWidget(self.banner)
        info = QHBoxLayout()
        self.last_sync = QLabel()
        self.aggregate = QLabel()
        self.aggregate.setAlignment(Qt.AlignmentFlag.AlignRight)
        info.addWidget(self.last_sync)
        info.addWidget(self.aggregate, 1)
        layout.addLayout(info)
        self.tabs = QTabBar()
        for name, _states in CATEGORIES:
            self.tabs.addTab(name)
        layout.addWidget(self.tabs)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter games…")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        self.model = GameListModel(self)
        self.proxy = GameFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.list = QListView()
        self.list.setModel(self.proxy)
        self.list.setItemDelegate(GameDelegate(self.list))
        self.list.setUniformItemSizes(True)
        layout.addWidget(self.list, 1)
        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet("color: %s;" % GREY)
        layout.addWidget(self.empty)
        bottom = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.status = QLabel()
        self.sync_button = QPushButton("Sync Now")
        self.sync_button.setDefault(True)
        self.sync_button.setMinimumWidth(130)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.progress)
        bottom.addWidget(self.sync_button)
        layout.addLayout(bottom)
        self.setCentralWidget(central)

        self.sync_button.clicked.connect(lambda: controller.sync_now(user=True))
        self.tabs.currentChanged.connect(self._on_tab)
        self.search.textChanged.connect(self.proxy.set_text)
        self.list.doubleClicked.connect(self._open_index)
        self.history_action.triggered.connect(self.open_history)
        self.settings_action.triggered.connect(self.open_settings)
        self.conflicts_action.triggered.connect(self.show_conflicts)
        self.refresh_action.triggered.connect(self.refresh)
        controller.usbChanged.connect(self.refresh)
        controller.gamesChanged.connect(self.refresh)
        controller.busyChanged.connect(self._on_busy)
        controller.syncProgress.connect(self._on_progress)
        controller.syncFinished.connect(self._on_sync_finished)
        controller.actionFinished.connect(self._on_action)
        controller.criticalCondition.connect(self._on_critical)
        controller.runningGames.connect(self._on_running)
        controller.message.connect(self.status.setText)
        self.refresh()

    # --- window behavior --------------------------------------------------------

    def closeEvent(self, event):
        # the application lives in the tray; closing the window only hides it
        event.ignore()
        self.hide()

    def bring_to_front(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # --- rendering ----------------------------------------------------------------

    def refresh(self) -> None:
        c = self.controller
        rows = c.rows()
        self.model.set_rows(rows)
        counts = c.aggregate()
        synced = counts.get(GameState.SYNCED, 0)
        attention = sum(n for s, n in counts.items()
                        if s in (GameState.CONFLICT, GameState.ERROR, GameState.UNKNOWN,
                                 GameState.FIRST_SYNC, GameState.MISSING_LOCAL_PATH,
                                 GameState.RUNNING))
        pending = counts.get(GameState.LOCAL_NEWER, 0) + counts.get(GameState.USB_NEWER, 0)
        parts = ["✓ %d synchronized" % synced]
        if pending:
            parts.append("⇅ %d pending" % pending)
        if attention:
            parts.append("⚠ %d require attention" % attention)
        conflicts = counts.get(GameState.CONFLICT, 0)
        if conflicts:
            parts.append("%d conflict%s" % (conflicts, "" if conflicts == 1 else "s"))
        self.aggregate.setText("   ".join(parts))
        self.last_sync.setText("Last synchronization: %s" % when_text(c.config.get("last_sync_ts")))
        for i, (name, states) in enumerate(CATEGORIES):
            n = len(rows) if states is None else sum(
                1 for r in rows if r.display_state in states and not r.excluded)
            self.tabs.setTabText(i, "%s (%d)" % (name, n))
        self._render_banner()
        self.conflicts_action.setText("Conflicts (%d)" % conflicts if conflicts else "Conflicts")
        self.sync_button.setEnabled(c.drive is not None and not c.busy)
        visible = self.proxy.rowCount()
        self.empty.setVisible(visible == 0)
        self.empty.setText("No games yet — connect the USB and press Sync Now."
                           if not rows else "No games in this category.")

    def _render_banner(self) -> None:
        c = self.controller
        state = c.tray_state()
        if c.drive is None:
            from ..app import UsbStatus
            if c.usb_status == UsbStatus.UNKNOWN:
                label = c.other_drive.label or c.other_drive.drive_letter
                self.banner.set_state(RED, "Unknown USB",
                                      "%s is not the registered Save Sync drive. Nothing is read "
                                      "from or written to it." % label)
            elif c.usb_status == UsbStatus.CORRUPT:
                self.banner.set_state(RED, "USB not trusted",
                                      "The Save Sync marker on the drive is damaged.")
            elif c.usb_status == UsbStatus.UNREGISTERED:
                self.banner.set_state(YELLOW, "Save Sync USB found",
                                      "Register it in Settings to synchronize with this PC.")
            elif not (c.config.get("usb_id") or ""):
                self.banner.set_state(GREY, "No USB registered",
                                      "Open Settings and choose the drive that will carry your saves.")
            else:
                pending = [r for r in c.rows() if r.display_state == GameState.LOCAL_NEWER]
                detail = ("The PC saves of %d game(s) are newer; they will be synchronized when "
                          "you reconnect the USB." % len(pending)) if pending else \
                    "Connect the registered USB to synchronize."
                self.banner.set_state(GREY, "USB not connected", detail)
            return
        name = c.drive.display_name
        if c.busy:
            self.banner.set_state(YELLOW, "Synchronizing…", name)
        elif state == "red":
            problem = text(c.last_problem) if c.last_problem else "Some games need your attention."
            self.banner.set_state(RED, "USB connected — attention needed", "%s · %s" % (name, problem))
        elif state == "yellow":
            self.banner.set_state(YELLOW, "USB connected — pending changes", name)
        else:
            self.banner.set_state(GREEN, "USB connected — synchronized", name)

    def _on_tab(self, index: int) -> None:
        self.proxy.set_states(CATEGORIES[index][1])
        self.refresh()

    def select_category(self, name: str) -> None:
        for i, (label, _s) in enumerate(CATEGORIES):
            if label == name:
                self.tabs.setCurrentIndex(i)

    def _on_busy(self, busy: bool) -> None:
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)
            self.status.setText("Synchronizing…")
        self.refresh()
        if not busy:
            # decisions made while a sync ran would otherwise wait in the queue
            # until some unrelated action finished
            QTimer.singleShot(0, self._next)

    def _on_progress(self, stage: str, done: int, total: int, title: str) -> None:
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        self.status.setText(title or stage.capitalize() + "…")

    def _on_sync_finished(self, report) -> None:
        synced, attention = len(report.synchronized), len(report.attention)
        text_ = "✓ %d synchronized" % synced
        if attention:
            text_ += "   ⚠ %d require attention" % attention
        if report.errors:
            text_ += "   — " + text(report.errors[0])
        self.status.setText(text_)
        self.refresh()

    # --- dialogs ----------------------------------------------------------------------

    def _open_index(self, index) -> None:
        row = index.data(ROW_ROLE)
        if row is not None:
            self.open_game(row.title)

    def open_game(self, title: str):
        row = self.controller.row(title)
        if row is None:
            return None
        if row.trial:
            return self.open_trial(title)
        dialog = GameDetailDialog(self.controller, title, self)
        self.dialog_exec(dialog)
        if dialog.action == "resolve":
            self.resolve(title)
        return dialog

    def resolve(self, title: str) -> None:
        row = self.controller.row(title)
        if row is None:
            return
        if row.state == GameState.FIRST_SYNC:
            self.open_first_sync([row])
        else:
            self.open_conflict(title)

    def open_conflict(self, title: str):
        row = self.controller.row(title)
        dialog = ConflictDialog(row, self)
        self.dialog_exec(dialog)
        self.dispatch({title: dialog.choice} if dialog.choice else {})
        return dialog

    def open_first_sync(self, rows=None):
        rows = rows if rows is not None else [
            r for r in self.controller.rows() if r.state == GameState.FIRST_SYNC and not r.trial]
        rows = [r for r in rows if not r.trial]
        if not rows:
            return None
        wizard = FirstSyncWizard(rows, self)
        if self.dialog_exec(wizard):
            self.dispatch(wizard.choices())
        return wizard

    def open_trial(self, title: str):
        dialog = TrialDialog(self.controller, title, self)
        self.dialog_exec(dialog)
        return dialog

    def open_settings(self):
        dialog = SettingsDialog(self.controller, self)
        self.dialog_exec(dialog)
        self.refresh()
        return dialog

    def open_history(self):
        dialog = HistoryDialog(self.controller.log, self)
        self.dialog_exec(dialog)
        return dialog

    def show_conflicts(self) -> None:
        self.bring_to_front()
        self.select_category("Conflicts")

    # --- dispatching decisions ------------------------------------------------------------

    def dispatch(self, choices: dict) -> None:
        """Run explicit choices one after another (the worker does one at a time)."""
        mapping = {conflictmod.USE_USB: self.controller.use_usb,
                   conflictmod.USE_PC: self.controller.use_pc,
                   conflictmod.EXPLORE: self.controller.trial_start}
        for title, choice in choices.items():
            action = mapping.get(choice) or {firstmod.USE_USB: self.controller.use_usb,
                                             firstmod.USE_PC: self.controller.use_pc,
                                             firstmod.EXPLORE: self.controller.trial_start}.get(choice)
            if action is not None:
                self.queue.append((action, title))
        self._next()

    def _next(self) -> None:
        """Run the next queued decision — only if it still applies: a sync that
        ran in between may have changed the game (or started a trial)."""
        while self.queue and not self.controller.busy:
            action, title = self.queue.pop(0)
            row = self.controller.row(title)
            if row is None or row.trial or row.state not in (GameState.CONFLICT,
                                                             GameState.FIRST_SYNC):
                self.status.setText("%s: the decision no longer applies (%s)." % (
                    title, STATE_LABELS[row.state] if row else "unknown game"))
                continue
            if action(title):
                return

    def _on_action(self, name: str, result) -> None:
        if result.message:
            self.status.setText(text(result.message))
        if name == "trial_start" and result.outcome == Outcome.RESTORED:
            self.open_trial(result.title)
        self.refresh()
        self._next()

    def _on_critical(self, kind: str, payload) -> None:
        """The only automatic window openings (plan §19.1)."""
        self.bring_to_front()
        if kind == "setup":
            self.open_settings()
        elif kind == "first_sync":
            self.open_first_sync()
        elif kind == "conflict":
            self.select_category("Conflicts")
        elif kind == "error":
            self.select_category("Errors")

    def _on_running(self, titles) -> None:
        for title in titles:
            prompt = RunningPrompt(title, self)
            self.dialog_exec(prompt)
            choice = prompt.choice()
            if choice == WAIT:
                self.controller.wait_and_sync(title)
            elif choice == SKIP:
                self.controller.skip_game(title)
