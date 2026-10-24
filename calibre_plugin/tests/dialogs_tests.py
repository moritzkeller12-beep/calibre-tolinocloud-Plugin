"""Dialoge: Vergleich, Aktionen und Loeschbestaetigung."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from . import ui_base_tests
from ..comparison import compare_inventory
from ..plan import merge_removals
from ._support import (_ui_source_text, start_cdp_stubs, stop_cdp_stubs,
                      ui_module_for)
from unittest.mock import patch


# Modul-Hooks für den Einzellauf; die Gesamtsuite ruft sie zentral auf.
setUpModule = start_cdp_stubs
tearDownModule = stop_cdp_stubs


def _marked_rows(indexes):
    """`selected_table_rows` für die Dauer des with-Blocks ersetzen."""
    return patch("calibre_plugin.ui_dialogs.selected_table_rows",
                 lambda _table: list(indexes))


class _QtDialogTest(unittest.TestCase):
    """Vergleichsfenster mit vollem Qt-Stub; räumt nach jedem Test auf."""

    def _ui(self):
        return ui_module_for(
            self, qt_stub=ui_base_tests.InventoryDialogBuildTests._qt_stub())

    @staticmethod
    def _marked(marked):
        return _marked_rows(marked)

    @staticmethod
    def _rows():
        """Drei Zeilen: gelesen, ungelesen, nur lokal."""
        return [
            {"title": "Gelesen", "authors": "A", "status": "identical",
             "book_id": 1, "tolino_id": "t-1", "read_in_cloud": True},
            {"title": "Ungelesen", "authors": "B", "status": "identical",
             "book_id": 2, "tolino_id": "t-2"},
            {"title": "Nur lokal", "authors": "D",
             "status": "new_in_calibre", "book_id": 3, "tolino_id": ""},
        ]

    @staticmethod
    def _messages(ui_module):
        """QMessageBox.information/warning als Aufzeichnung ersetzen."""
        calls = []

        class MsgBox:
            @staticmethod
            def information(*args, **kwargs):
                calls.append(" ".join(str(arg) for arg in args))

            @staticmethod
            def warning(*args, **kwargs):
                calls.append(" ".join(str(arg) for arg in args))

        ui_module.dialogs.QMessageBox = MsgBox
        return calls


class InventoryDialogSortTests(unittest.TestCase):
    """Spaltenkopf-Klick sortiert den Bestandsvergleich und zieht die
    Auswahl mit (Feldbefund: „das sortieren funktioniert nicht").
    Qt wird ueber den ToolbarIconTests-Stub geladen; die Zell-Widgets
    werden durch einfache Fakes ersetzt."""

    def test_column_click_sorts_and_keeps_checks(self):
        """Ein Klick auf einen Spaltenkopf sortiert die Tabelle neu; die
        Auswahl (Hochladen/Metadaten/Gelesen) bleibt denselben Büchern
        zugeordnet (Feldbefund: „das sortieren funktioniert nicht")."""

        class FakeCheck:
            def __init__(self):
                self._checked = False

            def setChecked(self, value):
                self._checked = bool(value)

            def isChecked(self):
                return self._checked

            def setEnabled(self, value):
                pass

            def setToolTip(self, text):
                pass

            def stateChanged(self):
                return None

        class FakeHeader:
            def setSortIndicatorShown(self, value):
                pass

            def setSortIndicator(self, column, order):
                pass

        class FakeTable:
            def __init__(self):
                self.row_count = -1

            def setRowCount(self, count):
                self.row_count = count

            def setCellWidget(self, row, column, widget):
                pass

            def setItem(self, row, column, item):
                pass

            def resizeColumnsToContents(self):
                pass

            def horizontalHeader(self):
                return FakeHeader()

        class FakeItem:
            def __init__(self, text):
                self.text = text

        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        ui_module.dialogs.QCheckBox = FakeCheck
        ui_module.dialogs.QTableWidgetItem = FakeItem
        try:
            dialog = ui_module.dialogs.InventoryDialog.__new__(
                ui_module.dialogs.InventoryDialog)
            rows = [
                {"upload": False, "metadata": False, "read": False,
                 "title": "Alpha", "authors": "A", "status": "identical",
                 "book_id": 1, "tolino_id": "c1",
                 "explanation": "Titel stimmt mit der Cloud überein."},
                {"upload": True, "metadata": False, "read": False,
                 "title": "Zulu", "authors": "Z", "status": "new_in_calibre",
                 "book_id": 2, "tolino_id": "",
                 "explanation": "Noch nicht in der Cloud – kann hochgeladen werden."},
                {"upload": False, "metadata": True, "read": True,
                 "title": "beta", "authors": "B", "status": "changed",
                 "book_id": 3, "tolino_id": "c3",
                 "explanation": "Nur Metadaten unterscheiden sich."},
                {"upload": True, "metadata": False, "read": False,
                 "title": "alpha", "authors": "M", "status": "new_in_calibre",
                 "book_id": 4, "tolino_id": "",
                 "explanation": "Noch nicht in der Cloud – kann hochgeladen werden."},
            ]
            dialog.rows = rows
            dialog.checks = [FakeCheck() for _ in rows]
            dialog.meta_checks = [FakeCheck() for _ in rows]
            dialog.read_checks = [FakeCheck() for _ in rows]
            for uploads, metadata, read, row in zip(dialog.checks,
                                                    dialog.meta_checks,
                                                    dialog.read_checks, rows):
                uploads.setChecked(row["upload"])
                metadata.setChecked(row["metadata"])
                read.setChecked(row["read"])
            dialog._sort_column = 0
            dialog._sort_descending = False
            dialog.table = FakeTable()

            # Klick auf den Titel (Spalte 5): aufsteigend, die Auswahl
            # bleibt denselben Büchern zugeordnet.
            dialog._sort_by_column(5)
            self.assertEqual(
                ["alpha", "alpha", "beta", "zulu"],
                [row["title"].casefold() for row in dialog.rows])
            upload_titles = {row["title"].casefold()
                             for row, check in zip(dialog.rows, dialog.checks)
                             if check.isChecked()}
            meta_titles = {row["title"].casefold()
                           for row, check in zip(dialog.rows, dialog.meta_checks)
                           if check.isChecked()}
            read_titles = {row["title"].casefold()
                           for row, check in zip(dialog.rows, dialog.read_checks)
                           if check.isChecked()}
            self.assertEqual({"alpha", "zulu"}, upload_titles)
            self.assertEqual({"beta"}, meta_titles)
            self.assertEqual({"beta"}, read_titles)
            # Zweiter Klick auf dieselbe Spalte: absteigend.
            dialog._sort_by_column(5)
            self.assertEqual(
                ["zulu", "beta", "alpha", "alpha"],
                [row["title"].casefold() for row in dialog.rows])
            self.assertEqual(
                {"alpha", "zulu"},
                {row["title"].casefold()
                 for row, check in zip(dialog.rows, dialog.checks)
                 if check.isChecked()})
            # Hochladen-Spalte (0): ausgewählte zuerst, danach Titel.
            dialog._sort_by_column(0)
            first_two = dialog.rows[:2]
            self.assertTrue(all(row["upload"] for row in first_two))
            self.assertEqual(["alpha", "zulu"],
                             [row["title"].casefold() for row in first_two])
            # Metadaten-Spalte (1): nur das Metadaten-Auswahlfeld steht oben.
            dialog._sort_by_column(1)
            self.assertEqual("beta", dialog.rows[0]["title"].casefold())
            # Gelesen-Spalte (2): nur die Lese-Marke steht oben.
            dialog._sort_by_column(2)
            self.assertEqual("beta", dialog.rows[0]["title"].casefold())
            self.assertTrue(dialog.rows[0]["read"])
            self.assertFalse(any(row["read"] for row in dialog.rows[1:]))
            # In Calibre (3): Bücher mit Calibre-Kopie zuerst -- hier haben
            # alle eine, also sortiert der Titel als Zweitschlüssel.
            dialog._sort_by_column(3)
            self.assertTrue(all(row["book_id"] is not None
                                for row in dialog.rows))
            self.assertEqual(
                ["alpha", "alpha", "beta", "zulu"],
                [row["title"].casefold() for row in dialog.rows])
            # In der Cloud (4): Cloud-Kopien zuerst, ohne Cloud-Kopie hinten.
            dialog._sort_by_column(4)
            self.assertEqual(
                ["c1", "c3", "", ""],
                [row["tolino_id"] for row in dialog.rows])
        finally:
            ui_module._restore_stubs_for_tests()

    def test_columns_sort_keys_and_technical_columns_stay_in_sync(self):
        """Spaltenzahl, Sortierschlüssel und technische Spalten müssen
        zusammenpassen – sonst sortiert ein Klick falsch. Die doppelte
        Statusspalte entfällt, die beiden Orts-Spalten stehen wieder
        einzeln da (Feldbefund)."""
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        try:
            dialog_cls = ui_module.dialogs.InventoryDialog
            # 8 sichtbare Spalten + 3 technische.
            self.assertEqual(11, len(dialog_cls.HEADERS))
            visible = [name for index, name in enumerate(dialog_cls.HEADERS)
                       if index not in dialog_cls.TECHNICAL_COLUMNS]
            self.assertEqual(8, len(visible))
            self.assertEqual(["Datei hochladen", "Metadaten aktualisieren",
                              "Gelesen", "In Calibre", "In tolino",
                              "Buchtitel", "Autor(en)",
                              "Hinweis"], visible)
            self.assertEqual(len(dialog_cls.HEADERS),
                             len(dialog_cls._SORT_KEYS))
            # Jede Spalte hat einen eigenen Sortierschlüssel: "Wo" gibt es
            # nicht mehr, die beiden Orts-Spalten sind getrennt sortierbar.
            self.assertNotIn("Wo", dialog_cls.HEADERS)
            for column in dialog_cls.TECHNICAL_COLUMNS:
                self.assertLess(column, len(dialog_cls.HEADERS))
            self.assertEqual((8, 9, 10), dialog_cls.TECHNICAL_COLUMNS)
        finally:
            ui_module._restore_stubs_for_tests()

    def test_note_names_only_the_automatic_actions(self):
        """Metadaten-Updates sind keine Automatik mehr – der Hinweis nennt
        nur, was zusätzlich zur Auswahl passiert."""
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        try:
            dialog = ui_module.dialogs.InventoryDialog.__new__(
                ui_module.dialogs.InventoryDialog)
            dialog.options = {"collections": True, "deletions": True,
                              "automatic": 3}
            note = dialog._automatic_note()
            self.assertIn("Sammlungen in tolino übernommen", note)
            self.assertIn("3 offen", note)
            self.assertNotIn("gelöscht", note)
            dialog.options = {"collections": True, "automatic": 1}
            note = dialog._automatic_note()
            self.assertIn("Sammlungen in tolino übernommen", note)
            self.assertIn("1 offen", note)
            self.assertNotIn("gelöscht", note)
            # Ohne automatische Aktionen ist kein Hinweis nötig.
            dialog.options = {}
            self.assertEqual("", dialog._automatic_note())
        finally:
            ui_module._restore_stubs_for_tests()

    def test_selection_helpers_mark_differences_and_clear(self):
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        try:
            class FakeCheck:
                def __init__(self):
                    self._checked = False

                def setChecked(self, value):
                    self._checked = bool(value)

                def isChecked(self):
                    return self._checked

                def setEnabled(self, value):
                    pass

                def setToolTip(self, text):
                    self.tooltip = text

            ui_module.dialogs.QCheckBox = FakeCheck
            dialog = ui_module.dialogs.InventoryDialog.__new__(
                ui_module.dialogs.InventoryDialog)
            rows = [
                {"status": "identical", "book_id": 1, "tolino_id": "c1"},
                {"status": "new_in_calibre", "book_id": 2, "tolino_id": ""},
                {"status": "changed", "book_id": 3, "tolino_id": "c3",
                 "file_changed": True},
                {"status": "changed", "book_id": 4, "tolino_id": "c4"},
                {"status": "only_tolino", "book_id": None, "tolino_id": "c9"},
            ]
            dialog.rows = rows
            dialog.checks = [FakeCheck() for _ in rows]
            dialog.meta_checks = [FakeCheck() for _ in rows]
            dialog.read_checks = [FakeCheck() for _ in rows]
            dialog.apply_recommended()
            # Neu -> hochladen, geänderte Datei -> hochladen, sonst
            # Metadaten; nichts für unveränderte oder reine Cloud-Bücher.
            self.assertEqual({2, 3}, dialog.selected_upload_ids())
            self.assertEqual({4}, dialog.selected_metadata_ids())
            self.assertEqual([False, True, True, False, False],
                             [check.isChecked() for check in dialog.checks])
            self.assertEqual([False, False, False, True, False],
                             [check.isChecked() for check in dialog.meta_checks])
            # Die Lese-Marke ist nie Teil der Empfehlung.
            self.assertEqual([False] * 5,
                             [check.isChecked() for check in dialog.read_checks])
            self.assertEqual([], dialog.selected_read_targets())
            # Ausdrücklich angehakt zählt sie aber -- nur mit Tolino-ID.
            dialog.read_checks[0].setChecked(True)
            dialog.read_checks[4].setChecked(True)
            self.assertEqual(["c1", "c9"], dialog.selected_read_targets())
            dialog.clear_all_selection()
            self.assertEqual(set(), dialog.selected_upload_ids())
            self.assertEqual(set(), dialog.selected_metadata_ids())
            self.assertEqual([], dialog.selected_read_targets())
        finally:
            ui_module._restore_stubs_for_tests()

    def test_default_actions_follow_the_kind_of_change(self):
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        try:
            choose = ui_module.dialogs.InventoryDialog.default_actions
            self.assertEqual((True, False), choose(
                {"book_id": 1, "status": "new_in_calibre"}))
            self.assertEqual((True, False), choose(
                {"book_id": 1, "tolino_id": "t1", "status": "changed",
                 "file_changed": True}))
            self.assertEqual((False, True), choose(
                {"book_id": 1, "tolino_id": "t1", "status": "changed"}))
            self.assertEqual((False, False), choose(
                {"book_id": 1, "tolino_id": "t1", "status": "identical"}))
            self.assertEqual((False, False), choose(
                {"book_id": None, "tolino_id": "t1", "status": "only_tolino"}))
        finally:
            ui_module._restore_stubs_for_tests()

class ComparisonLayoutTests(unittest.TestCase):
    """Der Bestandsvergleich bleibt kurz: 7 sichtbare Spalten und EIN Knopf
    für alle zeilenbezogenen Aktionen."""

    @staticmethod
    def _source():
        return _ui_source_text()

    @staticmethod
    def _body():
        source = ComparisonLayoutTests._source()
        return source.split("class InventoryDialog(QDialog):", 1)[1].split(
            "\nclass ", 1)[0]

    def test_two_location_columns_plus_technical_ones(self):
        """Die zwei Orts-Spalten stehen wieder einzeln da.

        Feldbefund: «Bring mir die zwei Spalten zurück, wo die Bücher
        sind.» Die doppelte Statusspalte bleibt entfallen.
        """
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        try:
            headers = ui_module.dialogs.InventoryDialog.HEADERS
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual(("Datei hochladen", "Metadaten aktualisieren", "Gelesen",
                          "In Calibre", "In tolino", "Buchtitel", "Autor(en)",
                          "Hinweis", "ISBN", "tolino-ID", "UUID"), headers)
        technical = ui_module.dialogs.InventoryDialog.TECHNICAL_COLUMNS
        self.assertEqual((8, 9, 10), technical)
        self.assertEqual(8, len(headers) - len(technical))
        # Die eine zusammengefasste Spalte und der doppelte Status sind weg.
        for gone in ("Wo", "Status"):
            self.assertNotIn(gone, headers)

    def test_one_button_holds_all_marked_row_actions(self):
        body = self._body()
        for gone in ("self.download_btn", "self.collection_add_btn",
                     "self.collection_rm_btn", "self.read_selected",
                     "self.more_toggle", "_toggle_more_actions"):
            self.assertNotIn(gone, body, "%s sollte entfallen sein" % gone)
        self.assertIn("self.row_actions = QPushButton(", body)
        self.assertIn("self.row_actions.clicked.connect(self.choose_row_action)",
                      body)

    def test_the_menu_offers_every_row_action(self):
        """Die Spalten-Aktionen gelten auch für markierte Zeilen."""
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        try:
            actions = ui_module.dialogs.InventoryDialog.ROW_ACTIONS
        finally:
            ui_module._restore_stubs_for_tests()
        keys = [key for _label, key in actions]
        self.assertEqual(["upload_on", "upload_off", "metadata_on",
                          "metadata_off", "read", "read_off", "download",
                          "cloud_delete", "cloud_delete_off"], keys)
        labels = [label for label, _key in actions]
        for wanted in ("Upload aktivieren", "Upload deaktivieren",
                       "Metadaten aktivieren",
                       "Metadaten deaktivieren",
                       "Als gelesen markieren", "Als ungelesen markieren",
                       "Aus tolino herunterladen …",
                       "Aus der tolino Cloud löschen …",
                       "Löschmarkierung aufheben"):
            self.assertIn(wanted, labels)
        # Jeder Menüpunkt steht auch im Verteiler, sonst wäre er tot.
        handlers = ui_module.dialogs.InventoryDialog.ROW_ACTION_HANDLERS
        self.assertEqual(set(keys), set(handlers))
        for name, _args in handlers.values():
            self.assertTrue(hasattr(ui_module.dialogs.InventoryDialog, name),
                            "Handler %s fehlt" % name)

    def test_every_row_action_still_has_a_handler(self):
        """Ein Menüpunkt ohne Handler wäre ein toter Eintrag."""
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        try:
            dialog = ui_module.dialogs.InventoryDialog
            used = {name for name, _args
                    in dialog.ROW_ACTION_HANDLERS.values()}
        finally:
            ui_module._restore_stubs_for_tests()
        handlers = {"mark_read_for_marked_rows", "clear_read_for_marked_rows",
                    "set_column_for_marked_rows", "download_selected",
                    "delete_marked_from_cloud",
                    "clear_cloud_delete_for_marked_rows"}
        self.assertEqual(set(), handlers - used)

class InventoryDialogReadStateTests(_QtDialogTest):
    """Spalte «Gelesen» kommt aus der Cloud und lässt sich mehrfach setzen.

    Feldbefund: «Der Knopf Gelesen soll bereits ausgewählt sein, wenn
    das Buch in der Cloud als gelesen markiert ist» und «Jetzt muss ich alle
    meine 500 Bücher einzeln anklicken».
    """

    def _rows(self):
        return [
            {"title": "Gelesen", "authors": "A", "status": "identical",
             "book_id": 1, "tolino_id": "t-1", "read_in_cloud": True},
            {"title": "Ungelesen", "authors": "B", "status": "identical",
             "book_id": 2, "tolino_id": "t-2"},
            {"title": "Auch ungelesen", "authors": "C", "status": "changed",
             "book_id": 3, "tolino_id": "t-3"},
            {"title": "Nur lokal", "authors": "D",
             "status": "new_in_calibre", "book_id": 4, "tolino_id": ""},
        ]

    @staticmethod
    def _marked_table(marked):
        class Index:
            def __init__(self, row):
                self._row = row

            def row(self):
                return self._row

        class Model:
            def selectedRows(self):
                return [Index(row) for row in marked]

        class Table:
            def selectionModel(self):
                return Model()

        return Table()

    @staticmethod
    def _cells_by_title(dialog):
        """Zeilen-Widgets, nach Titel – das Fenster sortiert beim Bau."""
        cells = {(row, column): widget
                 for row, column, widget in dialog.table.cell_widgets}
        return {row["title"]: cells[(index, 2)]
                for index, row in enumerate(dialog.rows)}

    def test_cloud_read_book_is_prechecked_and_unlockable(self):
        ui_module = self._ui()
        try:
            self._messages(ui_module)
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            read_cell = self._cells_by_title(dialog)
            # Schon gelesen: angehakt -- aber anklickbar, damit man es auch
            # wieder auf ungelesen setzen kann.
            self.assertTrue(read_cell["Gelesen"].isChecked())
            self.assertTrue(read_cell["Gelesen"].enabled)
            self.assertIn("Ausgewählt = gelesen",
                          read_cell["Gelesen"].tooltip)
            # Ungelesen: leer, aber anklickbar.
            self.assertFalse(read_cell["Ungelesen"].isChecked())
            self.assertTrue(read_cell["Ungelesen"].enabled)
            # Ohne Cloud-Kopie gibt es keine Lese-Marke.
            self.assertFalse(read_cell["Nur lokal"].enabled)
            self.assertIn("in tolino", read_cell["Nur lokal"].tooltip)
            # Nur echte Änderungen gehen an die Cloud -- und ohne Klick
            # wird nichts ungelesen.
            self.assertEqual([], dialog.selected_read_targets())
            self.assertEqual([], dialog.selected_read_unmarks())
            read_cell["Ungelesen"].setChecked(True)
            read_cell["Auch ungelesen"].setChecked(True)
            self.assertEqual({"t-2", "t-3"},
                             set(dialog.selected_read_targets()))
            dialog._update_summary()
            self.assertIn("2 Lesestatus", dialog.summary.text())
        finally:
            ui_module._restore_stubs_for_tests()

    def test_recommendation_keeps_the_cloud_read_marks(self):
        """«Empfehlung auswählen» hakt nie etwas Neues auf gelesen und
        nimmt die Cloud-Marken nicht weg."""
        ui_module = self._ui()
        try:
            self._messages(ui_module)
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            read_cell = self._cells_by_title(dialog)
            read_cell["Ungelesen"].setChecked(True)
            dialog._sync_checks_to_rows()
            dialog.apply_recommended()
            self.assertTrue(read_cell["Gelesen"].isChecked())
            self.assertFalse(read_cell["Ungelesen"].isChecked())
            self.assertEqual([], dialog.selected_read_targets())
            self.assertEqual([], dialog.selected_read_unmarks())
        finally:
            ui_module._restore_stubs_for_tests()

    def test_a_read_book_is_unmarked_with_one_click(self):
        """Ein Klick schaltet die Lese-Marke direkt um (Feldwunsch)."""
        ui_module = self._ui()
        try:
            self._messages(ui_module)
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            cell = self._cells_by_title(dialog)["Gelesen"]
            self.assertTrue(cell.isChecked())
            cell.setChecked(False)
            cell.clicked.emit()
            self.assertFalse(cell.isChecked())
            self.assertEqual(["t-1"], dialog.selected_read_unmarks())
            self.assertEqual([], dialog.selected_read_targets())
            dialog._update_summary()
            self.assertIn("1 Lesestatus", dialog.summary.text())
            self.assertIn("Synchronisierung starten", dialog.confirm.text())
            # Und wieder an: ein Klick zurueck.
            cell.setChecked(True)
            cell.clicked.emit()
            self.assertEqual([], dialog.selected_read_unmarks())
            self.assertEqual([], dialog.selected_read_targets())
        finally:
            ui_module._restore_stubs_for_tests()

    def test_an_unmarked_book_stays_unmarked_after_sorting(self):
        """Umsortieren baut die Zeilen neu – der Wunsch bleibt."""
        ui_module = self._ui()
        try:
            self._messages(ui_module)
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            cell = self._cells_by_title(dialog)["Gelesen"]
            cell.setChecked(False)
            cell.clicked.emit()   # in einem Klick abwaehlen
            dialog._sort_by_column(0)
            dialog._sort_by_column(0)
            self.assertEqual(["t-1"], dialog.selected_read_unmarks())
            rebuilt = self._cells_by_title(dialog)["Gelesen"]
            self.assertFalse(rebuilt.isChecked())
            # Ein Klick hakt sie wieder an.
            rebuilt.setChecked(True)
            rebuilt.clicked.emit()
            self.assertEqual([], dialog.selected_read_unmarks())
        finally:
            ui_module._restore_stubs_for_tests()

    def test_marked_rows_get_the_read_mark_in_one_click(self):
        """Die Zeilen-Aktion hakt ungelesene Cloud-Bücher in einem Zug
        auf – ohne Rückfrage und ohne Meldung."""
        ui_module = self._ui()
        try:
            calls = self._messages(ui_module)
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            dialog.table = self._marked_table(range(len(dialog.rows)))
            dialog.mark_read_for_marked_rows()
            # Das gelesene Buch hat schon seine Marke, „Nur lokal“ keine
            # Cloud-Kopie -- nur die zwei ungelesenen Cloud-Bücher bleiben.
            self.assertEqual({"t-2", "t-3"},
                             set(dialog.selected_read_targets()))
            self.assertEqual([], calls)
        finally:
            ui_module._restore_stubs_for_tests()

    def test_marked_rows_without_cloud_copy_change_nothing(self):
        ui_module = self._ui()
        try:
            calls = self._messages(ui_module)
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            local_index = [index for index, row in enumerate(dialog.rows)
                           if not row["tolino_id"]]
            dialog.table = self._marked_table(local_index)
            dialog.mark_read_for_marked_rows()
            self.assertEqual([], dialog.selected_read_targets())
            self.assertTrue(any("nichts geändert" in text.casefold()
                                for text in calls))
        finally:
            ui_module._restore_stubs_for_tests()

    def test_click_without_marked_rows_explains_the_multi_selection(self):
        ui_module = self._ui()
        try:
            calls = self._messages(ui_module)
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            dialog.table = self._marked_table([])
            dialog.mark_read_for_marked_rows()
            self.assertTrue(any("Strg" in text for text in calls), calls)
            self.assertEqual([], dialog.selected_read_targets())
        finally:
            ui_module._restore_stubs_for_tests()

    def test_marked_rows_can_be_chosen_for_upload_and_metadata(self):
        """Hochladen/Metadaten für markierte Zeilen – wie die Kästchen."""
        ui_module = self._ui()
        try:
            self._messages(ui_module)
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            dialog.table = self._marked_table(range(len(dialog.rows)))
            dialog.set_column_for_marked_rows("upload", True, "keine Datei")
            self.assertEqual({1, 2, 3, 4}, dialog.selected_upload_ids())
            dialog.set_column_for_marked_rows("metadata", True, "keine Cloud")
            # «Nur lokal» hat keine Cloud-Kopie – dort geht kein Metadaten-Abgleich.
            self.assertEqual({1, 2, 3}, dialog.selected_metadata_ids())
            dialog.set_column_for_marked_rows("upload", False, "keine Datei")
            self.assertEqual(set(), dialog.selected_upload_ids())
            dialog.set_column_for_marked_rows("metadata", False, "keine Cloud")
            self.assertEqual(set(), dialog.selected_metadata_ids())
        finally:
            ui_module._restore_stubs_for_tests()

    def test_marked_rows_can_be_set_back_to_unread(self):
        """«Als ungelesen markieren» löst die Sperre und zählt als Abwahl."""
        ui_module = self._ui()
        try:
            self._messages(ui_module)
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            dialog.table = self._marked_table(range(len(dialog.rows)))
            dialog.clear_read_for_marked_rows()
            # Nur das in der Cloud gelesene Buch lässt sich zurücksetzen.
            self.assertEqual({"t-1"}, set(dialog.selected_read_unmarks()))
            self.assertEqual([], dialog.selected_read_targets())
            row = next(item for item in dialog.rows
                       if item["tolino_id"] == "t-1")
            self.assertFalse(row["read"])
        finally:
            ui_module._restore_stubs_for_tests()


    def test_one_button_handles_all_actions_for_marked_rows(self):
        """Alle vier Zeilen-Aktionen liegen hinter EINEM Knopf."""
        ui_module = self._ui()
        try:
            self._messages(ui_module)
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            self.assertEqual("Aktionen für markierte Bücher …",
                             dialog.row_actions.text())
            self.assertIn("Strg", dialog.row_actions.tooltip)
            keys = [key for _label, key in dialog.ROW_ACTIONS]
            self.assertEqual(["upload_on", "upload_off", "metadata_on",
                              "metadata_off", "read", "read_off", "download",
                              "cloud_delete", "cloud_delete_off"], keys)
        finally:
            ui_module._restore_stubs_for_tests()

class MarkedRowActionFeedbackTests(_QtDialogTest):
    """Die Rückmeldung nach einer Zeilen-Aktion nennt die Zahlen.

    Vorher schwieg eine erfolgreiche Aktion; nur «nichts zu tun» kam als
    Meldung. Neu steht die Bilanz unter der Tabelle – ohne modalen Dialog.
    """

    @staticmethod
    def _rows():
        return [
            {"title": "Gelesen", "authors": "A", "status": "identical",
             "book_id": 1, "tolino_id": "t-1", "read_in_cloud": True},
            {"title": "Ungelesen", "authors": "B", "status": "identical",
             "book_id": 2, "tolino_id": "t-2"},
            {"title": "Cloud zwei", "authors": "C", "status": "identical",
             "book_id": 3, "tolino_id": "t-3"},
            {"title": "Nur lokal", "authors": "D",
             "status": "new_in_calibre", "book_id": 4, "tolino_id": ""},
        ]

    @staticmethod
    def _row(dialog, title):
        return next(row for row in dialog.rows if row["title"] == title)

    def _index(self, dialog, title):
        return dialog.rows.index(self._row(dialog, title))

    def test_without_marked_rows_nothing_changes(self):
        ui_module = self._ui()
        try:
            calls = self._messages(ui_module)
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            before = [dict(row) for row in dialog.rows]
            with self._marked([]):
                dialog.set_column_for_marked_rows("upload", True, "keine Datei")
            self.assertEqual(before, dialog.rows)
            self.assertEqual("", dialog.action_feedback.text())
            self.assertTrue(any("Markiere zuerst" in text for text in calls))
        finally:
            ui_module._restore_stubs_for_tests()

    def test_one_marked_row_reports_one_of_one(self):
        ui_module = self._ui()
        try:
            calls = self._messages(ui_module)
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            index = self._index(dialog, "Ungelesen")
            with self._marked([index]):
                dialog.set_column_for_marked_rows("upload", True, "keine Datei")
            self.assertTrue(self._row(dialog, "Ungelesen")["upload"])
            self.assertFalse(self._row(dialog, "Gelesen")["upload"])
            # Zeile UND Auswahlfeld in der Tabelle sind gesetzt.
            cells = {(row, column): widget
                     for row, column, widget in dialog.table.cell_widgets}
            self.assertTrue(cells[(index, 0)].isChecked())
            self.assertTrue(dialog.checks[index].isChecked())
            self.assertEqual(
                "Upload aktiviert für 1 von 1 markierten Büchern.",
                dialog.action_feedback.text())
            self.assertEqual([], calls)
        finally:
            ui_module._restore_stubs_for_tests()

    def test_several_marked_rows_report_changed_and_skipped(self):
        ui_module = self._ui()
        try:
            calls = self._messages(ui_module)
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            with self._marked(range(len(dialog.rows))):
                dialog.set_column_for_marked_rows("metadata", True,
                                                  "keine Cloud")
            # «Nur lokal» hat keine Cloud-Kopie – eine Zeile wird übersprungen.
            self.assertEqual(
                "Metadaten aktiviert für 3 von 4 markierten Büchern; "
                "1 übersprungen.",
                dialog.action_feedback.text())
            cells = {(row, column): widget
                     for row, column, widget in dialog.table.cell_widgets}
            for index, row in enumerate(dialog.rows):
                wanted = bool(row["tolino_id"])
                self.assertEqual(wanted, dialog.meta_checks[index].isChecked())
                self.assertEqual(wanted, cells[(index, 1)].isChecked())
            self.assertEqual([], calls)
        finally:
            ui_module._restore_stubs_for_tests()

    def test_nothing_to_do_keeps_the_info_box_and_clears_the_feedback(self):
        ui_module = self._ui()
        try:
            calls = self._messages(ui_module)
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            with self._marked(range(len(dialog.rows))):
                dialog.set_column_for_marked_rows("metadata", True,
                                                  "keine Cloud")
            self.assertIn("3 von 4", dialog.action_feedback.text())
            # Dieselbe Aktion noch einmal: jetzt ist nichts mehr zu tun.
            with self._marked(range(len(dialog.rows))):
                dialog.set_column_for_marked_rows("metadata", True,
                                                  "keine Cloud")
            self.assertEqual("", dialog.action_feedback.text())
            self.assertTrue(any("nichts geändert" in text.casefold()
                                for text in calls))
        finally:
            ui_module._restore_stubs_for_tests()

    def test_read_action_reports_the_count_too(self):
        ui_module = self._ui()
        try:
            calls = self._messages(ui_module)
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            with self._marked(range(len(dialog.rows))):
                dialog.mark_read_for_marked_rows()
            self.assertEqual(
                "Als gelesen markiert für 2 von 4 markierten Büchern; "
                "2 übersprungen.",
                dialog.action_feedback.text())
            self.assertEqual([], calls)
        finally:
            ui_module._restore_stubs_for_tests()

class SingleBookActionTests(_QtDialogTest):
    """Download und Sammlungen gelten nur für EIN Buch.

    Das stand bisher nur im Tooltip; jetzt sagt es der Menütext, und die
    Ablehnung bei mehreren markierten Zeilen bleibt vor jedem Dialog.
    """

    @staticmethod
    def _kinds(events):
        return [event[0] if isinstance(event, tuple) else event
                for event in events]

    def _download(self, ui_module, marked, title=None):
        events = []

        class Client:
            @staticmethod
            def download(tolino_id, **kwargs):
                events.append(("download", tolino_id))
                return b"data"

        class FileDialog:
            @staticmethod
            def getSaveFileName(*args, **kwargs):
                events.append("save")
                return "/tmp/tolino-single-book-test.epub", ""

        class MsgBox:
            @staticmethod
            def information(*args, **kwargs):
                events.append(
                    ("information", " ".join(str(arg) for arg in args)))

        def factory():
            events.append("client")
            return Client()

        ui_module.dialogs.QFileDialog = FileDialog
        ui_module.dialogs.QMessageBox = MsgBox
        ui_module.dialogs.info_dialog = (
            lambda *args, **kwargs: events.append("info"))
        dialog = ui_module.dialogs.InventoryDialog(self._rows())
        dialog.client_factory = factory
        if title is not None:
            marked = [next(index for index, row in enumerate(dialog.rows)
                           if row["title"] == title)]
        with self._marked(marked):
            dialog.download_selected()
        return events

    def test_bulk_selection_updates_the_summary_once(self):
        """Sammelaktionen schreiben die Zusammenfassung genau einmal."""
        ui_module = self._ui()
        try:
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            calls = []
            dialog._update_summary = lambda *args: calls.append(args)
            dialog.apply_recommended()
            self.assertEqual(1, len(calls))
            calls.clear()
            dialog.clear_all_selection()
            self.assertEqual(1, len(calls))
        finally:
            ui_module._restore_stubs_for_tests()

    def test_download_without_a_marked_row_does_nothing(self):
        ui_module = self._ui()
        try:
            events = self._download(ui_module, [])
            self.assertEqual(["information"], self._kinds(events))
            self.assertIn("Markiere zuerst", events[0][1])
        finally:
            ui_module._restore_stubs_for_tests()

    def test_download_refuses_several_rows_before_any_dialog(self):
        ui_module = self._ui()
        try:
            events = self._download(ui_module, [0, 1])
            self.assertEqual(["information"], self._kinds(events))
            self.assertIn("nur für ein Buch", events[0][1])
        finally:
            ui_module._restore_stubs_for_tests()

    def test_download_with_one_row_still_works(self):
        ui_module = self._ui()
        try:
            events = self._download(ui_module, [], title="Gelesen")
            self.assertIn("save", self._kinds(events))
            self.assertIn("download", self._kinds(events))
            self.assertNotIn("cover", self._kinds(events))
            self.assertIn("info", self._kinds(events))
        finally:
            try:
                os.remove("/tmp/tolino-single-book-test.epub")
            except OSError:
                pass
            ui_module._restore_stubs_for_tests()

class RowActionPromptTests(_QtDialogTest):
    """Der Menü-Prompt nennt die Zahl der markierten Bücher."""

    @staticmethod
    def _rows():
        return [
            {"title": "Gelesen", "authors": "A", "status": "identical",
             "book_id": 1, "tolino_id": "t-1"},
            {"title": "Ungelesen", "authors": "B", "status": "identical",
             "book_id": 2, "tolino_id": "t-2"},
            {"title": "Nur lokal", "authors": "C",
             "status": "new_in_calibre", "book_id": 3, "tolino_id": ""},
        ]

    def test_the_menu_selects_the_action_for_marked_books(self):
        ui_module = self._ui()
        try:
            chosen = []

            class Menu:
                def __init__(self, _parent=None):
                    self.actions = []

                def addAction(self, label):
                    self.actions.append(label)
                    return label

                def exec(self, _position=None):
                    chosen.extend(self.actions)
                    return self.actions[0]

            ui_module.dialogs.QMenu = Menu
            ui_module.dialogs.QMessageBox = type("MsgBox", (), {
                "information": staticmethod(lambda *args, **kwargs: None)})
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            with patch("calibre_plugin.ui_dialogs.selected_table_rows",
                       lambda _table: [0, 1, 2]):
                dialog.choose_row_action()
            self.assertEqual(6, len(chosen))
            self.assertTrue(dialog.rows[0]["upload"])
        finally:
            ui_module._restore_stubs_for_tests()

    def test_cloud_actions_appear_only_when_the_selection_has_cloud_copies(self):
        ui_module = self._ui()
        try:
            chosen = []

            class Menu:
                def __init__(self, _parent=None):
                    self.actions = []

                def addAction(self, label):
                    self.actions.append(label)
                    return label

                def exec(self, _position=None):
                    chosen.extend(self.actions)
                    return None

            ui_module.dialogs.QMenu = Menu
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            local_index = next(index for index, row in enumerate(dialog.rows)
                               if row["title"] == "Nur lokal")
            dialog.rows[local_index]["has_preferred_format"] = True
            dialog.rows[local_index]["upload"] = False
            with patch("calibre_plugin.ui_dialogs.selected_table_rows",
                       lambda _table: [local_index]):
                dialog.choose_row_action()
            self.assertEqual(["Upload aktivieren"], chosen)
            self.assertNotIn("Aus tolino herunterladen …", chosen)
            self.assertNotIn("Aus der tolino Cloud löschen …", chosen)
            self.assertNotIn("Metadaten aktivieren", chosen)
            self.assertNotIn("Als gelesen markieren", chosen)
        finally:
            ui_module._restore_stubs_for_tests()

    def test_without_marked_rows_no_menu_opens(self):
        ui_module = self._ui()
        try:
            prompts = []

            class Menu:
                def __init__(self, _parent=None):
                    prompts.append("menu")

                def addAction(self, label):
                    return label

                def exec(self, _position=None):
                    return None

            class MsgBox:
                @staticmethod
                def information(*args, **kwargs):
                    prompts.append("info")

            ui_module.dialogs.QMenu = Menu
            ui_module.dialogs.QMessageBox = MsgBox
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            with patch("calibre_plugin.ui_dialogs.selected_table_rows",
                       lambda _table: []):
                dialog.choose_row_action()
            self.assertEqual(["info"], prompts)
        finally:
            ui_module._restore_stubs_for_tests()

class CloudDeletionDialogTests(_QtDialogTest):
    """«Aus der tolino Cloud löschen» für markierte Zeilen."""

    @staticmethod
    def _rows():
        return [
            {"title": "Löschen", "authors": "A", "status": "identical",
             "book_id": 1, "tolino_id": "t-1", "uuid": "u-1",
             "explanation": "In beiden vorhanden."},
            {"title": "Behalten", "authors": "B", "status": "identical",
             "book_id": 2, "tolino_id": "t-2", "uuid": "u-2",
             "explanation": "In beiden vorhanden."},
            {"title": "Nur lokal", "authors": "C",
             "status": "new_in_calibre", "book_id": 3, "tolino_id": "",
             "uuid": "u-3", "explanation": "Nur in Calibre."},
        ]

    @staticmethod
    def _index(dialog, title):
        return next(index for index, row in enumerate(dialog.rows)
                    if row["title"] == title)

    @staticmethod
    def _msgbox(ui_module, confirm=True):
        calls = []

        class MsgBox:
            Yes = 1
            No = 0

            @staticmethod
            def question(*args, **kwargs):
                calls.append(("question", " ".join(str(a) for a in args)))
                return MsgBox.Yes if confirm else MsgBox.No

            @staticmethod
            def information(*args, **kwargs):
                calls.append(("information", " ".join(str(a) for a in args)))

        ui_module.dialogs.QMessageBox = MsgBox
        return calls

    @staticmethod
    def _marked(index):
        return patch("calibre_plugin.ui_dialogs.selected_table_rows",
                     lambda _table: [index])

    def test_marked_cloud_copy_is_queued_and_conflicting_actions_clear(self):
        ui_module = self._ui()
        try:
            calls = self._msgbox(ui_module)
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            dialog.clear_all_selection()
            index = self._index(dialog, "Löschen")
            with self._marked(index):
                dialog.delete_marked_from_cloud()
            self.assertEqual(
                [("u-1", "t-1", "Löschen")],
                dialog.selected_cloud_deletions())
            self.assertTrue(dialog.rows[index]["delete_cloud"])
            self.assertIn("Wird beim Start aus tolino gelöscht.",
                          dialog.table.items[(index, 7)])
            cells = {(row, column): widget
                     for row, column, widget in dialog.table.cell_widgets}
            for column in (0, 1, 2):
                self.assertFalse(cells[(index, column)].enabled)
                self.assertFalse(cells[(index, column)].isChecked())
            self.assertIn("Zum Löschen", cells[(index, 0)].tooltip)
            self.assertIn("1 Löschung", dialog.summary.text())
            self.assertIn("(1)", dialog.confirm.text())
            self.assertFalse(any(kind == "question"
                                 for kind, _text in calls))
            # Die nicht markierte Cloud-Kopie bleibt unangetastet.
            other = self._index(dialog, "Behalten")
            self.assertFalse(dialog.rows[other].get("delete_cloud"))
            self.assertTrue(cells[(other, 1)].enabled)
        finally:
            ui_module._restore_stubs_for_tests()

    def test_cancelling_the_question_changes_nothing(self):
        ui_module = self._ui()
        try:
            self._msgbox(ui_module, confirm=False)
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            index = self._index(dialog, "Löschen")
            with self._marked(index):
                dialog.delete_marked_from_cloud()
            self.assertEqual([("u-1", "t-1", "Löschen")],
                             dialog.selected_cloud_deletions())
            self.assertTrue(dialog.rows[index].get("delete_cloud"))
            cells = {(row, column): widget
                     for row, column, widget in dialog.table.cell_widgets}
            self.assertFalse(cells[(index, 0)].enabled)
        finally:
            ui_module._restore_stubs_for_tests()

    def test_the_delete_mark_can_be_removed_again(self):
        ui_module = self._ui()
        try:
            calls = self._msgbox(ui_module)
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            index = self._index(dialog, "Löschen")
            with self._marked(index):
                dialog.delete_marked_from_cloud()
            self.assertEqual(1, len(dialog.selected_cloud_deletions()))
            with self._marked(index):
                dialog.clear_cloud_delete_for_marked_rows()
            self.assertEqual([], dialog.selected_cloud_deletions())
            self.assertFalse(dialog.rows[index].get("delete_cloud"))
            self.assertNotIn("Wird beim Start aus tolino gelöscht.",
                             dialog.table.items[(index, 7)])
            cells = {(row, column): widget
                     for row, column, widget in dialog.table.cell_widgets}
            self.assertTrue(cells[(index, 0)].enabled)
            self.assertTrue(cells[(index, 1)].enabled)
            self.assertFalse(any("nichts geändert" in text.casefold()
                                 for kind, text in calls
                                 if kind == "information"))
        finally:
            ui_module._restore_stubs_for_tests()

    def test_rows_without_cloud_copy_are_explained_not_deleted(self):
        ui_module = self._ui()
        try:
            calls = self._msgbox(ui_module)
            dialog = ui_module.dialogs.InventoryDialog(self._rows())
            index = self._index(dialog, "Nur lokal")
            with self._marked(index):
                dialog.delete_marked_from_cloud()
            self.assertEqual([], dialog.selected_cloud_deletions())
            self.assertEqual(1, len(calls))
            self.assertEqual("information", calls[0][0])
            self.assertIn("Es wurde nichts geändert", calls[0][1])
        finally:
            ui_module._restore_stubs_for_tests()

class CloudDeletionPlanTests(unittest.TestCase):
    """markierte Löschungen erreichen Plan und Bestätigung."""

    def _ui(self):
        return ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)

    @staticmethod
    def _dashboard(ui_module):
        dashboard = ui_module.dashboard.SyncDashboard.__new__(
            ui_module.dashboard.SyncDashboard)
        dashboard._upload_jobs = lambda *args, **kwargs: ([], [])
        return dashboard

    @staticmethod
    def _settings():
        return {"preferred_formats": ("EPUB",),
                "sync_collections": False, "upload_covers": False}

    @staticmethod
    def _metadata(uuid="u-1", title="Titel"):
        return {1: {"uuid": uuid, "title": title, "formats": ["EPUB"],
                    "format_signatures": {}, "last_modified": "1"}}

    @staticmethod
    def _selection(deletions=()):
        return {"uploads": [], "metadata": [], "read": [], "unread": [],
                "cloud_deletions": list(deletions)}

    def test_marked_deletion_is_planned_after_explicit_selection(self):
        ui_module = self._ui()
        try:
            dashboard = self._dashboard(ui_module)
            state = {"u-1": {"tolino_id": "t-1", "calibre_id": 1,
                             "title": "Titel"}}
            preparation = dashboard._plan_actions(
                self._settings(), self._metadata(), state,
                self._selection([("u-1", "t-1", "Titel")]))
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual([("u-1", "t-1")], preparation["removals"])
        self.assertEqual({"t-1": "Titel"}, preparation["removal_titles"])

    def test_explicitly_marked_cloud_copy_is_planned_once(self):
        ui_module = self._ui()
        try:
            dashboard = self._dashboard(ui_module)
            state = {"u-9": {"tolino_id": "t-9", "title": "Alt"}}
            preparation = dashboard._plan_actions(
                self._settings(), self._metadata(), state,
                self._selection([("u-9", "t-9", "Alt")]))
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual([("u-9", "t-9")], preparation["removals"])
        self.assertEqual({"t-9": "Alt"}, preparation["removal_titles"])

    def test_confirmed_selection_carries_marked_deletions(self):
        ui_module = self._ui()
        try:
            seen = {}

            class FakeQDialog:
                Accepted = 1

            class FakeInventory:
                def __init__(self, rows, parent, client_factory=None,
                             options=None):
                    seen["options"] = options

                def exec(self):
                    return FakeQDialog.Accepted

                def selected_upload_ids(self):
                    return []

                def selected_metadata_ids(self):
                    return []

                def selected_read_targets(self):
                    return []

                def selected_read_unmarks(self):
                    return []

                def selected_cloud_deletions(self):
                    return [("u-1", "t-1", "Titel")]

            ui_module.dashboard.QDialog = FakeQDialog
            ui_module.dashboard.InventoryDialog = FakeInventory
            dashboard = ui_module.dashboard.SyncDashboard.__new__(
                ui_module.dashboard.SyncDashboard)
            selection = dashboard._confirm_comparison(
                [], self._settings(), client_factory=lambda: object())
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual([("u-1", "t-1", "Titel")],
                         selection["cloud_deletions"])
        self.assertNotIn("deletions", seen["options"])

    def test_merge_ignores_malformed_entries_and_blank_ids(self):
        pairs, titles = merge_removals(
            [("u-1", "t-1"), ("only-one",)],
            [None, ("u-2", "", "ohne ID"), ("u-2", "t-1", "doppelt"),
             ("u-3", "t-3", "Drei")])
        self.assertEqual([("u-1", "t-1"), ("u-3", "t-3")], pairs)
        # Der ausdrücklich markierte Titel gewinnt auch bei doppelter ID.
        self.assertEqual({"t-1": "doppelt", "t-3": "Drei"}, titles)

class DialogLayoutTests(unittest.TestCase):
    """P2: der Vergleich passt auf 1366x768, Textspalten wachsen."""

    def test_minimum_width_fits_a_small_screen(self):
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None,
            qt_stub=ui_base_tests.InventoryDialogBuildTests._qt_stub())
        try:
            dialog = ui_module.dialogs.InventoryDialog([], options={})
            self.assertEqual((640, 460), dialog.minimum_size)
        finally:
            ui_module._restore_stubs_for_tests()
        # 1366x768 bei 125 % Skalierung = 1092x614 logische Pixel. Die
        # Mindestgroesse und die Startgroesse muessen hineinpassen.
        logical = (int(1366 / 1.25), int(768 / 1.25))
        self.assertLessEqual(dialog.minimum_size[0], logical[0])
        self.assertLessEqual(dialog.minimum_size[1], logical[1])

