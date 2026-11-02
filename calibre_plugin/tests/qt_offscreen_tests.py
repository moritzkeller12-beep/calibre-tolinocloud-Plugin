"""Real-PyQt6 offscreen smoke test for InventoryDialog (Calibre APIs stubbed)."""

from ..comparison import ReadState
from ._support import _read_ids
import importlib.util
import os
import sys
import types
import unittest

if importlib.util.find_spec("PyQt6") is None:  # die CI installiert PyQt6
    raise unittest.SkipTest("PyQt6 ist nicht installiert")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QObject, QPoint, QThread, Qt, pyqtSignal
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                             QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                             QHeaderView, QInputDialog, QLabel, QLineEdit,
                             QMenu, QMessageBox, QProgressBar, QPushButton,
                             QProxyStyle, QStyle, QStyledItemDelegate,
                             QStyleOptionViewItem, QTableWidget,
                             QTableWidgetItem, QVBoxLayout)


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
from calibre_plugin.ui_dialogs import (  # noqa: E402
    InventoryDialog, _CenteredCheckDelegate)


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
        check = dialog.table.item(0, 2)
        self.assertEqual(Qt.CheckState.Unchecked, check.checkState())
        self.assertEqual([], _read_ids(dialog, True))
        self.assertEqual([], _read_ids(dialog, False))
        self.assertEqual(0, dialog._selection_counts()["work"])
        self.assertIn("Calibre-Lesestatus: gelesen", dialog.table.item(0, 7).text())

        check.setCheckState(Qt.CheckState.Checked)
        self.assertEqual(["cloud-1"], _read_ids(dialog, True))
        self.assertEqual([], _read_ids(dialog, False))

        check.setCheckState(Qt.CheckState.Unchecked)
        self.assertEqual([], _read_ids(dialog, True))
        self.assertEqual([], _read_ids(dialog, False))
        dialog.close()

    def test_a_click_anywhere_in_the_check_cell_toggles_it(self):
        dialog = InventoryDialog([{
            "title": "Read me", "authors": "Author",
            "status": "identical", "group": "unchanged", "book_id": 1,
            "tolino_id": "cloud-3", "read": ReadState(cloud=False),
        }])
        delegate = dialog.table.itemDelegateForColumn(2)
        model, index = dialog.table.model(), dialog.table.model().index(0, 2)

        class Click:
            @staticmethod
            def type():
                return QEvent.Type.MouseButtonRelease

            @staticmethod
            def button():
                return Qt.MouseButton.LeftButton

        self.assertTrue(delegate.editorEvent(
            Click(), model, QStyleOptionViewItem(), index))
        self.assertEqual(Qt.CheckState.Checked,
                         dialog.table.item(0, 2).checkState())
        self.assertEqual(["cloud-3"], _read_ids(dialog, True))
        dialog.close()

    def test_a_real_click_anywhere_in_the_check_cell_toggles_it(self):
        dialog = InventoryDialog([{
            "title": "Read me", "authors": "Author",
            "status": "identical", "group": "unchanged", "book_id": 1,
            "tolino_id": "cloud-4", "read": ReadState(cloud=False),
        }])
        dialog.show()
        self.app.processEvents()
        rect = dialog.table.visualRect(dialog.table.model().index(0, 2))
        QTest.mouseClick(dialog.table.viewport(),
                         Qt.MouseButton.LeftButton,
                         pos=QPoint(rect.center().x(), rect.center().y()))
        self.app.processEvents()
        self.assertEqual(Qt.CheckState.Checked,
                         dialog.table.item(0, 2).checkState())
        self.assertEqual(["cloud-4"], _read_ids(dialog, True))
        dialog.close()

    def test_check_indicator_is_drawn_once_and_centered(self):
        class RecordingStyle(QProxyStyle):
            def __init__(self):
                super().__init__()
                self.check_rects = []

            def drawPrimitive(self, element, option, painter, widget=None):
                if element == QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck:
                    self.check_rects.append(option.rect)
                return super().drawPrimitive(element, option, painter, widget)

        table = QTableWidget(1, 1)
        self.addCleanup(table.close)
        style = RecordingStyle()
        table.setStyle(style)
        table.setItemDelegate(_CenteredCheckDelegate(table))
        item = QTableWidgetItem()
        item.setFlags(Qt.ItemFlag.ItemIsEnabled |
                      Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked)
        table.setItem(0, 0, item)
        table.resize(120, 50)
        table.show()
        self.app.processEvents()
        style.check_rects.clear()
        table.viewport().grab()
        self.assertEqual(1, len(style.check_rects))
        cell = table.visualRect(table.model().index(0, 0))
        self.assertLessEqual(
            abs(style.check_rects[0].center().x() - cell.center().x()), 1)

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
        dialog.table.item(0, 2).setCheckState(Qt.CheckState.Unchecked)
        self.assertEqual([], _read_ids(dialog, True))
        self.assertEqual(["cloud-2"], _read_ids(dialog, False))
        self.assertFalse(dialog.rows[0]["read"].local)
        dialog.close()


if __name__ == "__main__":
    unittest.main()
