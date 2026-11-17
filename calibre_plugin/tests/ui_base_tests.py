"""Gemeinsame UI-Basis: Qt-Stubs, Toolbar und Vergleichsfenster."""

from ..comparison import ReadState
import os
import unittest
from ._support import (FakeSignal, cell_text, item_checked, load_ui_module,
                       real_qt_stub, start_cdp_stubs, stop_cdp_stubs)


# Der Einzellauf richtet die CDP-Stubs selbst ein.
setUpModule = start_cdp_stubs
tearDownModule = stop_cdp_stubs


class ToolbarIconTests(unittest.TestCase):
    """Shared UI module loader for tests without a live Calibre install."""


    @staticmethod
    def _load_ui_module(get_icons_result, qt_stub=None):
        return load_ui_module(get_icons_result, qt_stub)

    def test_genesis_loads_the_bundled_icon_when_calibre_helper_returns_null(self):
        Signal = FakeSignal

        class Action:
            triggered = Signal()

            def setIcon(self, icon):
                self.icon = icon

        class Plugin:
            pass

        icon_module = self._load_ui_module(None)
        icon_module._test_resources = {
            "images/tolino_cloud_sync.png": b"png-data"}
        try:
            plugin = Plugin()
            plugin.qaction = Action()
            plugin.show_dashboard = lambda: None
            plugin.load_resources = lambda _names: icon_module._test_resources
            icon = icon_module.TolinoSyncAction._toolbar_icon(plugin)
            self.assertIsNotNone(icon)
            self.assertFalse(icon.isNull())
        finally:
            icon_module._restore_stubs_for_tests()

    def test_calibre_entry_does_not_load_the_dashboard_or_workers(self):
        ui_module = self._load_ui_module(None)
        try:
            loaded = ui_module._loaded_by_entry_for_tests
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertFalse({
            "calibre_plugin.ui_common",
            "calibre_plugin.ui_dashboard",
            "calibre_plugin.ui_dialogs",
            "calibre_plugin.ui_workers",
            "calibre_plugin.config",
            "calibre_plugin.tolino",
        } & loaded)

