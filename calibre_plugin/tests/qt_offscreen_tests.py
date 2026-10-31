"""Real-PyQt6 offscreen smoke test for InventoryDialog (Calibre APIs stubbed)."""

from ..comparison import ReadState
import importlib.util
import os
import sys
import types
import unittest

if importlib.util.find_spec("PyQt6") is None:  # die CI installiert PyQt6
    raise unittest.SkipTest("PyQt6 ist nicht installiert")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QObject, QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                             QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                             QHeaderView, QInputDialog, QLabel, QLineEdit,
                             QMenu, QMessageBox, QProgressBar, QPushButton,
                             QStyle, QStyledItemDelegate,
                             QStyleOptionViewItem, QTableWidget,
                             QTableWidgetItem, QVBoxLayout)


def _read_ids(dialog, finished):
    """tolino-IDs der gewählten Lesestatus-Änderungen (True = gelesen)."""

    return [tolino_id for tolino_id, _book, state
            in dialog.selected_read_changes() if state is finished]


def _install_calibre_qt_shim():
    """Expose PyQt6 widgets as Calibre's qt.core names for this focused test."""
    qt = types.ModuleType("qt")
    core = types.ModuleType("qt.core")
    names = {
        "QObject": QObject, "QThread": QThread, "QCheckBox": QCheckBox,
        "QComboBox": QComboBox, "QDialog": QDialog,
        "QFileDialog": QFileDialog, "QFormLayout": QFormLayout,
        "QGroupBox": QGroupBox, "QHBoxLayout": QHBoxLayout,
        "QHeaderView": QHeaderView, "QInputDialog": QInputDialog,
        "QLabel": QLabel,
        "QLineEdit": QLineEdit, "QMenu": QMenu, "QMessageBox": QMessageBox,
        "QProgressBar": QProgressBar, "QPushButton": QPushButton,
        "QTableWidget": QTableWidget, "QTableWidgetItem": QTableWidgetItem,
        "QVBoxLayout": QVBoxLayout,
        "QStyledItemDelegate": QStyledItemDelegate,
        "QStyle": QStyle, "QStyleOptionViewItem": QStyleOptionViewItem,
        "QEvent": QEvent, "Qt": Qt,
        "pyqtSignal": pyqtSignal,
    }
    for name, value in names.items():
        setattr(core, name, value)
    qt.core = core
    sys.modules["qt"] = qt
    sys.modules["qt.core"] = core

    calibre = types.ModuleType("calibre")
    gui2 = types.ModuleType("calibre.gui2")
    gui2.error_dialog = lambda *args, **kwargs: None
    gui2.info_dialog = lambda *args, **kwargs: None
    calibre.gui2 = gui2
    sys.modules["calibre"] = calibre
    sys.modules["calibre.gui2"] = gui2


_install_calibre_qt_shim()
from calibre_plugin.ui_dialogs import InventoryDialog  # noqa: E402


class InventoryDialogQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_calibre_read_value_is_display_only(self):
        dialog = InventoryDialog([{
            "title": "Calibre marks read", "authors": "Author",
            "status": "identical", "group": "unchanged", "book_id": 1,
            "tolino_id": "cloud-1", "read_column": True,
            "read": ReadState(cloud=False, local=True),
        }])
        check = dialog.cells["read"][0]
        self.assertFalse(check.isChecked())
        self.assertEqual([], _read_ids(dialog, True))
        self.assertEqual([], _read_ids(dialog, False))
        self.assertIn("Calibre-Lesestatus: gelesen", dialog.table.item(0, 7).text())

        check.setChecked(True)
        self.assertEqual(["cloud-1"], _read_ids(dialog, True))
        self.assertEqual([], _read_ids(dialog, False))

        check.setChecked(False)
        self.assertEqual([], _read_ids(dialog, True))
        self.assertEqual([], _read_ids(dialog, False))
        dialog.close()

    def test_title_column_stays_wider_than_author_column(self):
        dialog = InventoryDialog([{
            "title": "Der Lehrling des Feldschers und die Reise durch das Königreich",
            "authors": "Christoph Magnus Freiherr von und zu Hohenstein-Waldburg",
            "status": "identical", "group": "unchanged", "book_id": 1,
            "tolino_id": "cloud-1", "isbn": "9781234567890",
            "read_column": True,
            "read": ReadState(cloud=False, local=False),
        }])
        dialog.show()
        self.app.processEvents()
        for width in (640, 760, 960):
            dialog.resize(width, 560)
            self.app.processEvents()
            header = dialog.table.horizontalHeader()
            self.assertGreater(header.sectionSize(5), header.sectionSize(6))
            self.assertGreaterEqual(header.sectionSize(5), 100)
        dialog.close()

    def test_explicit_unread_selection_changes_only_the_cloud_target(self):
        dialog = InventoryDialog([{
            "title": "Cloud marks read", "authors": "Author",
            "status": "identical", "group": "unchanged", "book_id": 1,
            "tolino_id": "cloud-2", "read_column": True,
            "read": ReadState(cloud=True, local=False),
        }])
        dialog.cells["read"][0].setChecked(False)
        self.assertEqual([], _read_ids(dialog, True))
        self.assertEqual(["cloud-2"], _read_ids(dialog, False))
        self.assertFalse(dialog.rows[0]["read"].local)
        dialog.close()


if __name__ == "__main__":
    unittest.main()
