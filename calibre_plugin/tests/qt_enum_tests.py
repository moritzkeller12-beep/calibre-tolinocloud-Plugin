"""Qt-6-Enums: Calibre 6 und neuer kennen keine Kurzformen wie QHeaderView.Stretch.

Feldbefund (Calibre 9.17): «type object 'QHeaderView' has no attribute
'Stretch' (Code E99)» beim Bibliotheksvergleich.
"""
import enum
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from . import ui_base_tests
from ._support import ui_module_for

# Klassenattribute in Grossschreibung hinter einem Qt-Namen sind Enum-Werte
# (Methoden folgt eine Klammer, Enum-Klassen ein weiterer Punkt).
_SHORT_ENUM = re.compile(r"\b(Qt|Q[A-Z]\w*)\.([A-Z]\w*)\b(?!\s*[.(])")


def _qt6_stub():
    """Qt-Stub, der wie PyQt6 nur die vollständigen Enum-Namen kennt."""
    stub = ui_base_tests.InventoryDialogBuildTests._qt_stub()
    base_table = stub["QTableWidget"]

    class SelectionBehavior(enum.Enum):
        SelectRows = 1

    class SelectionMode(enum.Enum):
        ExtendedSelection = 3

    class SortOrder(enum.Enum):
        AscendingOrder = 0
        DescendingOrder = 1

    class ResizeMode(enum.Enum):
        Stretch = 1
        ResizeToContents = 3

    class Table(base_table):
        pass

    # Die Kurzformen des (je Aufruf neuen) Basis-Stubs entfernen: genau die
    # fehlen unter Qt 6.
    del base_table.SelectRows
    del base_table.ExtendedSelection
    Table.SelectionBehavior = SelectionBehavior
    Table.SelectionMode = SelectionMode

    original_header = Table.horizontalHeader

    def horizontal_header(self):
        header = original_header(self)
        header.modes = getattr(self, "modes", {})
        self.modes = header.modes
        header.setSectionResizeMode = (
            lambda column, mode: header.modes.__setitem__(column, mode))
        return header

    Table.horizontalHeader = horizontal_header
    stub["QTableWidget"] = Table
    stub["Qt"] = type("Qt", (), {"SortOrder": SortOrder})
    stub["QHeaderView"] = type("QHeaderView", (), {"ResizeMode": ResizeMode})
    return stub, ResizeMode


class Qt6EnumTests(unittest.TestCase):
    def test_no_short_enum_names_in_the_plugin_sources(self):
        base = Path(__file__).resolve().parent.parent
        offenders = []
        for path in sorted(base.glob("*.py")):
            for number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                for match in _SHORT_ENUM.finditer(code):
                    offenders.append("%s:%d %s" % (path.name, number,
                                                   match.group(0)))
        self.assertEqual([], offenders,
                         "Qt-6 kennt nur Scope.Name, z. B. "
                         "qt_enum(QHeaderView, 'ResizeMode', 'Stretch')")

    def test_the_comparison_window_builds_with_qt6_enums(self):
        stub, resize_mode = _qt6_stub()
        ui_module = ui_module_for(self, qt_stub=stub)
        warnings = []
        rows = [{"title": "Eins", "authors": "A", "status": "identical",
                 "book_id": 1, "tolino_id": "t-1"}]
        with patch.object(ui_module.dialogs, "log_warning", warnings.append):
            dialog = ui_module.dialogs.InventoryDialog(rows)
        self.assertEqual([], warnings)
        self.assertEqual(resize_mode.Stretch, dialog.table.modes[5])
        self.assertEqual(resize_mode.ResizeToContents, dialog.table.modes[0])
        self.assertEqual("SelectRows", dialog.table.selection_behavior.name)
        self.assertEqual("ExtendedSelection", dialog.table.selection_mode.name)

    def test_qt_enum_prefers_the_scoped_form_and_falls_back(self):
        ui_module = ui_module_for(self)
        qt_enum = ui_module.common.qt_enum

        class Scoped:
            class ResizeMode:
                Stretch = "scoped"
            Stretch = "short"

        class ShortOnly:
            Stretch = "short"

        self.assertEqual("scoped", qt_enum(Scoped, "ResizeMode", "Stretch"))
        self.assertEqual("short", qt_enum(ShortOnly, "ResizeMode", "Stretch"))
        with self.assertRaises(AttributeError):
            qt_enum(object, "ResizeMode", "Stretch")

    def test_an_accepted_dialog_counts_as_int_and_as_enum(self):
        ui_module = ui_module_for(self)

        class DialogCode(enum.Enum):
            Rejected = 0
            Accepted = 1

        with patch.object(ui_module.dashboard, "QDialog",
                          type("QDialog", (), {"DialogCode": DialogCode})):
            self.assertTrue(ui_module.dashboard._accepted(1))
            self.assertTrue(ui_module.dashboard._accepted(DialogCode.Accepted))
            self.assertFalse(ui_module.dashboard._accepted(0))
