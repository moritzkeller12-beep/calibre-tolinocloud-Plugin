"""Was der Nutzer im Dashboard ausloest: Spalten anlegen, Lesewerte sehen."""
import unittest
from unittest.mock import patch

from ._support import (CalibreDatabase, dashboard_stub, real_qt_stub,
                       ui_module_for)


class CreateColumnTests(unittest.TestCase):
    """Ein Knopf legt genau seine Spalte an -- und nennt den Neustart."""

    def setUp(self):
        self.ui = ui_module_for(self, qt_stub=real_qt_stub())

    def _dashboard(self, database):
        cls = self.ui.dashboard.SyncDashboard
        dashboard = dashboard_stub()
        dashboard.gui = type("Gui", (), {"current_db": database})()
        dashboard.create_columns = cls.create_columns.__get__(dashboard)
        return dashboard

    def _create(self, database, label):
        boxes = []
        with patch("calibre_plugin.ui_dashboard_parts.info_dialog",
                   lambda *args, **kwargs: boxes.append(args)):
            self._dashboard(database).create_columns(label)
        return boxes

    def test_one_button_creates_only_its_own_column(self):
        database = CalibreDatabase({"#tolino_id": {"datatype": "text"}})
        boxes = self._create(database, "tolino_read")
        self.assertEqual(["tolino_read"], database.created)
        self.assertIn("#tolino_read", boxes[0][2])
        self.assertIn("Calibre neu starten", boxes[0][2])

    def test_an_existing_column_says_so_and_changes_nothing(self):
        database = CalibreDatabase({"#tolino_read": {"datatype": "bool"}})
        boxes = self._create(database, "tolino_read")
        self.assertEqual([], database.created)
        self.assertIn("schon vorhanden", boxes[0][2])


class ReadingMirrorTests(unittest.TestCase):
    """Lesestatus und Fortschritt sind getrennte Spalten."""

    def setUp(self):
        self.ui = ui_module_for(self, qt_stub=real_qt_stub())

    def test_a_failing_column_does_not_block_the_other_write(self):
        database = CalibreDatabase(
            {"#tolino_read": {"datatype": "bool"},
             "#tolino_progress": {"datatype": "int"}},
            fail={"#tolino_read"})
        dashboard = dashboard_stub(
            gui=type("Gui", (), {"current_db": database})())
        written = self.ui.dashboard._write_reading_updates(
            dashboard, [(1, True)], [(2, 42)], "Test")
        self.assertEqual(1, written)
        self.assertEqual([("#tolino_progress", {2: 42})], database.api.writes)


if __name__ == "__main__":
    unittest.main()
