"""Reusable widgets: the game list model/delegate and the USB status banner."""
from __future__ import annotations

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QRect, QSize, QSortFilterProxyModel,
                            Qt)
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QStyle, QStyledItemDelegate, QVBoxLayout

from ..core.state import GameState
from .resources import GREEN, GREY, RED, STATE_COLORS, STATE_LABELS, YELLOW, state_icon, when_text

ROW_ROLE = Qt.ItemDataRole.UserRole + 1
STATE_ROLE = Qt.ItemDataRole.UserRole + 2


class GameListModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []

    def set_rows(self, rows) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def rows(self):
        return list(self._rows)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return row.title
        if role == Qt.ItemDataRole.ToolTipRole:
            return row.message or STATE_LABELS[row.display_state]
        if role == Qt.ItemDataRole.DecorationRole:
            return state_icon(row.display_state)
        if role == ROW_ROLE:
            return row
        if role == STATE_ROLE:
            return row.display_state
        return None


class GameFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._states = None
        self._text = ""
        self.setDynamicSortFilter(True)

    def _refilter(self, change) -> None:
        if hasattr(self, "beginFilterChange"):  # Qt >= 6.10
            self.beginFilterChange()
            change()
            self.endFilterChange()
        else:
            change()
            self.invalidateFilter()

    def set_states(self, states) -> None:
        self._refilter(lambda: setattr(self, "_states", set(states) if states else None))

    def set_text(self, text: str) -> None:
        self._refilter(lambda: setattr(self, "_text", (text or "").strip().lower()))

    def filterAcceptsRow(self, source_row, source_parent):
        index = self.sourceModel().index(source_row, 0, source_parent)
        row = index.data(ROW_ROLE)
        if row is None:
            return False
        if self._states is not None and row.display_state not in self._states:
            return False
        return not self._text or self._text in row.title.lower()


class GameDelegate(QStyledItemDelegate):
    """Icon · title · state · one line of detail."""
    HEIGHT = 46

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.HEIGHT)

    def paint(self, painter, option, index):
        row = index.data(ROW_ROLE)
        if row is None:
            return super().paint(painter, option, index)
        painter.save()
        rect = option.rect
        palette = option.palette
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(rect, palette.color(QPalette.ColorRole.Highlight))
            fg = palette.color(QPalette.ColorRole.HighlightedText)
        else:
            fg = palette.color(QPalette.ColorRole.Text)
        state = row.display_state
        icon_rect = QRect(rect.left() + 8, rect.top() + 9, 28, 28)
        state_icon(state).paint(painter, icon_rect)
        title_font = QFont(option.font)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(fg)
        text_left = icon_rect.right() + 10
        label = STATE_LABELS[state] + ("  ·  ⚠ Trial mode" if row.trial else "") + \
            ("  ·  excluded" if row.excluded else "")
        label_width = painter.fontMetrics().horizontalAdvance(label) + 12
        title_rect = QRect(text_left, rect.top() + 5, rect.width() - text_left - label_width, 20)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(row.title, Qt.TextElideMode.ElideRight,
                                                          title_rect.width()))
        painter.setFont(option.font)
        painter.setPen(QColor(STATE_COLORS[state]))
        painter.drawText(QRect(rect.right() - label_width, rect.top() + 5, label_width - 8, 20),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)
        detail = row.message or ("Last synchronized: %s" % when_text(row.last_sync_ts))
        if row.dirty and state == GameState.LOCAL_NEWER:
            detail = "Changed on this PC — will be synchronized with the USB"
        small = QFont(option.font)
        small.setPointSizeF(max(7.0, option.font.pointSizeF() - 1))
        painter.setFont(small)
        painter.setPen(QColor(GREY) if not option.state & QStyle.StateFlag.State_Selected else fg)
        detail_rect = QRect(text_left, rect.top() + 24, rect.width() - text_left - 8, 18)
        painter.drawText(detail_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(detail, Qt.TextElideMode.ElideRight,
                                                          detail_rect.width()))
        painter.restore()


class StatusBanner(QFrame):
    """USB connection and aggregate status at the top of the main window."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("statusBanner")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(self)
        self.dot = QLabel("●")
        self.dot.setFixedWidth(22)
        text = QVBoxLayout()
        self.title = QLabel()
        font = self.title.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 2)
        self.title.setFont(font)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        text.addWidget(self.title)
        text.addWidget(self.detail)
        layout.addWidget(self.dot)
        layout.addLayout(text, 1)

    def set_state(self, color: str, title: str, detail: str) -> None:
        self.dot.setStyleSheet("color: %s; font-size: 20px;" % color)
        self.title.setText(title)
        self.detail.setText(detail)
        self.setProperty("tone", {GREEN: "ok", YELLOW: "pending", RED: "problem"}.get(color, "idle"))
