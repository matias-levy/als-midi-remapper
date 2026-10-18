"""Table window for editing MIDI mappings in an Ableton Live set."""

from __future__ import annotations

import math
import re
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtGui import QAction, QBrush, QColor, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QStatusBar,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from alsio import AlsError, default_save_path
from mappings import (
    MAP_MODES,
    LiveSet,
    Mapping,
    commit_range_text,
    map_mode_label,
    note_name,
    number_display,
    range_display,
)

COL_TRACK = 0
COL_DEVICE = 1
COL_PARAM = 2
COL_TYPE = 3
COL_CHANNEL = 4
COL_NUMBER = 5
COL_MIN = 6
COL_MAX = 7
COL_MODE = 8

NUMERIC_COLUMNS = {COL_CHANNEL, COL_NUMBER, COL_MIN, COL_MAX}
HEADERS = [
    "Track",
    "Device",
    "Parameter",
    "Type",
    "Channel",
    "Number",
    "Range min",
    "Range max",
    "Map mode",
]
LEADING_INT = re.compile(r"\s*(-?\d+)")
CHANGED_TINT = QColor(214, 140, 24, 110)
UNDO_LIMIT = 100
MAPPING_ROLE = Qt.ItemDataRole.UserRole


def _mapping_state(mapping: Mapping) -> tuple:
    return (
        mapping.is_note,
        mapping.channel,
        mapping.number,
        mapping.map_mode,
        mapping.range_min,
        mapping.range_max,
    )


def _range_sort_value(mapping: Mapping, which: str) -> tuple[int, float]:
    raw = mapping.range_min if which == "min" else mapping.range_max
    if raw is None or mapping.range_unit == "none":
        return (1, 0.0)
    if mapping.range_unit == "db":
        amplitude = float(raw)
        decibels = float("-inf") if amplitude <= 0 else 20 * math.log10(amplitude)
        return (0, decibels)
    return (0, float(raw))


def _restore_mapping(mapping: Mapping, state: tuple) -> None:
    (
        mapping.is_note,
        mapping.channel,
        mapping.number,
        mapping.map_mode,
        mapping.range_min,
        mapping.range_max,
    ) = state