class ReviewHintTests(unittest.TestCase):
    """Zeilen in «Zu prüfen» werden nicht vorausgewählt – das muss dastehen."""

    def _ui(self, messages):
        class MessageBox:
            @staticmethod
            def information(*args, **_kwargs):
                messages.append(args)

            @staticmethod
            def warning(*args, **_kwargs):
                messages.append(args)

        stub = ui_base_tests.InventoryDialogBuildTests._qt_stub()
        stub["QMessageBox"] = MessageBox
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None, qt_stub=stub)
        self.addCleanup(ui_module._restore_stubs_for_tests)
        return ui_module

    @staticmethod
    def _row():
        return {"title": "Der Schwarm", "authors": "Frank Schatzing",
                "status": "review", "group": "review", "book_id": 1,
                "tolino_id": "t1", "isbn": "9783161484100",
                "explanation": "Autor oder ISBN widersprechen sich."}

    def test_the_guard_names_the_undecided_rows(self):
        messages = []
        ui_module = self._ui(messages)
        dialog = ui_module.dialogs.InventoryDialog([self._row()], options={})
        dialog.accept()
        self.assertEqual(1, len(messages))
        text = " ".join(str(part) for part in messages[0])
        self.assertIn("Zu prüfen", text)

    def test_the_note_explains_undecided_rows(self):
        messages = []
        ui_module = self._ui(messages)
        dialog = ui_module.dialogs.InventoryDialog([self._row()], options={})
        self.assertIn("Zu prüfen", dialog.summary.text())

