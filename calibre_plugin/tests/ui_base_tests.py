"""Gemeinsame UI-Basis: Qt-Stubs, Toolbar und Vergleichsfenster."""

import os
import unittest
from ._support import load_ui_module, start_cdp_stubs, stop_cdp_stubs


# Modul-Hooks für den Einzellauf; die Gesamtsuite ruft sie zentral auf.
setUpModule = start_cdp_stubs
tearDownModule = stop_cdp_stubs


class ToolbarIconTests(unittest.TestCase):
    """Shared UI module loader for tests without a live Calibre install."""

    @staticmethod
    def _load_ui_module(get_icons_result, qt_stub=None):
        return load_ui_module(get_icons_result, qt_stub)

    def test_genesis_loads_the_bundled_icon_when_calibre_helper_returns_null(self):
        class Signal:
            def connect(self, _slot):
                pass

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
            plugin._keepalive_credentials = lambda: {}
            plugin._keepalive_persist = lambda *_args: None
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

    Qt wird dabei minimal, aber ehrlich nachgebildet (fehlende Methoden
    fallen als AttributeError auf). Damit sind Tippfehler und falsche
    Spaltenindizes sichtbar, auch ohne Calibre auf dem Rechner.
    """

    @staticmethod
    def _qt_stub():
        class Signal:
            def __init__(self):
                self.slots = []

            def connect(self, slot):
                self.slots.append(slot)

            def emit(self, *args):
                for slot in self.slots:
                    slot(*args)

        class Widget:
            def __init__(self, *args, **_kwargs):
                self.args = args
                self.tooltip = None

            def setToolTip(self, text):
                self.tooltip = text

            def setEnabled(self, value):
                self.enabled = value

            def setVisible(self, value):
                self.visible = value

            def isVisible(self):
                return getattr(self, "visible", True)

            def setWindowTitle(self, title):
                self.window_title = title

            def setMinimumSize(self, *args):
                self.minimum_size = args

            def setMinimumWidth(self, width):
                self.minimum_width = width

            def rect(self):
                return type("Rect", (), {"bottomLeft": lambda _self: None})()

            def mapToGlobal(self, position):
                return position

            def setWordWrap(self, value):
                self.word_wrap = value

            def setText(self, text):
                self._text = text

            def text(self):
                """Text des Widgets – auch wenn er beim Bau kam."""
                if hasattr(self, "_text"):
                    return self._text
                return self.args[0] if self.args else ""

            def setLayout(self, layout):
                self.layout = layout

            def setChecked(self, value):
                self.checked = bool(value)

            def isChecked(self):
                return getattr(self, "checked", False)

            def setSelectionBehavior(self, value):
                self.selection_behavior = value

            def setRowCount(self, count):
                self.row_count = count

            def setCellWidget(self, row, column, widget):
                self.cell_widgets = getattr(self, "cell_widgets", [])
                self.cell_widgets.append((row, column, widget))

            def setItem(self, row, column, item):
                self.items = getattr(self, "items", {})
                self.items[(row, column)] = item.text

            def resizeColumnsToContents(self):
                self.resized = True

            def setColumnHidden(self, column, hidden):
                self.hidden = getattr(self, "hidden", {})
                self.hidden[column] = hidden

        class Layout:
            def __init__(self, *args, **_kwargs):
                self.children = []

            def addWidget(self, *args):
                self.children.append(args)

            def addLayout(self, layout):
                self.children.append(layout)

            def addRow(self, *args):
                self.children.append(args)

            def addStretch(self, *args):
                pass

        class Header:
            def __init__(self):
                self.sectionClicked = Signal()

            def setSortIndicatorShown(self, shown):
                self.shown = shown

            def setSortIndicator(self, column, order):
                self.indicator = (column, order)

        class Table(Widget):
            SelectRows = "SelectRows"
            ExtendedSelection = "ExtendedSelection"

            def setSelectionMode(self, mode):
                self.selection_mode = mode

            def __init__(self, rows, columns):
                Widget.__init__(self)
                self.columns = columns
                self.header_items = {}

            def setHorizontalHeaderLabels(self, labels):
                self.headers = list(labels)

            def horizontalHeader(self):
                self.header = Header()
                return self.header

            def horizontalHeaderItem(self, index):
                return self.header_items.setdefault(index, Widget())

        class Item:
            def __init__(self, text):
                self.text = text

        class Dialog(Widget):
            def __init__(self, parent=None):
                Widget.__init__(self)
                self.parent = parent

            def exec(self):
                return 0

            def accept(self):
                pass

            def reject(self):
                pass

        class CheckBox(Widget):
            def __init__(self, text=""):
                Widget.__init__(self, text)
                self.stateChanged = Signal()
                self.toggled = Signal()
                # Wie in Qt: nach dem Umschalten kommt clicked.
                self.clicked = Signal()

        class Button(Widget):
            def __init__(self, text=""):
                Widget.__init__(self, text)
                self.clicked = Signal()

        class SelectRows:
            pass

        class Object:
            def __init__(self, *args, **_kwargs):
                pass

            def moveToThread(self, thread):
                self.thread = thread

            def deleteLater(self):
                pass

        class Thread:
            """Synchroner QThread-Ersatz: `start()` fuehrt den Worker aus.

            So bleiben die Dialogtests ohne echtes Qt lesbar. Dass der
            Aufruf im Betrieb in einem anderen Thread laeuft, belegt
            `BackgroundCallTests` mit einer echten Thread-Fassung.
            """

            def __init__(self, parent=None):
                self.started = Signal()
                self.finished = Signal()

            def start(self):
                for slot in list(self.started.slots):
                    slot()

            def quit(self, *_args):
                self.stopped = True

            def wait(self, *_args):
                return True

            def isRunning(self):
                return False

            def deleteLater(self):
                self.deleted = True

        class SignalDescriptor:
            """pyqtSignal-Ersatz: je Instanz ein eigenes Signal."""

            def __init__(self, *args, **_kwargs):
                self.name = None

            def __set_name__(self, owner, name):
                self.name = name

            def __get__(self, instance, owner=None):
                if instance is None:
                    return self
                store = instance.__dict__.setdefault("_signals", {})
                return store.setdefault(self.name, Signal())

        qt = type("Qt", (), {"AscendingOrder": 0, "DescendingOrder": 1,
                             "SelectRows": SelectRows})
        return {
            "Qt": qt,
            "QDialog": Dialog,
            "QVBoxLayout": Layout,
            "QHBoxLayout": Layout,
            "QLabel": Widget,
            "QPushButton": Button,
            "QCheckBox": CheckBox,
            "QGroupBox": Widget,
            "QTableWidget": Table,
            "QTableWidgetItem": Item,
            "QMenu": type("Menu", (), {
                "__init__": lambda self, parent=None: setattr(self, "actions", []),
                "addAction": lambda self, label: self.actions.append(label) or label,
                "exec": lambda self, _position=None: None,
            }),
            "QObject": Object,
            "QThread": Thread,
            "pyqtSignal": SignalDescriptor,
        }

    def _build(self, options):
        ui_module = ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None, qt_stub=self._qt_stub())
        rows = [
            {"title": "Zulu", "authors": "Z", "status": "identical",
             "book_id": 1, "tolino_id": "c1", "isbn": "111",
             "explanation": "Titel stimmt mit der Cloud überein."},
            {"title": "Alpha", "authors": "A", "status": "new_in_calibre",
             "book_id": 2, "tolino_id": "", "isbn": "222",
             "has_preferred_format": True,
             "explanation": "Noch nicht in der Cloud – kann hochgeladen "
             "werden."},
        ]
        return ui_module, ui_module.dialogs.InventoryDialog(rows, options=options)

    def test_constructor_writes_every_column_in_the_right_order(self):
        ui_module, dialog = self._build({"collections": True, "automatic": 1})
        try:
            self.assertEqual(list(ui_module.dialogs.InventoryDialog.HEADERS),
                             dialog.table.headers)
            # Tooltips versprechen Strg-/Shift-Mehrfachauswahl.
            self.assertEqual("ExtendedSelection",
                             dialog.table.selection_mode)
            cells = {(row, column): widget
                     for row, column, widget in dialog.table.cell_widgets}
            for index, row in enumerate(dialog.rows):
                in_cloud = bool(row["tolino_id"])
                # Drei Auswahlfelder je Buch ...
                self.assertIs(cells[(index, 0)], dialog.checks[index])
                self.assertIs(cells[(index, 1)], dialog.meta_checks[index])
                self.assertIs(cells[(index, 2)], dialog.read_checks[index])
                # ... die zwei Orts-Spalten mit je einem Häkchen ...
                self.assertEqual("✓" if row["book_id"] is not None else "",
                                 dialog.table.items[(index, 3)])
                self.assertEqual("✓" if in_cloud else "",
                                 dialog.table.items[(index, 4)])
                # ... und danach die Textspalten.
                self.assertEqual(row["title"], dialog.table.items[(index, 5)])
                self.assertEqual(row["authors"], dialog.table.items[(index, 6)])
                self.assertEqual(row["explanation"],
                                 dialog.table.items[(index, 7)])
                self.assertEqual(row["isbn"], dialog.table.items[(index, 8)])
                self.assertEqual(row["tolino_id"],
                                 dialog.table.items[(index, 9)])
                self.assertEqual(row.get("uuid", ""),
                                 dialog.table.items[(index, 10)])
                # Metadaten und Lese-Marke gehen nur für Bücher, die schon in
                # der Cloud liegen; Hochladen nur mit Calibre-Buch.
                upload = dialog.checks[index]
                metadata = dialog.meta_checks[index]
                mark_read = dialog.read_checks[index]
                self.assertTrue(upload.enabled)
                self.assertEqual(in_cloud, metadata.enabled)
                self.assertEqual(in_cloud, mark_read.enabled)
                # Vorbelegt ist, was der Vergleich vorschlägt; die Lese-Marke
                # bleibt unangetastet.
                self.assertEqual(bool(row["upload"]), upload.isChecked())
                self.assertEqual(bool(row["metadata"]), metadata.isChecked())
                self.assertFalse(mark_read.isChecked())
            # Nur die technischen Spalten sind zunächst verborgen.
            self.assertEqual({8: True, 9: True, 10: True}, dialog.table.hidden)
            self.assertIn("Sammlungen in tolino übernommen",
                          dialog.note.text())
            self.assertIn("1 offen", dialog.note.text())
            self.assertNotIn("Zusätzliche Aktionen:",
                              dialog.note.text())
            self.assertIn("1 Upload", dialog.summary.text())
            self.assertIn("0 Metadaten", dialog.summary.text())
            self.assertIn("0 Lesestatus", dialog.summary.text())
            self.assertEqual("Abbrechen", dialog.cancel.text())
            self.assertEqual("Synchronisierung starten (1)",
                             dialog.confirm.text())
        finally:
            ui_module._restore_stubs_for_tests()

    def test_toggle_switches_the_technical_columns(self):
        ui_module, dialog = self._build({"collections": True, "automatic": 1})
        try:
            dialog._toggle_technical_columns(True)
            self.assertEqual({8: False, 9: False, 10: False}, dialog.table.hidden)
            dialog._toggle_technical_columns(False)
            self.assertEqual({8: True, 9: True, 10: True}, dialog.table.hidden)
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

        row_index = next(index for index, row in enumerate(dialog.rows)
                         if row.get("tolino_id"))

        class Index:
            @staticmethod
            def row():
                return row_index

        class Selection:
            @staticmethod
            def selectedRows():
                return [Index()]

        class Client:
            @staticmethod
            def download(tolino_id, **kwargs):
                events.append(("download", kwargs))
                return b"data"

        class FileDialog:
            @staticmethod
            def getSaveFileName(*args):
                events.append("save")
                return path, ""

        dialog.table.selectionModel = lambda: Selection()
        dialog.client_factory = lambda: Client()
        ui_module.dialogs.QFileDialog = FileDialog
        ui_module.dialogs.info_dialog = lambda *args, **kwargs: events.append("info")
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
            lambda *args, **kwargs: None, qt_stub=self._qt_stub())
        row = {"title": "Zulu", "authors": "Z", "status": "identical",
               "book_id": 1, "tolino_id": "c1", "isbn": "111",
               "explanation": "Gleich."}
        try:
            dialog = ui_module.dialogs.InventoryDialog([dict(row)], options={})
            self.assertFalse(dialog.confirm.enabled)
            dialog = ui_module.dialogs.InventoryDialog(
                [dict(row)], options={"automatic": 1})
            self.assertTrue(dialog.confirm.enabled)
        finally:
            ui_module._restore_stubs_for_tests()

    def test_group_filter_hides_other_groups_without_losing_rows(self):
        """Der Gruppenfilter versteckt nur Zeilen, die Auswahl bleibt."""
        ui_module, dialog = self._build({})
        try:
            groups = [row["group"] for row in dialog.rows]
            self.assertIn("action", groups)
            self.assertIn("unchanged", groups)
            hidden = {}
            dialog.table.setRowHidden = (
                lambda index, value: hidden.__setitem__(index, value))
            dialog.group_filter = type(
                "Combo", (), {"currentData": lambda self: "action"})()
            dialog._apply_group_filter()
            self.assertEqual(len(groups), len(dialog.rows))
            for index, group in enumerate(groups):
                self.assertEqual(group != "action", hidden[index])
        finally:
            ui_module._restore_stubs_for_tests()