class MappingComboBox(QComboBox):
    """Closed dropdowns let the cell show through, and ignore the wheel until opened."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    def paintEvent(self, event) -> None:  # noqa: ANN001
        painter = QStylePainter(self)
        painter.setClipRect(event.rect())
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)
        option.subControls = QStyle.SubControl.SC_ComboBoxArrow
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)

    def wheelEvent(self, event) -> None:  # noqa: ANN001
        if self.view().isVisible():
            super().wheelEvent(event)
            return
        event.ignore()


class IntDelegate(QStyledItemDelegate):
    def __init__(self, minimum: int, maximum: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._minimum = minimum
        self._maximum = maximum

    def createEditor(self, parent: QWidget, option, index):  # noqa: ANN001
        spin = QSpinBox(parent)
        spin.setRange(self._minimum, self._maximum)
        spin.setFrame(False)
        return spin

    def setEditorData(self, editor: QSpinBox, index) -> None:  # noqa: ANN001
        match = LEADING_INT.match(index.data() or "")
        editor.setValue(int(match.group(1)) if match else self._minimum)

    def setModelData(self, editor: QSpinBox, model, index) -> None:  # noqa: ANN001
        model.setData(index, str(editor.value()))


class RemapDialog(QDialog):
    """Set a channel and number on the selection, or replace one pair everywhere."""

    def __init__(self, parent: QWidget, selection_count: int) -> None:
        super().__init__(parent)
        self.setWindowTitle("Remap")
        self._selection_count = selection_count

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Point existing mappings at a new channel or number. "
            "Nothing is written until you Save As."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.mode = QComboBox()
        self.mode.addItem(f"Selected rows ({selection_count})", "selected")
        self.mode.addItem("Every row with this channel and number", "match")
        self.mode.addItem("Every row with this channel", "channel")
        if selection_count == 0:
            self.mode.setCurrentIndex(1)
        layout.addWidget(self.mode)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_selected_page())
        self._stack.addWidget(self._build_match_page())
        self._stack.addWidget(self._build_channel_page())
        self._stack.setCurrentIndex(self.mode.currentIndex())
        self.mode.currentIndexChanged.connect(self._stack.setCurrentIndex)
        layout.addWidget(self._stack)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(420, 280)

    def _build_selected_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        channel_row = QHBoxLayout()
        self.set_channel = QCheckBox("Set channel")
        self.channel = QSpinBox()
        self.channel.setRange(1, 16)
        channel_row.addWidget(self.set_channel)
        channel_row.addWidget(self.channel)
        form.addRow(channel_row)

        number_row = QHBoxLayout()
        self.set_number = QCheckBox("Set number")
        self.number = QSpinBox()
        self.number.setRange(0, 127)
        number_row.addWidget(self.set_number)
        number_row.addWidget(self.number)
        form.addRow(number_row)
        form.addRow(QLabel("The other value on each selected row stays as it is."))
        return page

    def _build_match_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.from_channel = QSpinBox()
        self.from_channel.setRange(1, 16)
        self.from_number = QSpinBox()
        self.from_number.setRange(0, 127)
        self.to_channel = QSpinBox()
        self.to_channel.setRange(1, 16)
        self.to_number = QSpinBox()
        self.to_number.setRange(0, 127)
        form.addRow("From channel", self.from_channel)
        form.addRow("From number", self.from_number)
        form.addRow("To channel", self.to_channel)
        form.addRow("To number", self.to_number)
        note = QLabel("Matches CC and note mappings. Type stays the same.")
        note.setWordWrap(True)
        form.addRow(note)
        return page

    def _build_channel_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.channel_from = QSpinBox()
        self.channel_from.setRange(1, 16)
        self.channel_to = QSpinBox()
        self.channel_to.setRange(1, 16)
        form.addRow("From channel", self.channel_from)
        form.addRow("To channel", self.channel_to)
        note = QLabel("Changes the channel on every matching row. Numbers stay as they are.")
        note.setWordWrap(True)
        form.addRow(note)
        return page

    def accept(self) -> None:
        if self.mode.currentData() == "selected":
            if self._selection_count == 0:
                QMessageBox.warning(self, "Remap", "Select one or more rows first.")
                return
            if not self.set_channel.isChecked() and not self.set_number.isChecked():
                QMessageBox.warning(self, "Remap", "Choose a channel, a number, or both.")
                return
        super().accept()

    def spec(self) -> dict:
        if self.mode.currentData() == "selected":
            return {
                "mode": "selected",
                "channel": self.channel.value() if self.set_channel.isChecked() else None,
                "number": self.number.value() if self.set_number.isChecked() else None,
            }
        if self.mode.currentData() == "channel":
            return {
                "mode": "channel",
                "from_channel": self.channel_from.value(),
                "to_channel": self.channel_to.value(),
            }
        return {
            "mode": "match",
            "from_channel": self.from_channel.value(),
            "from_number": self.from_number.value(),
            "to_channel": self.to_channel.value(),
            "to_number": self.to_number.value(),
        }


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.live_set: LiveSet | None = None
        self.source_path: Path | None = None
        self._directory = str(Path.home())
        self._undo: list[dict[int, tuple]] = []
        self._redo: list[dict[int, tuple]] = []
        self._display_order: list[int] = []
        self._sort_column: int | None = None
        self._sort_ascending = True

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setItemDelegateForColumn(COL_CHANNEL, IntDelegate(1, 16, self.table))
        self.table.setItemDelegateForColumn(COL_NUMBER, IntDelegate(0, 127, self.table))
        self.table.itemChanged.connect(self._on_item_changed)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(False)
        header.sectionClicked.connect(self._sort_by_column)
        for column, width in (
            (COL_TRACK, 140),
            (COL_DEVICE, 180),
            (COL_PARAM, 160),
            (COL_TYPE, 80),
            (COL_CHANNEL, 80),
            (COL_NUMBER, 110),
            (COL_MIN, 100),
            (COL_MAX, 100),
            (COL_MODE, 280),
        ):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeaderItem(COL_NUMBER).setToolTip(
            "CC number, or note number. Ableton calls note 60 C3."
        )
        self.table.horizontalHeaderItem(COL_MIN).setToolTip(
            "Volume and Utility Gain are shown in dB. Other controls use Live's internal value."
        )
        self.table.horizontalHeaderItem(COL_MODE).setToolTip(
            "Absolute for faders and knobs. Relative modes are for endless encoders."
        )

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by track, device, parameter, or number")
        self.filter.setClearButtonEnabled(True)
        self.filter.setMinimumWidth(280)
        self.filter.textChanged.connect(self._apply_filter)

        self.setCentralWidget(self.table)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Open a .als file")
        self._build_actions()
        self.resize(1280, 640)
        self._update_title()

    def _build_actions(self) -> None:
        style = self.style()
        open_action = QAction("Open...", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton))
        open_action.triggered.connect(self._open)

        self.save_action = QAction("Save As...", self)
        self.save_action.setShortcut(QKeySequence.StandardKey.Save)
        self.save_action.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
        self.save_action.setEnabled(False)
        self.save_action.triggered.connect(self.save_as)

        self.remap_action = QAction("Remap...", self)
        self.remap_action.setShortcut(QKeySequence("Ctrl+R"))
        self.remap_action.setEnabled(False)
        self.remap_action.triggered.connect(self._remap)

        self.undo_action = QAction("Undo", self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self.undo)
        self.redo_action = QAction("Redo", self)
        self.redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.redo_action.triggered.connect(self.redo)
        self._update_history_actions()

        quit_action = QAction("Quit", self)
        quit_action.setMenuRole(QAction.MenuRole.QuitRole)
        quit_action.triggered.connect(self.close)

        file_menu = self.menuBar().addMenu("File")
        file_menu.addAction(open_action)
        file_menu.addAction(self.save_action)
        file_menu.addSeparator()
        file_menu.addAction(quit_action)
        edit_menu = self.menuBar().addMenu("Edit")
        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.remap_action)

        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.addAction(open_action)
        toolbar.addAction(self.save_action)
        toolbar.addAction(self.remap_action)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        toolbar.addWidget(self.filter)
        self.addToolBar(toolbar)

    def load_path(self, path: Path) -> None:
        try:
            live = LiveSet.load(path)
        except (AlsError, OSError) as exc:
            QMessageBox.critical(self, "Could not open", str(exc))
            return
        self.live_set = live
        self.source_path = path
        self._undo.clear()
        self._redo.clear()
        self._update_history_actions()
        self._display_order = []
        self._sort_column = None
        self._sort_ascending = True
        self.table.horizontalHeader().setSortIndicatorShown(False)
        self._directory = str(path.parent)
        self._fill_table()
        self.save_action.setEnabled(True)
        self.remap_action.setEnabled(True)
        count = len(live.mappings)
        noun = "mapping" if count == 1 else "mappings"
        creator = live.creator or "Ableton Live set"
        self.statusBar().showMessage(f"{creator} — {count} {noun}")
        self._update_title()

    def save_as(self) -> bool:
        if self.live_set is None or self.source_path is None:
            return False
        suggested = default_save_path(self.source_path)
        chosen, _filter = QFileDialog.getSaveFileName(
            self,
            "Save Ableton set",
            str(suggested),
            "Ableton Live Set (*.als)",
        )
        if not chosen:
            return False
        dest = Path(chosen)
        if dest.suffix.lower() != ".als":
            dest = Path(str(dest) + ".als")
        try:
            self.live_set.save(dest)
        except (AlsError, OSError) as exc:
            QMessageBox.critical(self, "Could not save", str(exc))
            return False
        self.statusBar().showMessage(f"Saved {dest.name}")
        self._tint_all()
        self._update_title()
        return True

    def closeEvent(self, event) -> None:  # noqa: ANN001
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()

    def _open(self) -> None:
        if not self._confirm_discard():
            return
        chosen, _filter = QFileDialog.getOpenFileName(
            self,
            "Open Ableton set",
            self._directory,
            "Ableton Live Set (*.als);;All files (*.*)",
        )
        if chosen:
            self.load_path(Path(chosen))

    def _confirm_discard(self) -> bool:
        if self.live_set is None or not self.live_set.is_dirty:
            return True
        answer = QMessageBox.question(
            self,
            "Save changes?",
            "This set has MIDI mapping edits that are not saved.",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self.save_as()
        return answer == QMessageBox.StandardButton.Discard

    def _fill_table(self) -> None:
        mappings = self.live_set.mappings if self.live_set else []
        if len(self._display_order) != len(mappings):
            self._display_order = list(range(len(mappings)))
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.setRowCount(len(self._display_order))
        for row, mapping_index in enumerate(self._display_order):
            self._populate_row(row, mapping_index)
        self.table.blockSignals(False)
        self._apply_filter()
        self.table.updateGeometries()

    def _populate_row(self, row: int, mapping_index: int) -> None:
        mapping = self.live_set.mappings[mapping_index]  # type: ignore[union-attr]
        self._set_text(row, COL_TRACK, mapping.track, False)
        self._set_text(row, COL_DEVICE, mapping.device, False)
        self._set_text(row, COL_PARAM, mapping.parameter, False)
        self._set_text(row, COL_CHANNEL, str(mapping.channel), True)
        self._set_text(row, COL_NUMBER, number_display(mapping), True)
        has_range = mapping.range_unit != "none"
        self._set_text(row, COL_MIN, range_display(mapping, "min"), has_range)
        self._set_text(row, COL_MAX, range_display(mapping, "max"), has_range)
        self._set_range_tip(row, mapping)
        self._set_choice(row, COL_TYPE, ((False, "CC"), (True, "Note")), mapping.is_note, self._type_changed)
        self._set_choice(row, COL_MODE, MAP_MODES, mapping.map_mode, self._mode_changed)
        for column in range(self.table.columnCount()):
            item = self.table.item(row, column)
            if item is not None:
                item.setData(MAPPING_ROLE, mapping_index)
        self._tint_row(row)

    def _mapping_index(self, row: int) -> int:
        item = self.table.item(row, COL_TRACK)
        if item is None:
            raise RuntimeError("A table row is missing its mapping.")
        return int(item.data(MAPPING_ROLE))

    def _mapping_for_row(self, row: int) -> Mapping:
        return self.live_set.mappings[self._mapping_index(row)]  # type: ignore[union-attr]

    def _visual_row(self, mapping_index: int) -> int:
        for row in range(self.table.rowCount()):
            if self._mapping_index(row) == mapping_index:
                return row
        raise RuntimeError("A mapping is missing from the table.")

    def _set_choice(self, row: int, column: int, options: tuple, current, on_change) -> None:  # noqa: ANN001
        if self.table.item(row, column) is None:
            backing = QTableWidgetItem()
            backing.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(row, column, backing)
        combo = self.table.cellWidget(row, column)
        if not isinstance(combo, QComboBox):
            combo = MappingComboBox()
            for value, label in options:
                combo.addItem(label, value)
            # Fit-to-contents calls adjustSize() and pulls the closed combo back to its label.
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            combo.setMinimumWidth(0)
            combo.currentIndexChanged.connect(lambda _index, row=row, column=column: on_change(row))
            self.table.setCellWidget(row, column, combo)
        index = next((i for i in range(combo.count()) if combo.itemData(i) == current), -1)
        if index < 0:
            label = map_mode_label(current) if column == COL_MODE else str(current)
            combo.addItem(label, current)
            index = combo.count() - 1
        combo.blockSignals(True)
        combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _sync_row(self, row: int) -> None:
        if self.live_set is None:
            return
        self.table.blockSignals(True)
        try:
            self._populate_row(row, self._mapping_index(row))
        finally:
            self.table.blockSignals(False)
        self._update_title()

    def _set_text(self, row: int, column: int, text: str, editable: bool) -> None:
        item = self.table.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            self.table.setItem(row, column, item)
        flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        if editable:
            flags |= Qt.ItemFlag.ItemIsEditable
        item.setFlags(flags)
        if column in NUMERIC_COLUMNS:
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        item.setText(text)

    def _tint_all(self) -> None:
        if self.live_set is None:
            return
        for row in range(self.table.rowCount()):
            self._tint_row(row)

    def _tint_row(self, row: int) -> None:
        if self.live_set is None or row >= self.table.rowCount():
            return
        mapping = self._mapping_for_row(row)
        self._mark_item(row, COL_CHANNEL, mapping.channel != mapping.orig_channel)
        self._mark_item(row, COL_NUMBER, mapping.number != mapping.orig_number)
        self._mark_item(row, COL_MIN, mapping.range_min != mapping.orig_range_min)
        self._mark_item(row, COL_MAX, mapping.range_max != mapping.orig_range_max)
        self._mark_item(row, COL_TYPE, mapping.is_note != mapping.orig_is_note)
        self._mark_item(row, COL_MODE, mapping.map_mode != mapping.orig_map_mode)

    def _mark_item(self, row: int, column: int, changed: bool) -> None:
        item = self.table.item(row, column)
        if item is None:
            return
        if changed:
            item.setBackground(QBrush(CHANGED_TINT))
        else:
            item.setBackground(QBrush(Qt.BrushStyle.NoBrush))

    def _set_range_tip(self, row: int, mapping: Mapping) -> None:
        if mapping.range_unit == "db":
            tip = "Decibels. Live stores this as amplitude, 10^(dB/20)."
        elif mapping.range_unit == "raw":
            tip = "Live's internal value for this control."
        else:
            tip = ""
        for column in (COL_MIN, COL_MAX):
            item = self.table.item(row, column)
            if item is not None:
                item.setToolTip(tip)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self.live_set is None:
            return
        row = item.row()
        column = item.column()
        if column == COL_CHANNEL:
            self._commit_channel(row, item.text())
        elif column == COL_NUMBER:
            self._commit_number(row, item.text())
        elif column in (COL_MIN, COL_MAX):
            self._commit_range(row, column, item.text())

    def _commit_channel(self, row: int, text: str) -> None:
        mapping = self._mapping_for_row(row)
        before = self._capture_state()
        match = LEADING_INT.match(text or "")
        value = int(match.group(1)) if match else None
        if value is None or not 1 <= value <= 16:
            self.statusBar().showMessage("Channel must be from 1 to 16.")
        else:
            mapping.channel = value
        self._record_edit(before)
        self._sync_row(row)

    def _commit_number(self, row: int, text: str) -> None:
        mapping = self._mapping_for_row(row)
        before = self._capture_state()
        match = LEADING_INT.match(text or "")
        value = int(match.group(1)) if match else None
        if value is None or not 0 <= value <= 127:
            self.statusBar().showMessage("Number must be from 0 to 127.")
        else:
            mapping.number = value
        self._record_edit(before)
        self._sync_row(row)

    def _commit_range(self, row: int, column: int, text: str) -> None:
        mapping = self._mapping_for_row(row)
        which = "min" if column == COL_MIN else "max"
        before = self._capture_state()
        try:
            raw = commit_range_text(mapping, which, text)
        except ValueError:
            self.statusBar().showMessage("Enter a number. Levels in dB can look like -6 or 0 dB.")
            self._sync_row(row)
            return
        if which == "min":
            mapping.range_min = raw
        else:
            mapping.range_max = raw
        self._record_edit(before)
        self._sync_row(row)

    def _type_changed(self, row: int) -> None:
        if self.live_set is None:
            return
        combo = self.table.cellWidget(row, COL_TYPE)
        if not isinstance(combo, QComboBox):
            return
        before = self._capture_state()
        self._mapping_for_row(row).is_note = bool(combo.currentData())
        self._record_edit(before)
        self._sync_row(row)

    def _mode_changed(self, row: int) -> None:
        if self.live_set is None:
            return
        combo = self.table.cellWidget(row, COL_MODE)
        if not isinstance(combo, QComboBox):
            return
        value = combo.currentData()
        if value is None:
            return
        before = self._capture_state()
        self._mapping_for_row(row).map_mode = int(value)
        self._record_edit(before)
        self._tint_row(row)
        self._update_title()

    def _apply_filter(self) -> None:
        if self.live_set is None:
            return
        query = self.filter.text().casefold().strip()
        for row in range(self.table.rowCount()):
            mapping = self._mapping_for_row(row)
            haystack = " ".join(
                [
                    mapping.track,
                    mapping.device,
                    mapping.parameter,
                    str(mapping.channel),
                    str(mapping.number),
                    note_name(mapping.number) if mapping.is_note else "",
                    "note" if mapping.is_note else "cc",
                ]
            ).casefold()
            self.table.setRowHidden(row, bool(query) and query not in haystack)

    def _selected_rows(self) -> list[int]:
        return sorted({index.row() for index in self.table.selectionModel().selectedRows()})

    def _remap(self) -> None:
        if self.live_set is None:
            return
        dialog = RemapDialog(self, len(self._selected_rows()))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.apply_spec(dialog.spec())

    def apply_spec(self, spec: dict) -> int:
        if self.live_set is None:
            return 0
        before = self._capture_state()
        if spec["mode"] == "selected":
            targets = [self._mapping_for_row(row) for row in self._selected_rows()]
            if not targets:
                self.statusBar().showMessage("Select one or more rows first.")
                return 0
            for mapping in targets:
                if spec["channel"] is not None:
                    mapping.channel = spec["channel"]
                if spec["number"] is not None:
                    mapping.number = spec["number"]
        elif spec["mode"] == "channel":
            targets = [
                mapping for mapping in self.live_set.mappings if mapping.channel == spec["from_channel"]
            ]
            if not targets:
                self.statusBar().showMessage("No mappings use that channel.")
                return 0
            for mapping in targets:
                mapping.channel = spec["to_channel"]
        else:
            targets = [
                mapping
                for mapping in self.live_set.mappings
                if mapping.channel == spec["from_channel"] and mapping.number == spec["from_number"]
            ]
            if not targets:
                self.statusBar().showMessage("No mappings use that channel and number.")
                return 0
            for mapping in targets:
                mapping.channel = spec["to_channel"]
                mapping.number = spec["to_number"]
        self._record_edit(before)
        target_ids = {id(mapping) for mapping in targets}
        for row in range(self.table.rowCount()):
            if id(self._mapping_for_row(row)) in target_ids:
                self._sync_row(row)
        count = len(targets)
        noun = "mapping" if count == 1 else "mappings"
        self.statusBar().showMessage(f"Updated {count} {noun}.")
        return count

    def _capture_state(self) -> dict[int, tuple]:
        if self.live_set is None:
            return {}
        return {index: _mapping_state(mapping) for index, mapping in enumerate(self.live_set.mappings)}

    def _record_edit(self, before: dict[int, tuple]) -> None:
        if self.live_set is None:
            return
        changed = {
            index: state
            for index, state in before.items()
            if _mapping_state(self.live_set.mappings[index]) != state
        }
        if not changed:
            return
        self._undo.append(changed)
        del self._undo[:-UNDO_LIMIT]
        self._redo.clear()
        self._update_history_actions()

    def _restore_entry(self, entry: dict[int, tuple]) -> dict[int, tuple]:
        inverse: dict[int, tuple] = {}
        for index, state in entry.items():
            mapping = self.live_set.mappings[index]  # type: ignore[union-attr]
            inverse[index] = _mapping_state(mapping)
            _restore_mapping(mapping, state)
            self._sync_row(self._visual_row(index))
        return inverse

    def undo(self) -> None:
        if self.live_set is None or not self._undo:
            return
        self._redo.append(self._restore_entry(self._undo.pop()))
        self._update_history_actions()
        self.statusBar().showMessage("Undid the last change.")

    def redo(self) -> None:
        if self.live_set is None or not self._redo:
            return
        self._undo.append(self._restore_entry(self._redo.pop()))
        self._update_history_actions()
        self.statusBar().showMessage("Redid the last change.")

    def _sort_by_column(self, column: int) -> None:
        if self.live_set is None or not self.live_set.mappings:
            return
        if self._sort_column == column:
            self._sort_ascending = not self._sort_ascending
        else:
            self._sort_column = column
            self._sort_ascending = True
        selected = {self._mapping_index(row) for row in self._selected_rows()}
        order = list(range(len(self.live_set.mappings)))
        order.sort(key=lambda index: self._sort_value(index, column), reverse=not self._sort_ascending)
        self._display_order = order
        self._fill_table()
        header = self.table.horizontalHeader()
        header.setSortIndicator(
            column,
            Qt.SortOrder.AscendingOrder if self._sort_ascending else Qt.SortOrder.DescendingOrder,
        )
        header.setSortIndicatorShown(True)
        self._restore_selection(selected)

    def _sort_value(self, mapping_index: int, column: int):
        mapping = self.live_set.mappings[mapping_index]  # type: ignore[union-attr]
        if column == COL_TRACK:
            return mapping.track.casefold()
        if column == COL_DEVICE:
            return mapping.device.casefold()
        if column == COL_PARAM:
            return mapping.parameter.casefold()
        if column == COL_TYPE:
            return "note" if mapping.is_note else "cc"
        if column == COL_CHANNEL:
            return mapping.channel
        if column == COL_NUMBER:
            return mapping.number
        if column == COL_MIN:
            return _range_sort_value(mapping, "min")
        if column == COL_MAX:
            return _range_sort_value(mapping, "max")
        return map_mode_label(mapping.map_mode).casefold()

    def _restore_selection(self, mapping_indexes: set[int]) -> None:
        if not mapping_indexes:
            return
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        model = self.table.model()
        selection = self.table.selectionModel()
        for row in range(self.table.rowCount()):
            if self._mapping_index(row) in mapping_indexes:
                selection.select(model.index(row, 0), flags)

    def _update_history_actions(self) -> None:
        self.undo_action.setEnabled(bool(self._undo))
        self.redo_action.setEnabled(bool(self._redo))

    def _update_title(self) -> None:
        if self.source_path is None:
            self.setWindowTitle("ALS MIDI Remapper")
            return
        star = "*" if self.live_set and self.live_set.is_dirty else ""
        self.setWindowTitle(f"{star}ALS MIDI Remapper — {self.source_path.name}")
