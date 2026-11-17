"""Was der Nutzer in der Oberflaeche ausloest: Spalten, Auswahl, Markierung.

Alle Tests laufen mit echtem PyQt6 (offscreen); Doppel gibt es nur fuer
modale Dialoge und die Calibre-Datenbankgrenze.
"""
import unittest
from unittest.mock import patch

from ..comparison import compare_inventory
from ._support import (CalibreDatabase, dashboard_stub, real_qt_stub,
                       ui_module_for)

UUID = "11111111-1111-4111-8111-111111111111"


def comparison_rows(**kwargs):
    """Echte Vergleichszeilen: ein Buch, das in Calibre und tolino liegt."""
    book = {"uuid": UUID, "title": "Der Schwarm",
            "authors": "Frank Schätzing", "isbn": "9780306406157",
            "formats": ["EPUB"]}
    cloud = {"tolino_id": "d1", "title": "Der Schwarm",
             "authors": "Frank Schätzing", "isbn": "9780306406157"}
    kwargs.setdefault("read_ids", {"d1"})
    return compare_inventory({1: book}, {UUID: {"tolino_id": "d1"}},
                             [cloud], ["EPUB"], **kwargs)


def select_rows(table, rows):
    """Zeilen der echten Tabelle markieren (wie Strg-/Shift-Klick)."""
    from PyQt6.QtCore import QItemSelectionModel
    model = table.selectionModel()
    model.clearSelection()
    flags = (QItemSelectionModel.SelectionFlag.Select
             | QItemSelectionModel.SelectionFlag.Rows)
    for row in rows:
        model.select(table.model().index(row, 0), flags)


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


class InventoryDialogTests(unittest.TestCase):
    """Vergleichsfenster: Aufbau, Auswahl je Zeile, Gelesen-Markierung."""

    def setUp(self):
        self.ui = ui_module_for(self, qt_stub=real_qt_stub())

    def _dialog(self, rows=None):
        return self.ui.dialogs.InventoryDialog(
            comparison_rows() if rows is None else rows)

    def test_the_dialog_shows_one_row_per_book(self):
        dialog = self._dialog()
        self.assertEqual(1, dialog.table.rowCount())
        self.assertEqual("Der Schwarm",
                         dialog.table.item(0, dialog.TITLE_COLUMN).text())
        self.assertEqual("Frank Schätzing",
                         dialog.table.item(0, dialog.AUTHORS_COLUMN).text())
        self.assertEqual(0, dialog._selection_counts()["work"])

    def test_marked_rows_can_be_set_to_unread(self):
        dialog = self._dialog()
        select_rows(dialog.table, [0])
        dialog.set_column_for_marked_rows("read", False,
                                         "sind bereits ungelesen")
        self.assertEqual([("d1", 1, False, UUID)],
                         dialog.selected_read_changes())
        self.assertEqual(1, dialog._selection_counts()["unread"])
        self.assertIn("1 von 1", dialog.action_feedback.text())

    def test_clearing_the_selection_leaves_no_work(self):
        dialog = self._dialog()
        select_rows(dialog.table, [0])
        dialog.set_column_for_marked_rows("read", False,
                                         "sind bereits ungelesen")
        dialog.clear_all_selection()
        self.assertEqual([], dialog.selected_read_changes())
        self.assertEqual(0, dialog._selection_counts()["work"])

    def test_without_a_marked_row_the_dialog_explains_why(self):
        dialog = self._dialog()
        dialog.set_column_for_marked_rows("read", False,
                                         "sind bereits ungelesen")
        self.assertEqual([], dialog.selected_read_changes())
        self.assertTrue(self.ui.common.QMessageBox.calls)

    def test_a_book_without_cloud_copy_cannot_be_marked(self):
        rows = comparison_rows(read_ids=None)
        rows[0]["tolino_id"] = ""
        dialog = self._dialog(rows)
        select_rows(dialog.table, [0])
        dialog.set_column_for_marked_rows("read", True,
                                         "sind schon gelesen")
        self.assertEqual([], dialog.selected_read_changes())


if __name__ == "__main__":
    unittest.main()