class AutomaticCloseGuardTests(unittest.TestCase):
    """Automatiken dürfen den Wächter nur umgehen, wenn sie etwas tun."""

    def _ui(self, messages):
        class BaseDialog(ui_base_tests.InventoryDialogBuildTests._qt_stub()["QDialog"]):
            closed = []

            def accept(self):
                BaseDialog.closed.append(True)

        class MessageBox:
            @staticmethod
            def information(*args, **_kwargs):
                messages.append(args)

            @staticmethod
            def warning(*args, **_kwargs):
                messages.append(args)

            @staticmethod
            def question(*args, **_kwargs):
                return 0

        stub = ui_base_tests.InventoryDialogBuildTests._qt_stub()
        stub["QDialog"] = BaseDialog
        stub["QMessageBox"] = MessageBox
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None, qt_stub=stub)
        self.addCleanup(ui_module._restore_stubs_for_tests)
        return ui_module

    @staticmethod
    def _rows():
        return [{"title": "Neu", "authors": "A", "status": "new_in_calibre",
                 "book_id": 1, "tolino_id": "", "isbn": "111"}]

    def test_switched_on_collections_without_open_work_do_not_close(self):
        messages = []
        ui_module = self._ui(messages)
        dialog = ui_module.dialogs.InventoryDialog(
            self._rows(), options={"collections": True, "automatic": 0})
        dialog.clear_all_selection()
        dialog.accept()
        self.assertEqual([], ui_module.common.QDialog.closed)
        self.assertEqual(1, len(messages))

    def test_open_collections_may_close(self):
        messages = []
        ui_module = self._ui(messages)
        dialog = ui_module.dialogs.InventoryDialog(
            self._rows(), options={"collections": True, "automatic": 2})
        dialog.clear_all_selection()
        dialog.accept()
        self.assertEqual([True], ui_module.common.QDialog.closed)

