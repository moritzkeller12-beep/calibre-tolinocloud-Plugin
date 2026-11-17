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


def click_cell(table, row, column):
    """Echten Linksklick der Maus auf eine Zelle der Tabelle schicken."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    table.scrollToItem(table.item(row, column))
    rect = table.visualRect(table.model().index(row, column))
    QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton,
                     pos=rect.center())


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

    def test_a_click_on_a_choice_cell_matches_the_menu_action(self):
        clicked = self._dialog()
        clicked.show()
        click_cell(clicked.table, 0, clicked.ACTION_KEYS.index("read"))
        self.assertEqual([("d1", 1, False, UUID)],
                         clicked.selected_read_changes())
        self.assertEqual(1, clicked._selection_counts()["unread"])

        chosen = self._dialog()
        select_rows(chosen.table, [0])
        chosen.set_column_for_marked_rows("read", False,
                                          "sind bereits ungelesen")
        self.assertEqual(chosen.selected_read_changes(),
                         clicked.selected_read_changes())

    def test_a_second_click_takes_the_choice_back(self):
        dialog = self._dialog()
        dialog.show()
        column = dialog.ACTION_KEYS.index("read")
        click_cell(dialog.table, 0, column)
        click_cell(dialog.table, 0, column)
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


SECOND = "22222222-2222-4222-8222-222222222222"


def two_books():
    """Zwei eindeutig zugeordnete Buecher fuer Mehrfachauswahlen."""
    first = {"uuid": UUID, "title": "Der Schwarm",
             "authors": "Frank Schätzing", "isbn": "9780306406157",
             "formats": ["EPUB"]}
    second = {"uuid": SECOND, "title": "Zweites Buch",
              "authors": "Autor Zwei", "isbn": "", "formats": ["EPUB"]}
    cloud = [{"tolino_id": "d1", "title": "Der Schwarm",
              "authors": "Frank Schätzing", "isbn": "9780306406157"},
             {"tolino_id": "d2", "title": "Zweites Buch",
              "authors": "Autor Zwei", "isbn": ""}]
    return compare_inventory({1: first, 2: second}, {}, cloud, ["EPUB"])


class DestructiveSelectionTests(unittest.TestCase):
    """Loeschen ist destruktiv: nur ausgewaehlte, konkrete Cloud-Kopien."""

    def setUp(self):
        self.ui = ui_module_for(self, qt_stub=real_qt_stub())

    def dialog(self, rows=None):
        return self.ui.dialogs.InventoryDialog(
            two_books() if rows is None else rows)

    def index_of(self, dialog, tolino_id):
        return next(index for index, row in enumerate(dialog.rows)
                    if row["tolino_id"] == tolino_id)

    def test_only_the_marked_cloud_copy_is_scheduled_for_deletion(self):
        dialog = self.dialog()
        index = self.index_of(dialog, "d1")
        select_rows(dialog.table, [index])
        dialog.delete_marked_from_cloud()
        self.assertEqual([(UUID, "d1", "Der Schwarm")],
                         dialog.selected_cloud_deletions())
        self.assertEqual((False, False),
                         (dialog.rows[index]["upload"],
                          dialog.rows[index]["metadata"]))
        self.assertEqual((1, 1), (dialog._selection_counts()["deletions"],
                                  dialog._selection_counts()["work"]))

    def test_the_delete_mark_can_be_taken_back(self):
        dialog = self.dialog()
        index = self.index_of(dialog, "d1")
        select_rows(dialog.table, [index])
        dialog.delete_marked_from_cloud()
        select_rows(dialog.table, [index])
        dialog.clear_cloud_delete_for_marked_rows()
        self.assertEqual([], dialog.selected_cloud_deletions())
        self.assertEqual(0, dialog._selection_counts()["deletions"])

    def test_a_book_outside_the_cloud_cannot_be_marked_for_deletion(self):
        book = {"uuid": UUID, "title": "Nur lokal",
                "authors": "Frank Schätzing", "isbn": "9780306406157",
                "formats": ["EPUB"]}
        dialog = self.dialog(compare_inventory({1: book}, {}, [], ["EPUB"]))
        select_rows(dialog.table, [0])
        dialog.delete_marked_from_cloud()
        self.assertEqual([], dialog.selected_cloud_deletions())
        self.assertIsNone(dialog.rows[0].get("delete_cloud"))
        self.assertEqual("Nichts zu löschen",
                         self.ui.common.QMessageBox.calls[-1][2])

    def test_an_ignored_row_offers_no_action(self):
        book = {"uuid": UUID, "title": "Der Schwarm",
                "authors": "Frank Schätzing", "isbn": "9780306406157",
                "formats": ["EPUB"], "ignored": True}
        cloud = {"tolino_id": "d1", "title": "Der Schwarm",
                 "authors": "Frank Schätzing", "isbn": "9780306406157"}
        dialog = self.dialog(compare_inventory({1: book}, {}, [cloud],
                                               ["EPUB"]))
        caps = self.ui.dialogs.row_capabilities(dialog.rows[0])
        self.assertEqual(
            {"upload": False, "metadata": False, "read": False,
             "delete": False},
            {key: caps[key] for key in ("upload", "metadata", "read",
                                        "delete")})

    def test_clearing_a_multiple_selection_leaves_no_hidden_action(self):
        dialog = self.dialog()
        select_rows(dialog.table, [0, 1])
        dialog.delete_marked_from_cloud()
        self.assertEqual({"d1", "d2"},
                         {entry[1] for entry in dialog.selected_cloud_deletions()})
        dialog.clear_all_selection()
        self.assertEqual([], dialog.selected_cloud_deletions())
        self.assertEqual(0, dialog._selection_counts()["work"])
        self.assertTrue(all(not row.get("upload") and not row.get("metadata")
                            and not row.get("delete_cloud")
                            for row in dialog.rows))


if __name__ == "__main__":
    unittest.main()