class InventoryDialogBuildTests(unittest.TestCase):
    """Der Aufbau des Bestandsvergleichs wird vollständig durchlaufen.

    Qt ist echtes PyQt6 im Offscreen-Modus: Zellen, Flags, Breiten und
    Delegates werden so geprüft, wie Calibre sie später sieht. Doubles
    gibt es nur an den modalen Grenzen (siehe ``_support.real_qt_stub``).
    """

    def _build(self, options):
        """Echtes PyQt6 (offscreen): derselbe Aufbau wie im Betrieb."""
        ui_module = ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None, qt_stub=real_qt_stub())
        rows = [
            {"title": "Zulu", "authors": "Z", "status": "identical",
             "book_id": 1, "tolino_id": "c1", "isbn": "111",
             "read": ReadState(), "group": "unchanged",
             "explanation": "Titel stimmt mit der Cloud überein."},
            {"title": "Alpha", "authors": "A", "status": "new_in_calibre",
             "book_id": 2, "tolino_id": "", "isbn": "222",
             "has_preferred_format": True, "read": ReadState(),
             "group": "action",
             "explanation": "Noch nicht in der Cloud – kann hochgeladen "
             "werden."},
        ]
        return ui_module, ui_module.dialogs.InventoryDialog(rows, options=options)

    def test_constructor_writes_every_column_in_the_right_order(self):
        ui_module, dialog = self._build({"automatic": 1})
        dialogs, table = ui_module.dialogs, dialog.table
        try:
            self.assertEqual(
                list(dialogs.InventoryDialog.HEADERS),
                [table.horizontalHeaderItem(index).text()
                 for index in range(table.columnCount())])
            # Tooltips versprechen Strg-/Shift-Mehrfachauswahl.
            self.assertEqual(
                dialogs.QTableWidget.SelectionMode.ExtendedSelection,
                table.selectionMode())
            enabled = dialogs.Qt.ItemFlag.ItemIsEnabled
            for index, row in enumerate(dialog.rows):
                in_cloud = bool(row["tolino_id"])
                # Die zwei Orts-Spalten mit je einem Häkchen ...
                self.assertEqual("✓" if row["book_id"] is not None else "",
                                 cell_text(table, index, 3))
                self.assertEqual("✓" if in_cloud else "",
                                 cell_text(table, index, 4))
                # ... und danach die Textspalten.
                self.assertEqual(row["title"], cell_text(table, index, 5))
                self.assertEqual(row["authors"], cell_text(table, index, 6))
                self.assertEqual(row["explanation"],
                                 cell_text(table, index, 7))
                self.assertEqual("", cell_text(table, index, 8))
                self.assertEqual(row["isbn"], cell_text(table, index, 9))
                self.assertEqual(row["tolino_id"],
                                 cell_text(table, index, 10))
                self.assertEqual(row.get("uuid", ""),
                                 cell_text(table, index, 11))
                # Metadaten und Lese-Marke gehen nur für Bücher, die schon in
                # der Cloud liegen; Hochladen nur mit Calibre-Buch.
                upload = table.item(index, 0)
                metadata = table.item(index, 1)
                mark_read = table.item(index, 2)
                self.assertTrue(upload.flags() & enabled)
                self.assertEqual(in_cloud,
                                 bool(metadata.flags() & enabled))
                self.assertEqual(in_cloud,
                                 bool(mark_read.flags() & enabled))
                # Vorbelegt ist, was der Vergleich vorschlägt; die Lese-Marke
                # bleibt unangetastet.
                self.assertEqual(bool(row["upload"]), item_checked(upload))
                self.assertEqual(bool(row["metadata"]),
                                 item_checked(metadata))
                self.assertFalse(item_checked(mark_read))
            self.assertIsInstance(dialog._check_delegate,
                                  dialogs._CellCheckDelegate)
            for column in (0, 1, 2):
                self.assertIs(dialog._check_delegate,
                              table.itemDelegateForColumn(column))
            self.assertEqual({0: 80, 1: 100, 2: 80, 3: 80, 4: 80},
                             {column: table.columnWidth(column)
                              for column in (0, 1, 2, 3, 4)})
            header = table.horizontalHeader()
            interactive = dialogs.QHeaderView.ResizeMode.Interactive
            for column in (0, 3, 5, 6):
                self.assertEqual(interactive,
                                 header.sectionResizeMode(column))
            self.assertEqual(160, table.columnWidth(5))
            self.assertEqual(90, table.columnWidth(6))
            self.assertFalse(dialog.testAttribute(
                dialogs.Qt.WidgetAttribute.WA_QuitOnClose))
            # Nur die technischen Spalten sind zunächst verborgen.
            self.assertEqual({9: True, 10: True, 11: True},
                             {column: table.isColumnHidden(column)
                              for column in (9, 10, 11)})
            self.assertIn("Sammlungen in tolino übernommen",
                          dialog.note.text())
            self.assertIn("1 offen", dialog.note.text())
            self.assertNotIn("Zusätzliche Aktionen:", dialog.note.text())
            self.assertIn("1 Upload", dialog.summary.text())
            self.assertIn("0 Metadaten", dialog.summary.text())
            self.assertIn("0 Lesestatus", dialog.summary.text())
            self.assertEqual("Abbrechen", dialog.cancel.text())
            self.assertEqual("Synchronisierung starten (1)",
                             dialog.confirm.text())
            # Derselbe Dialog: der Knopf blendet nur die technischen Spalten
            # um und laesst die Zeilen stehen.
            dialog._toggle_technical_columns(True)
            self.assertEqual({9: False, 10: False, 11: False},
                             {column: table.isColumnHidden(column)
                              for column in (9, 10, 11)})
            dialog._toggle_technical_columns(False)
            self.assertEqual({9: True, 10: True, 11: True},
                             {column: table.isColumnHidden(column)
                              for column in (9, 10, 11)})
            self.assertIn("Sammlungen in tolino übernommen",
                          dialog.note.text())
            self.assertIn("1 offen", dialog.note.text())
        finally:
            ui_module._restore_stubs_for_tests()

    def test_download_asks_for_the_path_before_fetching(self):
        """Ein Abbruch im Dateidialog darf keinen Download ausloesen."""
        ui_module, dialog = self._build({})
        events = []
        path = "/tmp/tolino-download-test.epub"

        class Client:
            @staticmethod
            def download(tolino_id, **kwargs):
                events.append(("download", kwargs))
                return b"PK\x03\x04data"

        class FileDialog:
            @staticmethod
            def getSaveFileName(*args):
                events.append("save")
                return path, ""

        row_index = next(index for index, row in enumerate(dialog.rows)
                         if row.get("tolino_id"))
        dialog.table.selectRow(row_index)
        dialog.client_factory = lambda: Client()
        ui_module.dialogs.QFileDialog = FileDialog
        ui_module.dialogs.info_dialog = (
            lambda *args, **kwargs: events.append("info"))
        try:
            dialog.download_selected()
            self.assertEqual(["save", ("download", {}), "info"], events)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
            ui_module._restore_stubs_for_tests()

    def test_zero_actions_disable_the_start_button(self):
        """Ohne Auswahl und ohne Automatik gibt es nichts zu starten."""
        ui_module = ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None, qt_stub=real_qt_stub())
        row = {"title": "Zulu", "authors": "Z", "status": "identical",
               "book_id": 1, "tolino_id": "c1", "isbn": "111",
               "read": ReadState(), "explanation": "Gleich."}
        try:
            dialog = ui_module.dialogs.InventoryDialog([dict(row)], options={})
            self.assertFalse(dialog.confirm.isEnabled())
            dialog = ui_module.dialogs.InventoryDialog(
                [dict(row)], options={"automatic": 1})
            self.assertTrue(dialog.confirm.isEnabled())
        finally:
            ui_module._restore_stubs_for_tests()

    def test_group_filter_hides_other_groups_without_losing_rows(self):
        """Der Gruppenfilter versteckt nur Zeilen, die Auswahl bleibt."""
        ui_module, dialog = self._build({})
        try:
            groups = [row["group"] for row in dialog.rows]
            self.assertIn("action", groups)
            self.assertIn("unchanged", groups)
            dialog.group_filter = type(
                "Combo", (), {"currentData": lambda self: "action"})()
            dialog._apply_group_filter()
            self.assertEqual(len(groups), len(dialog.rows))
            for index, group in enumerate(groups):
                self.assertEqual(group != "action",
                                 dialog.table.isRowHidden(index))
        finally:
            ui_module._restore_stubs_for_tests()