class AuthorFormattingRegressionTests(unittest.TestCase):
    """0.15 -> 0.16: anders geschriebene Autoren galten als Widerspruch.

    In 0.15 war ein Buch mit gleicher ISBN und «Schätzing, Frank» gegen
    «Frank Schatzing» ein normaler Unterschied (Metadaten-Abgleich wurde
    vorausgewählt). 0.16 machte daraus «Zu prüfen» ohne Vorauswahl: Wer
    «Synchronisierung starten» drückte, plante damit nichts ein – das Fenster
    schloss sich und es wurde weder hochgeladen noch aktualisiert.
    """

    @staticmethod
    def _metadata(**values):
        base = {"uuid": "u1", "title": "Der Schwarm",
                "authors": "Frank Schatzing", "isbn": "9783161484100",
                "formats": ["EPUB"], "last_modified": "1",
                "format_signatures": {}}
        base.update(values)
        return {1: base}

    @staticmethod
    def _inventory(**values):
        base = {"id": "t1", "title": "Der Schwarm",
                "authors": "Schätzing, Frank", "isbn": "9783161484100"}
        base.update(values)
        return [base]

    def _row(self, metadata=None, inventory=None, state=None):
        rows = compare_inventory(
            metadata or self._metadata(), state or {},
            inventory or self._inventory(), ["EPUB"],
            ("authors", "title", "isbn"), use_metadata_ids=False)
        return rows[0]

    def test_reversed_author_order_is_not_a_conflict(self):
        row = self._row()
        self.assertNotEqual("review", row["status"])
        self.assertEqual("changed", row["status"])

    def test_reversed_author_order_preselects_the_metadata_update(self):
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None,
            qt_stub=ui_base_tests.InventoryDialogBuildTests._qt_stub())
        self.addCleanup(ui_module._restore_stubs_for_tests)
        row = self._row()
        row.setdefault("group", "action")
        self.assertEqual((False, True),
                         ui_module.dialogs.InventoryDialog.default_actions(row))

    def test_non_latin_and_extended_latin_author_names_match(self):
        from ..matching import authors_match as _authors_match
        self.assertTrue(_authors_match("Достоевский Фёдор",
                                      "Фёдор Достоевский"))
        self.assertTrue(_authors_match("Bolesław Prus", "Boleslaw Prus"))

    def test_accent_and_comma_variants_match(self):
        from ..matching import authors_match as _authors_match
        cases = (
            ("Frank Schatzing", "Schätzing, Frank"),
            ("J. R. R. Tolkien", "Tolkien, J. R. R."),
            ("J K Rowling", "Joanne K. Rowling"),
            ("Müller, Hans", "Hans Mueller"),
            ("Autor A & Autor B", "Autor B und Autor A"),
        )
        for left, right in cases:
            self.assertTrue(_authors_match(left, right), (left, right))

    def test_really_different_authors_stay_a_review(self):
        row = self._row(
            inventory=[{"id": "t1", "title": "Der Schwarm",
                        "authors": "Ganz Anderer", "isbn": "9783161484100"}])
        self.assertEqual("review", row["status"])
        self.assertEqual("conflict", row["match_reason"])

    def test_known_tolino_id_with_reordered_authors_is_a_change(self):
        state = {"u1": {"tolino_id": "t1"}}
        row = self._row(state=state)
        self.assertNotEqual("review", row["status"])
        self.assertEqual("t1", row["tolino_id"])

