"""Mappings from the MapTesting Live 12.4 set, and a surgical save."""

from __future__ import annotations

import gzip
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mappings import LiveSet, amplitude_from_db, commit_range_text, db_from_amplitude, format_db, note_name, range_display

ALS = Path(__file__).resolve().parents[1] / "MapTesting Project" / "MapTesting.als"
KAOS = Path(__file__).resolve().parents[1] / "MapTesting Project" / "keyworks kaos.als"
VALUE_RE = re.compile(r'<([A-Za-z0-9_.]+) Value="([^"]*)"')


class MapTestingTests(unittest.TestCase):
    def test_four_mappings_match_the_set(self) -> None:
        live = LiveSet.load(ALS)
        self.assertEqual(live.creator, "Ableton Live 12.4")
        self.assertEqual(
            [(mapping.parameter, mapping.number) for mapping in live.mappings],
            [("Track Activator", 60), ("Pan", 74), ("Volume", 73), ("Gain", 75)],
        )

        activator, pan, volume, gain = live.mappings

        self.assertEqual(activator.track, "MapTest")
        self.assertEqual(activator.device, "")
        self.assertTrue(activator.is_note)
        self.assertEqual(activator.channel, 4)
        self.assertEqual(activator.number, 60)
        self.assertIsNone(activator.range_min)
        self.assertEqual(activator.range_unit, "none")
        self.assertEqual(activator.map_mode, 0)

        self.assertEqual(pan.device, "")
        self.assertFalse(pan.is_note)
        self.assertEqual(pan.channel, 1)
        self.assertEqual(pan.range_min, "-1")
        self.assertEqual(pan.range_max, "1")
        self.assertEqual(pan.range_unit, "raw")

        self.assertFalse(volume.is_note)
        self.assertEqual(volume.channel, 1)
        self.assertEqual(volume.number, 73)
        self.assertEqual(volume.range_unit, "db")
        self.assertAlmostEqual(db_from_amplitude(volume.range_min or ""), -70, places=2)
        self.assertAlmostEqual(db_from_amplitude(volume.range_max or ""), 6, places=2)

        self.assertEqual(gain.track, "MapTest")
        self.assertEqual(gain.device, "Utility")
        self.assertFalse(gain.is_note)
        self.assertEqual(gain.channel, 1)
        self.assertEqual(gain.number, 75)
        self.assertEqual(gain.range_min, "0.5011872053")
        self.assertEqual(gain.range_max, "1.99526238")
        self.assertEqual(gain.range_unit, "db")
        self.assertAlmostEqual(db_from_amplitude(gain.range_min), -6, places=2)
        self.assertAlmostEqual(db_from_amplitude(gain.range_max), 6, places=2)
        self.assertEqual(note_name(activator.number), "C3")

    def test_reentering_displayed_db_keeps_the_stored_amplitude(self) -> None:
        live = LiveSet.load(ALS)
        gain = next(mapping for mapping in live.mappings if mapping.parameter == "Gain")
        gain.range_min = commit_range_text(gain, "min", format_db(gain.range_min or ""))
        gain.range_max = commit_range_text(gain, "max", format_db(gain.range_max or ""))
        self.assertEqual(gain.range_min, "0.5011872053")
        self.assertEqual(gain.range_max, "1.99526238")
        self.assertEqual(live.render(), live.xml)

    def test_no_edit_round_trip_keeps_the_xml(self) -> None:
        live = LiveSet.load(ALS)
        self.assertEqual(live.render(), live.xml)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "MapTesting remapped.als"
            live.save(dest)
            raw = dest.read_bytes()
            self.assertEqual(raw[:2], b"\x1f\x8b")
            self.assertEqual(raw[3] & 0x08, 0)
            self.assertEqual(raw[4:8], b"\x00\x00\x00\x00")
            self.assertEqual(gzip.decompress(raw).decode("utf-8"), live.xml)

    def test_loads_uncompressed_xml(self) -> None:
        xml_text = LiveSet.load(ALS).xml
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "raw.als"
            dest.write_bytes(xml_text.encode("utf-8"))
            live = LiveSet.load(dest)
        self.assertEqual(len(live.mappings), 4)
        self.assertEqual(live.creator, "Ableton Live 12.4")

    def test_edit_changes_only_volume_and_gain_range(self) -> None:
        live = LiveSet.load(ALS)
        original = live.xml
        volume = next(mapping for mapping in live.mappings if mapping.parameter == "Volume")
        gain = next(mapping for mapping in live.mappings if mapping.parameter == "Gain")
        volume.channel = 2
        volume.number = 10
        gain.range_min = amplitude_from_db(0)
        gain.range_max = amplitude_from_db(3)

        updated = live.render()
        again = LiveSet.from_xml(updated)
        volume_again = next(mapping for mapping in again.mappings if mapping.parameter == "Volume")
        gain_again = next(mapping for mapping in again.mappings if mapping.parameter == "Gain")
        note = next(mapping for mapping in again.mappings if mapping.is_note)
        pan = next(mapping for mapping in again.mappings if mapping.parameter == "Pan")

        self.assertEqual(volume_again.channel, 2)
        self.assertEqual(volume_again.number, 10)
        self.assertFalse(volume_again.is_note)
        self.assertAlmostEqual(db_from_amplitude(gain_again.range_min or ""), 0, places=3)
        self.assertAlmostEqual(db_from_amplitude(gain_again.range_max or ""), 3, places=3)
        self.assertEqual(note.channel, 4)
        self.assertEqual(note.number, 60)
        self.assertTrue(note.is_note)
        self.assertEqual(pan.number, 74)
        self.assertEqual(pan.range_min, "-1")
        self.assertEqual(pan.range_max, "1")

        original_lines = original.splitlines()
        updated_lines = updated.splitlines()
        self.assertEqual(len(original_lines), len(updated_lines))
        diffs = [(old, new) for old, new in zip(original_lines, updated_lines) if old != new]
        expected = {
            ("Channel", "0", "1"),
            ("NoteOrController", "73", "10"),
            ("Min", "0.5011872053", amplitude_from_db(0)),
            ("Max", "1.99526238", amplitude_from_db(3)),
        }
        found = set()
        for old, new in diffs:
            old_tag, old_value = VALUE_RE.search(old).groups()  # type: ignore[union-attr]
            new_tag, new_value = VALUE_RE.search(new).groups()  # type: ignore[union-attr]
            self.assertEqual(old_tag, new_tag)
            self.assertEqual(old.replace(f'Value="{old_value}"', f'Value="{new_value}"', 1), new)
            found.add((old_tag, old_value, new_value))
        self.assertEqual(found, expected)
        self.assertEqual(len(diffs), 4)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "edited.als"
            live.save(dest)
            self.assertEqual(gzip.decompress(dest.read_bytes()).decode("utf-8"), updated)

    def test_map_mode_edit_changes_only_that_attribute(self) -> None:
        live = LiveSet.load(ALS)
        original = live.xml
        volume = next(mapping for mapping in live.mappings if mapping.parameter == "Volume")
        volume.map_mode = 5
        updated = live.render()
        again = LiveSet.from_xml(updated)
        volume_again = next(mapping for mapping in again.mappings if mapping.parameter == "Volume")
        self.assertEqual(volume_again.map_mode, 5)
        self.assertTrue(all(mapping.map_mode == 0 for mapping in again.mappings if mapping.parameter != "Volume"))

        original_lines = original.splitlines()
        updated_lines = updated.splitlines()
        self.assertEqual(len(original_lines), len(updated_lines))
        diffs = [(old, new) for old, new in zip(original_lines, updated_lines) if old != new]
        self.assertEqual(len(diffs), 1)
        old, new = diffs[0]
        self.assertIn('ControllerMapMode Value="0"', old)
        self.assertIn('ControllerMapMode Value="5"', new)
        self.assertEqual(old.replace('Value="0"', 'Value="5"', 1), new)

        # Saving the edited set still round-trips through gzip to that same XML.
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "edited.als"
            live.save(dest)
            self.assertEqual(gzip.decompress(dest.read_bytes()).decode("utf-8"), updated)


