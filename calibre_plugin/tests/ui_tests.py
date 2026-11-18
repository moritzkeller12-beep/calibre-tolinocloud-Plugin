"""Was der Nutzer in der Oberflaeche ausloest: Spalten, Auswahl, Markierung.

Alle Tests laufen mit echtem PyQt6 (offscreen); Doppel gibt es nur fuer
modale Dialoge und die Calibre-Datenbankgrenze.
"""
import os
import shutil
import tempfile
import types
import unittest
from unittest.mock import patch

from ..comparison import compare_inventory
from ..errors import TolinoError
from ..run_context import RunContext
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
        written = self.ui.sync_results.write_reading_updates(
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
        self.assertTrue(self.ui.dialogs.QMessageBox.calls)

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
                         self.ui.dialogs.QMessageBox.calls[-1][2])

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


class DownloadSaveTests(unittest.TestCase):
    """Eine Cloud-Antwort wird nur als echte EPUB-/PDF-Datei gespeichert."""

    def setUp(self):
        self.ui = ui_module_for(self, qt_stub=real_qt_stub())
        self.tmp = tempfile.mkdtemp(prefix="download-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def save(self, name, data):
        path = os.path.join(self.tmp, name)
        dialog = self.ui.dialogs.InventoryDialog(comparison_rows())
        dialog._download_path = path
        messages = []
        with patch("calibre_plugin.ui_dialogs.info_dialog",
                   lambda *args, **kwargs: messages.append(("info", args))), \
                patch("calibre_plugin.ui_dialogs.error_dialog",
                      lambda *args, **kwargs: messages.append(("error", args))):
            dialog._download_finished(data)
        return path, messages

    def test_an_epub_is_saved_under_the_chosen_name(self):
        path, messages = self.save("buch.epub", b"PK\x03\x04EPUB-DATEN")
        with open(path, "rb") as handle:
            self.assertEqual(b"PK\x03\x04EPUB-DATEN", handle.read())
        self.assertEqual("info", messages[0][0])
        self.assertIn("buch.epub", messages[0][1][2])

    def test_a_pdf_is_saved_as_pdf_even_when_epub_was_chosen(self):
        path, messages = self.save("buch.epub", b"%PDF-1.7")
        self.assertFalse(os.path.exists(path))
        with open(path[:-5] + ".pdf", "rb") as handle:
            self.assertEqual(b"%PDF-1.7", handle.read())
        self.assertEqual("info", messages[0][0])

    def test_an_existing_adjusted_target_needs_confirmation(self):
        target = os.path.join(self.tmp, "buch.pdf")
        with open(target, "wb") as handle:
            handle.write(b"ALT")
        box = self.ui.dialogs.QMessageBox
        original = box.__dict__["question"]
        box.question = staticmethod(
            lambda *args, **kwargs: box.StandardButton.No)
        self.addCleanup(setattr, box, "question", original)
        _path, messages = self.save("buch.epub", b"%PDF-1.7")
        with open(target, "rb") as handle:
            self.assertEqual(b"ALT", handle.read())
        self.assertEqual("Nicht gespeichert", messages[0][1][1])

    def test_html_and_empty_bodies_are_not_saved(self):
        for label, data in (("html", b"<html>Fehler</html>"),
                            ("leer", b"")):
            with self.subTest(label):
                path, messages = self.save("%s.epub" % label, data)
                self.assertFalse(os.path.exists(path))
                self.assertEqual("error", messages[0][0])


class ConfirmationTests(unittest.TestCase):
    """Vor Löschen und Ersetzen nennt das Plugin die Folgen."""

    def setUp(self):
        self.ui = ui_module_for(self, qt_stub=real_qt_stub())
        self.parts = self.ui.dashboard_parts
        self.box = self.parts.QMessageBox
        self.box.calls = []

    def ask(self, answer):
        """Antwort des modalen Dialogs festlegen und die Aufrufe sammeln."""
        cls = self.box
        cls.calls = []
        original = cls.__dict__["question"]

        def question(_cls, *args):
            cls.calls.append(("question",) + args)
            return answer

        cls.question = classmethod(question)
        self.addCleanup(setattr, cls, "question", original)

    def question_text(self, index=0):
        return self.box.calls[index][3]

    def test_the_deletion_confirmation_names_the_books(self):
        self.ask(self.box.No)
        self.assertFalse(self.parts.confirm_deletions(
            None, [(UUID, "d1")], {"d1": "Der Schwarm"}))
        text = self.question_text()
        self.assertIn("Der Schwarm", text)
        self.assertIn("in Calibre bleibt", text)

    def test_replacing_more_than_twenty_books_names_the_rest(self):
        def job(label):
            return types.SimpleNamespace(label=lambda: label, old_id="d1")

        self.ask(self.box.Yes)
        jobs = [job("Buch %d" % number) for number in range(22)]
        self.assertTrue(self.parts.confirm_replacements(None, jobs))
        text = self.question_text()
        self.assertIn("Lesestatus", text)
        self.assertIn("Lesefortschritt", text)
        self.assertIn("Sammlungen", text)
        self.assertIn("und 2 weitere", text)
        self.assertNotIn("Buch 20", text)

    def test_no_returns_false_and_yes_returns_true(self):
        job = types.SimpleNamespace(label=lambda: "Der Schwarm", old_id="d1")
        for answer, expected in ((self.box.No, False),
                                 (self.box.Yes, True)):
            with self.subTest(answer=answer):
                self.ask(answer)
                self.assertEqual(
                    expected, self.parts.confirm_replacements(None, [job]))

    def test_the_deletion_confirmation_accepts_with_yes(self):
        self.ask(self.box.Yes)
        self.assertTrue(self.parts.confirm_deletions(
            None, [(UUID, "d1"), ("zweite", "d2")],
            {"d1": "Der Schwarm", "d2": "Zweites Buch"}))
        text = self.question_text()
        self.assertIn("2 Bücher", text)
        self.assertIn("Zweites Buch", text)

    def sync_stub(self):
        """Dashboard-Doppel mit genau den Widgets, die der Start anfasst."""
        stub = dashboard_stub()
        stub.status_text = ""
        stub.plan_text = ""
        stub.failure = ""
        stub.status = types.SimpleNamespace(
            setText=lambda text: setattr(stub, "status_text", text))
        stub.progress = types.SimpleNamespace(
            setRange=lambda *args: None, setValue=lambda *args: None)
        stub.progress_label = types.SimpleNamespace(
            setText=lambda text: setattr(stub, "plan_text", text))
        stub.update_status = lambda: None
        stub.progress_changed = lambda *args: None
        stub.sync_completed = lambda *args: None
        stub.sync_failed = lambda message: setattr(stub, "failure", message)
        stub._start_sync_worker = (
            self.ui.dashboard.SyncDashboard._start_sync_worker.__get__(stub))
        return stub

    def start_sync(self, stub, preparation):
        """Echten Startpfad ausfuehren; nur der Netzzugang ist blockiert."""
        common = self.ui.common
        threads, workers = list(common._THREADS), list(common._WORKERS)

        def restore():
            common._THREADS[:] = threads
            common._WORKERS[:] = workers

        self.addCleanup(restore)
        context = RunContext.from_values(
            {"account_name": "A", "preferred_formats": ["EPUB"]})
        with patch("calibre_plugin.ui_workers.TOKEN_VAULT.login",
                   side_effect=TolinoError("offline")):
            stub._start_sync_worker(context, preparation, {})
        return stub

    def test_the_confirmed_deletions_reach_the_sync_worker(self):
        stub = self.start_sync(self.sync_stub(), {
            "jobs": [], "removals": [(UUID, "d1")],
            "meta_updates": [], "series_map": {}, "skipped_uploads": []})
        self.assertIsNotNone(stub.thread)
        self.assertEqual([(UUID, "d1")], stub.worker.removals)
        self.assertEqual([], stub.worker.jobs)
        self.assertIn("1 Löschung", stub.plan_text)

    def test_an_accepted_replacement_confirmation_starts_the_same_jobs(self):
        self.ask(self.box.Yes)

        def job(label, old_id=None):
            return types.SimpleNamespace(label=lambda: label, old_id=old_id)

        jobs = [job("Der Schwarm", "d1"), job("Nur in Calibre")]
        stub = self.start_sync(self.sync_stub(), {
            "jobs": jobs, "removals": [], "meta_updates": [],
            "series_map": {}, "skipped_uploads": []})
        self.assertIsNotNone(stub.thread)
        self.assertEqual(jobs, stub.worker.jobs)
        # Der Worker laeuft wirklich an (hier scheitert nur der Netzzugang).
        self.assertTrue(stub.failure)
        text = self.question_text()
        self.assertIn("Der Schwarm", text)
        self.assertNotIn("Nur in Calibre", text)

    def test_a_cancelled_replacement_confirmation_starts_no_worker(self):
        self.ask(self.box.No)

        def job(label, old_id=None):
            return types.SimpleNamespace(label=lambda: label, old_id=old_id)

        context = RunContext.from_values(
            {"account_name": "A", "preferred_formats": ["EPUB"]})
        stub = dashboard_stub()
        stub.status_text = ""
        stub.status = types.SimpleNamespace(
            setText=lambda text: setattr(stub, "status_text", text))
        stub._start_sync_worker = (
            self.ui.dashboard.SyncDashboard._start_sync_worker.__get__(stub))
        stub._start_sync_worker(context, {
            "jobs": [job("Der Schwarm", "d1"), job("Ohne Kopie")],
            "removals": [], "meta_updates": [], "skipped_uploads": []}, {})
        self.assertIn("Abgebrochen", stub.status_text)
        # Kein Worker, kein Thread: die bestätigte Auswahl bleibt ungenutzt.
        self.assertIsNone(stub.thread)
        self.assertIsNone(stub.worker)
        text = self.question_text()
        self.assertIn("Der Schwarm", text)
        self.assertNotIn("Ohne Kopie", text)


if __name__ == "__main__":
    unittest.main()