class DialogClientReuseTests(_QtDialogTest):
    """Aktionen im Vergleichsfenster teilen einen Client bis zum Fehler."""

    @staticmethod
    def _rows():
        return [{"title": "A", "authors": "B", "status": "identical",
                 "book_id": 1, "tolino_id": "t-1"}]

    def test_client_is_cached_between_actions(self):
        ui_module = self._ui()
        calls = []
        client = type("Client", (), {})()
        try:
            dialog = ui_module.dialogs.InventoryDialog(self._rows(), options={})
            dialog.client_factory = lambda: calls.append(1) or client
            self.assertIs(client, dialog._cached_or_new_client())
            self.assertIs(client, dialog._cached_or_new_client())
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual(1, len(calls))

    def test_a_failed_action_drops_the_cached_login(self):
        ui_module = self._ui()
        factory_calls = []
        clients = []

        class Client:
            def download(self, tolino_id, **kwargs):
                raise RuntimeError("Download kaputt")

        def factory():
            factory_calls.append(1)
            clients.append(Client())
            return clients[-1]

        class Index:
            @staticmethod
            def row():
                return 0

        class Selection:
            @staticmethod
            def selectedRows():
                return [Index()]

        class FileDialog:
            @staticmethod
            def getSaveFileName(*args, **kwargs):
                return os.path.join(tempfile.gettempdir(),
                                    "tolino-reuse-test.epub"), ""

        dialog = ui_module.dialogs.InventoryDialog(self._rows(), options={})
        dialog.client_factory = factory
        dialog.table.selectionModel = lambda: Selection()
        ui_module.dialogs.QFileDialog = FileDialog
        ui_module.dialogs.error_dialog = lambda *args, **kwargs: None
        try:
            dialog.download_selected()
            self.assertEqual(1, len(factory_calls))
            # Nach dem Fehler darf der nächste Versuch wieder anmelden.
            self.assertIsNotNone(dialog._cached_or_new_client())
            self.assertEqual(2, len(factory_calls))
        finally:
            ui_module._restore_stubs_for_tests()

