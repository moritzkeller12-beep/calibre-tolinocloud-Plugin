"""Real-PyQt6 offscreen smoke test for InventoryDialog (Calibre APIs stubbed)."""
import importlib.util
import os
import sys
import types
import unittest

if importlib.util.find_spec("PyQt6") is None:  # die CI installiert PyQt6
    raise unittest.SkipTest("PyQt6 ist nicht installiert")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                             QComboBox, QDialog, QFileDialog, QFormLayout,
                             QGroupBox, QHBoxLayout, QHeaderView, QInputDialog,
                             QLabel,
                             QLineEdit, QMenu, QMessageBox, QProgressBar,
                             QPushButton, QTableWidget, QTableWidgetItem,
                             QVBoxLayout)


class _TableWidget(QTableWidget):
    SelectRows = QAbstractItemView.SelectionBehavior.SelectRows
    ExtendedSelection = QAbstractItemView.SelectionMode.ExtendedSelection


class _HeaderView(QHeaderView):
    Stretch = QHeaderView.ResizeMode.Stretch
    ResizeToContents = QHeaderView.ResizeMode.ResizeToContents


class _QtCompat:
    AscendingOrder = Qt.SortOrder.AscendingOrder
    DescendingOrder = Qt.SortOrder.DescendingOrder


def _install_calibre_qt_shim():
    """Expose PyQt6 widgets as Calibre's qt.core names for this focused test."""
    qt = types.ModuleType("qt")
    core = types.ModuleType("qt.core")
    names = {
        "QObject": QObject, "QThread": QThread, "QCheckBox": QCheckBox,
        "QComboBox": QComboBox, "QDialog": QDialog,
        "QFileDialog": QFileDialog, "QFormLayout": QFormLayout,
        "QGroupBox": QGroupBox, "QHBoxLayout": QHBoxLayout,
        "QHeaderView": _HeaderView, "QInputDialog": QInputDialog,
        "QLabel": QLabel,
        "QLineEdit": QLineEdit, "QMenu": QMenu, "QMessageBox": QMessageBox,
        "QProgressBar": QProgressBar, "QPushButton": QPushButton,
        "QTableWidget": _TableWidget, "QTableWidgetItem": QTableWidgetItem,
        "QVBoxLayout": QVBoxLayout, "Qt": _QtCompat,
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
            "tolino_id": "cloud-1", "read_in_cloud": False,
            "read_local": True, "read_column": True,
        }])
        check = dialog.read_checks[0]
        self.assertFalse(check.isChecked())
        self.assertEqual([], dialog.selected_read_targets())
        self.assertEqual([], dialog.selected_read_unmarks())
        self.assertIn("Calibre-Lesestatus: gelesen", dialog.table.item(0, 7).text())

        check.setChecked(True)
        self.assertEqual(["cloud-1"], dialog.selected_read_targets())
        self.assertEqual([], dialog.selected_read_unmarks())

        check.setChecked(False)
        self.assertEqual([], dialog.selected_read_targets())
        self.assertEqual([], dialog.selected_read_unmarks())
        dialog.close()

    def test_explicit_unread_selection_changes_only_the_cloud_target(self):
        dialog = InventoryDialog([{
            "title": "Cloud marks read", "authors": "Author",
            "status": "identical", "group": "unchanged", "book_id": 1,
            "tolino_id": "cloud-2", "read_in_cloud": True,
            "read_local": False, "read_column": True,
        }])
        dialog.read_checks[0].setChecked(False)
        self.assertEqual([], dialog.selected_read_targets())
        self.assertEqual(["cloud-2"], dialog.selected_read_unmarks())
        self.assertFalse(dialog.rows[0]["read_local"])
        dialog.close()


if __name__ == "__main__":
    unittest.main()