class KeyworksKaosTests(unittest.TestCase):
    def test_lists_controller_mappings_and_silent_gain(self) -> None:
        live = LiveSet.load(KAOS)
        self.assertEqual(len(live.mappings), 60)
        self.assertTrue(all(1 <= mapping.channel <= 16 for mapping in live.mappings))
        self.assertTrue(all(not mapping.parameter.startswith("MacroControls.") for mapping in live.mappings))
        self.assertTrue(all("MxDevice" not in mapping.device for mapping in live.mappings))
        self.assertNotIn("Timeable", {mapping.parameter for mapping in live.mappings})
        self.assertEqual(live.render(), live.xml)

        silent = [
            mapping
            for mapping in live.mappings
            if mapping.parameter == "Gain" and mapping.range_min == "0"
        ]
        self.assertEqual(len(silent), 2)
        self.assertEqual(format_db("0"), "-inf dB")
        self.assertEqual(range_display(silent[0], "min"), "-inf dB")
        self.assertEqual(commit_range_text(silent[0], "min", "-inf dB"), "0")

        silent[0].channel = 2
        updated = live.render()
        self.assertEqual(updated.count('Channel Value="16"'), live.xml.count('Channel Value="16"'))
        self.assertNotEqual(updated, live.xml)

    def test_lists_scene_rack_and_max_mappings(self) -> None:
        live = LiveSet.load(KAOS)
        by_parameter = {}
        for mapping in live.mappings:
            by_parameter.setdefault(mapping.parameter, []).append(mapping)

        scene_up = by_parameter["Scene Up"][0]
        self.assertEqual(scene_up.track, "Main")
        self.assertEqual(scene_up.device, "")
        self.assertFalse(scene_up.is_note)
        self.assertEqual(scene_up.channel, 1)
        self.assertEqual(scene_up.number, 31)
        self.assertIsNone(scene_up.range_min)

        scene_launch = by_parameter["Scene Launch"][0]
        self.assertEqual(scene_launch.track, "Main")
        self.assertFalse(scene_launch.is_note)
        self.assertEqual(scene_launch.number, 115)
        self.assertEqual(by_parameter["Scene Down"][0].number, 86)
        scroll = by_parameter["Scroll Scene"][0]
        self.assertEqual(scroll.number, 114)
        self.assertEqual(scroll.map_mode, 4)

        self.assertEqual(len(by_parameter["Randomize Macros"]), 3)
        self.assertEqual(len(by_parameter["Recall Variation"]), 1)
        variations = [mapping for mapping in live.mappings if mapping.parameter.startswith("Variation ")]
        self.assertEqual(len(variations), 13)
        self.assertIn("Select Variation", by_parameter)
        randomize = next(mapping for mapping in by_parameter["Randomize Macros"] if mapping.number == 61)
        self.assertEqual(randomize.track, "Modular")
        self.assertTrue(randomize.is_note)
        self.assertIn("Rack", randomize.device)

        probability = next(mapping for mapping in live.mappings if mapping.parameter == "Probability")
        self.assertNotIn("MxDevice", probability.device)
        self.assertTrue(probability.device)
        self.assertIn("Macro 1", by_parameter)

        scene_up.channel = 2
        updated = live.render()
        again = LiveSet.from_xml(updated)
        restored = next(mapping for mapping in again.mappings if mapping.parameter == "Scene Up")
        self.assertEqual(restored.channel, 2)
        self.assertEqual(restored.number, 31)
        self.assertEqual(
            next(mapping for mapping in again.mappings if mapping.parameter == "Scene Launch").number,
            115,
        )


class UiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            raise unittest.SkipTest("PySide6 is not installed")
        cls.app = QApplication.instance() or QApplication([])

    def test_table_lists_the_four_mappings(self) -> None:
        from PySide6.QtWidgets import QComboBox

        from ui import COL_CHANNEL, COL_DEVICE, COL_MAX, COL_MIN, COL_MODE, COL_NUMBER, COL_PARAM, MainWindow

        window = MainWindow()
        window.load_path(ALS)
        self.assertEqual(window.table.rowCount(), 4)
        self.assertEqual(window.table.item(0, COL_PARAM).text(), "Track Activator")
        self.assertEqual(window.table.item(0, COL_CHANNEL).text(), "4")
        self.assertEqual(window.table.item(0, COL_NUMBER).text(), "60 (C3)")
        self.assertEqual(window.table.item(2, COL_PARAM).text(), "Volume")
        self.assertEqual(window.table.item(2, COL_CHANNEL).text(), "1")
        self.assertEqual(window.table.item(3, COL_DEVICE).text(), "Utility")
        self.assertIn("dB", window.table.item(3, COL_MIN).text())
        self.assertIn("-6.00", window.table.item(3, COL_MIN).text())
        self.assertIn("6.00", window.table.item(3, COL_MAX).text())
        self.assertIn("4 mappings", window.statusBar().currentMessage())
        mode = window.table.cellWidget(2, COL_MODE)
        self.assertIsInstance(mode, QComboBox)
        self.assertEqual(mode.currentData(), 0)
        mode.setCurrentIndex(mode.findData(5))
        self.assertEqual(window.live_set.mappings[2].map_mode, 5)
        self.assertEqual(mode.currentText(), "Absolute 14-bit")
        self.assertEqual(mode.styleSheet(), "")
        self.assertGreater(window.table.item(2, COL_MODE).background().color().alpha(), 0)

        window.filter.setText("utility")
        self.assertTrue(window.table.isRowHidden(0))
        self.assertFalse(window.table.isRowHidden(3))
        window.filter.clear()

        updated = window.apply_spec(
            {
                "mode": "match",
                "from_channel": 1,
                "from_number": 73,
                "to_channel": 2,
                "to_number": 10,
            }
        )
        self.assertEqual(updated, 1)
        self.assertEqual(window.live_set.mappings[2].channel, 2)
        self.assertEqual(window.live_set.mappings[2].number, 10)
        self.assertEqual(window.table.item(2, COL_CHANNEL).text(), "2")
        self.assertEqual(window.table.item(2, COL_NUMBER).text(), "10")
        self.assertEqual(window.live_set.mappings[1].number, 74)

        window.table.selectRow(1)
        window.apply_spec({"mode": "selected", "channel": 8, "number": None})
        self.assertEqual(window.live_set.mappings[1].channel, 8)
        self.assertEqual(window.live_set.mappings[1].number, 74)
        for mapping in window.live_set.mappings:
            mapping.capture_original()
        window.close()

    def test_remap_every_row_on_a_channel(self) -> None:
        from ui import COL_MIN, MainWindow

        window = MainWindow()
        window.load_path(ALS)
        updated = window.apply_spec({"mode": "channel", "from_channel": 1, "to_channel": 3})
        self.assertEqual(updated, 3)
        by_number = {mapping.number: mapping for mapping in window.live_set.mappings}
        self.assertEqual(by_number[73].channel, 3)
        self.assertEqual(by_number[74].channel, 3)
        self.assertEqual(by_number[75].channel, 3)
        self.assertFalse(by_number[73].is_note)
        activator = next(mapping for mapping in window.live_set.mappings if mapping.is_note)
        self.assertEqual(activator.channel, 4)
        self.assertEqual(activator.number, 60)
        from PySide6.QtCore import Qt

        from ui import COL_CHANNEL, COL_NUMBER

        volume_row = next(
            row
            for row in range(window.table.rowCount())
            if window.table.item(row, COL_NUMBER).text() == "73"
        )
        self.assertGreater(window.table.item(volume_row, COL_CHANNEL).background().color().alpha(), 0)
        self.assertEqual(
            window.table.item(volume_row, COL_NUMBER).background().style(),
            Qt.BrushStyle.NoBrush,
        )
        for mapping in window.live_set.mappings:
            mapping.capture_original()
        window.load_path(KAOS)
        self.assertEqual(window.table.rowCount(), 60)
        self.assertTrue(
            any(window.table.item(row, COL_MIN).text() == "-inf dB" for row in range(window.table.rowCount()))
        )
        for mapping in window.live_set.mappings:
            mapping.capture_original()
        window.close()

    def test_undo_and_redo_restore_mapping_edits(self) -> None:
        from ui import COL_MODE, MainWindow

        window = MainWindow()
        window.load_path(ALS)
        self.assertFalse(window.undo_action.isEnabled())
        updated = window.apply_spec({"mode": "channel", "from_channel": 1, "to_channel": 3})
        self.assertEqual(updated, 3)
        mode = window.table.cellWidget(2, COL_MODE)
        mode.setCurrentIndex(mode.findData(5))
        self.assertEqual(window.live_set.mappings[2].map_mode, 5)

        window.undo()
        self.assertEqual(window.live_set.mappings[2].map_mode, 0)
        self.assertEqual(window.live_set.mappings[2].channel, 3)
        window.undo()
        channels = {mapping.number: mapping.channel for mapping in window.live_set.mappings}
        self.assertEqual(channels[73], 1)
        self.assertEqual(channels[74], 1)
        self.assertEqual(channels[75], 1)
        self.assertFalse(window.undo_action.isEnabled())

        window.redo()
        channels = {mapping.number: mapping.channel for mapping in window.live_set.mappings}
        self.assertEqual(channels[73], 3)
        self.assertEqual(window.live_set.mappings[2].map_mode, 0)
        for mapping in window.live_set.mappings:
            mapping.capture_original()
        window.close()

    def test_column_sort_reorders_the_view_only(self) -> None:
        from ui import COL_CHANNEL, COL_NUMBER, COL_PARAM, MainWindow

        window = MainWindow()
        window.load_path(ALS)
        original = [mapping.parameter for mapping in window.live_set.mappings]
        header = window.table.horizontalHeader()

        header.sectionClicked.emit(COL_CHANNEL)
        self.assertEqual(
            [window.table.item(row, COL_PARAM).text() for row in range(window.table.rowCount())],
            ["Pan", "Volume", "Gain", "Track Activator"],
        )
        self.assertEqual([mapping.parameter for mapping in window.live_set.mappings], original)
        self.assertEqual(window.live_set.render(), window.live_set.xml)
        self.assertFalse(window.live_set.is_dirty)

        window.table.selectRow(0)
        header.sectionClicked.emit(COL_CHANNEL)
        self.assertEqual(
            [window.table.item(row, COL_PARAM).text() for row in range(window.table.rowCount())],
            ["Track Activator", "Pan", "Volume", "Gain"],
        )
        selected = window._selected_rows()
        self.assertEqual(
            [window.table.item(row, COL_PARAM).text() for row in selected],
            ["Pan"],
        )

        header.sectionClicked.emit(COL_NUMBER)
        self.assertEqual(
            [window.table.item(row, COL_PARAM).text() for row in range(window.table.rowCount())],
            ["Track Activator", "Volume", "Pan", "Gain"],
        )
        volume_row = next(
            row
            for row in range(window.table.rowCount())
            if window.table.item(row, COL_PARAM).text() == "Volume"
        )
        window.table.item(volume_row, COL_CHANNEL).setText("9")
        self.assertEqual(window.live_set.mappings[2].parameter, "Volume")
        self.assertEqual(window.live_set.mappings[2].channel, 9)
        self.assertEqual(window.live_set.mappings[1].channel, 1)
        window.undo()
        self.assertEqual(window.live_set.mappings[2].channel, 1)
        self.assertEqual(window.table.item(volume_row, COL_CHANNEL).text(), "1")
        self.assertEqual(window.live_set.render(), window.live_set.xml)
        for mapping in window.live_set.mappings:
            mapping.capture_original()
        window.close()

    def test_dropdowns_fill_their_cells_after_load_and_sort(self) -> None:
        from PySide6.QtTest import QTest

        from ui import COL_CHANNEL, COL_MODE, COL_TYPE, MainWindow

        window = MainWindow()
        window.show()
        window.load_path(ALS)
        QTest.qWait(50)
        self._assert_choices_fill_cells(window)

        window.table.horizontalHeader().sectionClicked.emit(COL_CHANNEL)
        QTest.qWait(50)
        self._assert_choices_fill_cells(window)
        window.close()

    def _assert_choices_fill_cells(self, window) -> None:
        from ui import COL_MODE, COL_TYPE

        model = window.table.model()
        for row in range(window.table.rowCount()):
            for column in (COL_TYPE, COL_MODE):
                rect = window.table.visualRect(model.index(row, column))
                combo = window.table.cellWidget(row, column)
                self.assertFalse(rect.isEmpty(), column)
                self.assertEqual(combo.geometry().width(), rect.width(), column)
                self.assertEqual(combo.geometry().x(), rect.x(), column)

    def test_wheel_over_a_closed_dropdown_does_not_change_it(self) -> None:
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent

        from ui import MappingComboBox

        combo = MappingComboBox()
        combo.addItem("Absolute", 0)
        combo.addItem("Absolute 14-bit", 5)
        combo.wheelEvent(
            QWheelEvent(
                QPointF(8, 8),
                QPointF(8, 8),
                QPoint(0, 0),
                QPoint(0, 120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.ScrollUpdate,
                False,
            )
        )
        self.assertEqual(combo.currentData(), 0)


if __name__ == "__main__":
    unittest.main()