class BackgroundCallTests(unittest.TestCase):
    """Netzaufrufe verlassen den GUI-Thread."""

    @staticmethod
    def _threaded_qt(ui_module):
        """QThread-Ersatz, der den Worker in einem ECHTEN Thread ausfuehrt."""

        class Signal:
            def __init__(self):
                self.slots = []

            def connect(self, slot):
                self.slots.append(slot)

            def emit(self, *args):
                for slot in self.slots:
                    slot(*args)

        class Thread:
            def __init__(self, parent=None):
                self.started = Signal()
                self.finished = Signal()
                self.worker_thread = None

            def start(self):
                self.worker_thread = threading.Thread(target=self._run)
                self.worker_thread.start()
                self.worker_thread.join(5)

            def _run(self):
                for slot in list(self.started.slots):
                    slot()

            def quit(self, *_args):
                pass

            def wait(self, *_args):
                return True

            def isRunning(self):
                return False

            def deleteLater(self):
                pass

        class Worker:
            """CallWorker-Ersatz mit echten Signalen (Qt-Stub hat keine)."""

            def __init__(self, call):
                self.call = call
                self.done = Signal()
                self.failed = Signal()

            def moveToThread(self, thread):
                self.thread = thread

            def run(self):
                try:
                    result = self.call()
                except Exception as exc:
                    self.failed.emit(exc)
                else:
                    self.done.emit(result)

        ui_module.common.QThread = Thread
        ui_module.auth_workers.CallWorker = Worker
        return Thread

    def _dialog(self, ui_module):
        rows = [{"title": "Der Schwarm", "authors": "A", "book_id": 1,
                 "tolino_id": "t-1", "status": "identical", "isbn": "111",
                 "explanation": "Gleich."}]
        dialog = ui_module.dialogs.InventoryDialog(rows, options={})
        dialog.table.selectionModel = lambda: type("Sel", (), {
            "selectedRows": staticmethod(lambda: [
                type("Idx", (), {"row": staticmethod(lambda: 0)})()])})()
        return dialog

    def test_pdf_content_changes_the_saved_epub_suffix_to_pdf(self):
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None,
            qt_stub=ui_base_tests.InventoryDialogBuildTests._qt_stub())
        self.addCleanup(ui_module._restore_stubs_for_tests)
        dialog = self._dialog(ui_module)
        dialog._download_path = "/tmp/chosen.epub"
        dialog._end_action = lambda: None
        written = {}

        class Handle:
            def __init__(self, path, mode):
                written["path"] = path
                written["mode"] = mode

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def write(self, data):
                written["data"] = data

        ui_module.dialogs.open = Handle
        ui_module.dialogs.info_dialog = lambda *args, **kwargs: None
        dialog._download_finished(b"%PDF-1.7 test")
        self.assertEqual("/tmp/chosen.pdf", written["path"])
        self.assertEqual(b"%PDF-1.7 test", written["data"])

    def test_the_download_leaves_the_gui_thread(self):
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None,
            qt_stub=ui_base_tests.InventoryDialogBuildTests._qt_stub())
        self.addCleanup(ui_module._restore_stubs_for_tests)
        self._threaded_qt(ui_module)
        seen = []
        path = os.path.join(tempfile.gettempdir(), "tolino-bg-test.epub")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        class Client:
            @staticmethod
            def download(tolino_id, **kwargs):
                seen.append(threading.current_thread())
                return b"data"

        dialog = self._dialog(ui_module)
        dialog.client_factory = lambda: Client()
        ui_module.dialogs.QFileDialog = type("FD", (), {
            "getSaveFileName": staticmethod(lambda *args: (path, ""))})
        ui_module.dialogs.info_dialog = lambda *args, **kwargs: None
        dialog.download_selected()
        self.assertEqual(1, len(seen))
        self.assertIsNot(threading.main_thread(), seen[0])
        self.assertTrue(os.path.exists(path))


    def test_download_action_is_offloaded_from_the_gui_thread(self):
        """Wächter: Netzarbeit beim Download bleibt im Worker-Thread."""
        source = _ui_source_text()
        body = source.split("    def download_selected(", 1)[1].split(
            "\n    def ", 1)[0]
        self.assertIn("_start_action", body)
        self.assertIn("self._download_finished", body)
        self.assertNotIn("lambda result: self._download_finished", body)
        self.assertIn("EPUB (*.epub);;PDF (*.pdf)", body)
        dialogs = source.split("    def _start_action(", 1)[1].split(
            "\n    def ", 1)[0]
        self.assertIn("start_call", dialogs)

    def test_ui_worker_result_signals_never_connect_lambdas(self):
        """Qt callbacks on worker signals must be bound GUI-object methods."""
        import ast
        base = Path(__file__).resolve().parents[1]
        signal_names = {"done", "failed", "valid", "invalid", "busy",
                        "completed", "ready"}
        violations = []
        for filename in ("ui_dialogs.py", "ui_dashboard.py"):
            tree = ast.parse((base / filename).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                function = node.func
                if not (isinstance(function, ast.Attribute)
                        and function.attr == "connect"
                        and isinstance(function.value, ast.Attribute)
                        and function.value.attr in signal_names):
                    continue
                if isinstance(node.args[0], ast.Lambda):
                    violations.append((filename, node.lineno,
                                       function.value.attr))
        self.assertEqual([], violations)
