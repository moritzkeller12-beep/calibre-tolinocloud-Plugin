"""Gemeinsame Basis der Testmodule.

Enthaelt nur, was die Suite wirklich braucht: der UI-Loader mit Qt-/Calibre-
Stubs, ein echtes PyQt6-Offscreen-Fenster, ein Loopback-HTTP-Server, die
Calibre-Datenbankgrenze und das Dashboard-Doppel.
"""
import contextlib
import importlib
import os
import sys
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeSignal:
    """Minimales Qt-Signal: Slots verbinden, ausloesen, ``fire``-Alias."""

    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)

    fire = emit


_WORKER_FIELDS = ("thread", "worker", "prepare_thread", "prepare_worker",
                  "snapshot_thread", "snapshot_worker", "login_thread",
                  "login_worker")


# Felder, die der Produktivcode ohne Wächter liest: das Double ist vollständig.
_DASHBOARD_FIELDS = {"account_name": "", "account_names": [],
                     "_hardware_edited": False, "_partner_chosen": False,
                     "login_button": None, "gui": None,
                     "primary": types.SimpleNamespace(
                         setDefault=lambda *_a: None, setFocus=lambda: None)}


def dashboard_stub(**attrs):
    """Minimales Dashboard mit leeren Worker-Referenzen."""
    values = dict(dict.fromkeys(_WORKER_FIELDS), **_DASHBOARD_FIELDS)
    values.update(attrs)
    return types.SimpleNamespace(**values)


# Felder, die der Produktivcode als Widgets anspricht.
_ACCOUNT_PATCH = None


class FakeTableItem:
    """QTableWidgetItem-Ersatz fuer alle Tabellentests."""

    def __init__(self, text=""):
        self.text = text
        self._check_state = 0
        self._flags = 0
        self.tooltip = ""
        self.text_alignment = None
        self._row = 0
        self._column = 0

    def row(self):
        return self._row

    def column(self):
        return self._column

    def setTextAlignment(self, alignment):
        self.text_alignment = alignment

    def setFlags(self, flags):
        self._flags = flags

    def flags(self):
        return self._flags

    def setCheckState(self, state):
        changed = state != self._check_state
        self._check_state = state
        table = getattr(self, "_table", None)
        if changed and table is not None:
            table.itemChanged.emit(self)

    def checkState(self):
        return self._check_state

    def isChecked(self):
        return bool(self._check_state)

    def setChecked(self, value):
        self.setCheckState(2 if value else 0)

    def setToolTip(self, text):
        self.tooltip = text

    def toolTip(self):
        return self.tooltip


def load_ui_module(get_icons_result, qt_stub=None):
    """Import ui.py with Calibre and Qt imports stubbed.

    `qt_stub` überschreibt einzelne Qt-Namen (z. B. einen ehrlichen
    QDialog/QTableWidget-Ersatz, damit der Aufbau eines Fensters
    wirklich durchlaufen werden kann).
    """

    class FakeIcon:
        def __init__(self, *args, **kwargs):
            self._null = not args or not getattr(args[0], "data", None)

        def isNull(self):
            return self._null

    class FakePixmap:
        def loadFromData(self, data):
            self.data = data
            return bool(data)

    qt_core = types.ModuleType("qt.core")
    qt_core.QIcon = FakeIcon
    qt_core.QPixmap = FakePixmap
    qt_core.QByteArray = bytes
    for name in ("QCheckBox", "QComboBox", "QDialog", "QFormLayout",
                 "QGroupBox", "QLabel", "QLineEdit", "QMessageBox",
                 "QProgressBar", "QPushButton", "QThread", "QVBoxLayout",
                 "QHBoxLayout", "QTableWidget", "QTableWidgetItem",
                 "QObject", "QInputDialog", "QFileDialog", "QMenu", "Qt"):
        setattr(qt_core, name, type(name, (), {}))
    # Qt6-Form: die unskopierten Namen (QHeaderView.Stretch) gibt es dort
    # nicht mehr. Ein Stub mit nur ResizeMode deckt genau diesen Fehler auf.
    header_view = type("QHeaderView", (), {})
    header_view.ResizeMode = type(
        "ResizeMode", (), {"Interactive": 0, "Stretch": 1,
                           "ResizeToContents": 2})
    qt_core.QHeaderView = header_view
    qt_core.QStyledItemDelegate = type(
        "QStyledItemDelegate", (),
        {"__init__": lambda self, *args, **kwargs: None})
    qt_core.QEvent = type("QEvent", (), {})
    qt_core.QDialog.Accepted = 1
    qt_core.QDialog.DialogCode = qt_core.QDialog
    qt_core.QMessageBox.Yes = 1
    qt_core.QMessageBox.No = 2
    qt_core.QMessageBox.StandardButton = qt_core.QMessageBox
    # Qt6-Form: Enums sind skopiert; die alten unskopierten Namen fehlen.
    qt_core.Qt = type("Qt", (), {
        "SortOrder": type("SortOrder", (),
                          {"AscendingOrder": 0, "DescendingOrder": 1}),
        "ItemFlag": type("ItemFlag", (), {
            "ItemIsEnabled": 1, "ItemIsUserCheckable": 2}),
        "CheckState": type("CheckState", (),
                           {"Checked": 2, "Unchecked": 0}),
        "WidgetAttribute": type("WidgetAttribute", (),
                                 {"WA_QuitOnClose": 55}),
    })
    qt_core.QDialog.setAttribute = lambda self, attribute, value: (
        setattr(self, "attributes", getattr(self, "attributes", {})),
        self.attributes.__setitem__(attribute, value),
    )
    qt_core.QDialog.setWindowTitle = lambda self, title: setattr(
        self, "window_title", title)
    qt_core.QTableWidget.SelectionBehavior = type(
        "SelectionBehavior", (), {"SelectRows": 1})
    qt_core.QTableWidget.SelectionMode = type(
        "SelectionMode", (), {"ExtendedSelection": 2})
    qt_core.QLineEdit.EchoMode = type("EchoMode", (), {"Password": 2})
    qt_core.pyqtSignal = lambda *a, **_kwargs: None
    qt_core.Signal = lambda *a, **_kwargs: None
    qt_core.QTableWidgetItem = FakeTableItem
    if qt_stub:
        # Zuletzt, damit ein Stub auch pyqtSignal/Signal ersetzen darf.
        for name, value in qt_stub.items():
            setattr(qt_core, name, value)
    calibre = types.ModuleType("calibre")
    calibre_gui2 = types.ModuleType("calibre.gui2")
    calibre_gui2.error_dialog = None
    calibre_gui2.info_dialog = None
    calibre_gui2.get_icons = lambda name, context=None: get_icons_result
    calibre_actions = types.ModuleType("calibre.gui2.actions")
    calibre_actions.InterfaceAction = object
    calibre.gui2 = calibre_gui2
    calibre_gui2.actions = calibre_actions
    # Nur die fremden Module werden zurueckgesetzt. Die UI-Module
    # bleiben nach dem Test mit ihren Stubs in sys.modules: so patcht
    # ein Test, der nach dem Aufraeumen noch eine Methode aufruft,
    # dieselbe Modulinstanz wie die getestete Klasse.
    saved = {name: sys.modules.get(name)
             for name in ("calibre", "calibre.gui2",
                          "calibre.gui2.actions", "qt.core", "qt")}
    for name, module in (("calibre", calibre),
                         ("calibre.gui2", calibre_gui2),
                         ("calibre.gui2.actions", calibre_actions),
                         ("qt", types.ModuleType("qt")),
                         ("qt.core", qt_core)):
        sys.modules[name] = module
    # Nur Module mit Qt-/Calibre-Importen werden neu geladen. Reine
    # Logikmodule (Token-Kandidaten, Crypto) bleiben dieselbe Instanz:
    # sonst patcht ein Test eine Kopie, während bereits importierter
    # Produktivcode die andere benutzt.
    for module_name in ("calibre_plugin.ui", "calibre_plugin.ui_common",
                        "calibre_plugin.ui_dialogs",
                        "calibre_plugin.planning",
                        "calibre_plugin.cdp_transport",
                        "calibre_plugin.ui_workers",
                        "calibre_plugin.ui_auth_workers",
                        "calibre_plugin.ui_dashboard",
                        "calibre_plugin.ui_dashboard_parts"):
        sys.modules.pop(module_name, None)
    import calibre_plugin
    # Das Paket hält sonst die Referenz auf das zuvor geladene
    # ui-Modul (anderer Qt-Stub) und `from ... import ui` gäbe genau
    # diese alte Fassung zurück.
    for attribute in ("ui", "ui_common", "ui_dialogs", "cdp_transport",
                      "ui_workers",
                      "ui_auth_workers", "ui_dashboard",
                      "ui_dashboard_parts", "planning"):
        if getattr(calibre_plugin, attribute, None) is not None:
            delattr(calibre_plugin, attribute)
    modules_before_entry = set(sys.modules)
    ui_module = importlib.import_module("calibre_plugin.ui")
    loaded_by_entry = set(sys.modules) - modules_before_entry
    # Tests use these modules directly; production's Calibre entrypoint does
    # not re-export them or import them eagerly.
    ui_module.common = importlib.import_module("calibre_plugin.ui_common")
    ui_module.dashboard = importlib.import_module("calibre_plugin.ui_dashboard")
    ui_module.dashboard_parts = importlib.import_module(
        "calibre_plugin.ui_dashboard_parts")
    ui_module.dialogs = importlib.import_module("calibre_plugin.ui_dialogs")
    ui_module.workers = importlib.import_module("calibre_plugin.ui_workers")
    ui_module.auth_workers = importlib.import_module("calibre_plugin.ui_auth_workers")
    ui_module._loaded_by_entry_for_tests = loaded_by_entry
    # Stubs stay active until restore_stubs() so that function-level
    # imports (from calibre.gui2 import get_icons) still resolve.
    def restore_stubs():
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    ui_module._restore_stubs_for_tests = restore_stubs
    return ui_module


def ui_module_for(test_case, qt_stub=None):
    """UI-Modul für einen Test laden und die Stubs danach zurücknehmen."""
    module = load_ui_module(lambda *args, **kwargs: None, qt_stub=qt_stub)
    test_case.addCleanup(module._restore_stubs_for_tests)
    return module


# The login helpers must not open a REAL browser window during tests.
# On machines that have Chrome/Chromium installed (CI runners do),
# find_browsers() would succeed and launch_reader_window() would spawn a
# browser the tests never asked for -- every login test that asserts the
# no-Chromium fallback would then fail or hang. Tests that need a
# browser patch calibre_plugin.browser_finder.find_browsers themselves.
# close_grabber_window() is stubbed as well: the real one
# talks to a DevTools endpoint on 127.0.0.1:9223 and must never close
# a developer's open grabber window while the suite runs. Tests that
# care about the close patch the name again with their own mock.
# --- Echte Qt-Oberflaeche fuer die Dialogtests -------------------------

_REAL_QT_APP = None


class _QtObject:
    """QObject-Ersatz: nur Thread-Zuordnung und deleteLater.

    Signale laufen als Python-Signal, damit Worker ohne echten
    Qt-Thread testbar bleiben.
    """

    def __init__(self, *args, **_kwargs):
        pass

    def moveToThread(self, thread):
        self.thread = thread

    def deleteLater(self):
        pass


class _SyncThread:
    """Synchroner QThread-Ersatz: ``start()`` fuehrt den Worker aus."""

    def __init__(self, parent=None):
        self.started = FakeSignal()
        self.finished = FakeSignal()

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
    """pyqtSignal-Ersatz: je Instanz ein eigenes Python-Signal."""

    def __init__(self, *args, **_kwargs):
        self.name = None

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        store = instance.__dict__.setdefault("_signals", {})
        return store.setdefault(self.name, FakeSignal())


class RecordingMessageBox:
    """QMessageBox-Ersatz: ein echter Dialog wuerde den Testlauf blockieren."""

    calls = []
    Yes = 1
    No = 0
    StandardButton = types.SimpleNamespace(Yes=1, No=0)

    @classmethod
    def information(cls, *args):
        cls.calls.append(("information",) + args)

    @classmethod
    def warning(cls, *args):
        cls.calls.append(("warning",) + args)

    @classmethod
    def question(cls, *args):
        cls.calls.append(("question",) + args)
        return cls.No


class RecordingMenu:
    """QMenu-Ersatz: sammelt Aktionen; ohne Auswahl liefert exec() None."""

    def __init__(self, _parent=None):
        self.actions = []

    def addAction(self, label):
        self.actions.append(label)
        return label

    def exec(self, *_args):
        return None


class RecordingFileDialog:
    """QFileDialog-Ersatz: ohne Auswahl kommt kein Pfad zurueck."""

    @staticmethod
    def getSaveFileName(*_args, **_kwargs):
        return "", ""


def real_qt_stub():
    """``qt.core``-Ersatz aus echtem PyQt6 (offscreen).

    Widgets, Layouts, Tabellen, Delegates, Flags und Events kommen aus
    PyQt6. Doubles bleiben nur dort, wo echtes Qt den Testlauf stoeren
    wuerde: modale Dialoge (QMessageBox, QMenu, QFileDialog) blockieren,
    ein echter QThread macht die Worker nebenlaeufig, und Signale laufen
    als Python-Signal. Ohne PyQt6 ueberspringt der Test.
    """
    global _REAL_QT_APP
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PyQt6.QtCore import QEvent, Qt
        from PyQt6.QtWidgets import (
            QApplication, QCheckBox, QComboBox, QDialog, QFormLayout,
            QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
            QLineEdit, QProgressBar, QPushButton, QStyledItemDelegate,
            QStyleOptionViewItem, QTableWidget, QTableWidgetItem,
            QVBoxLayout)
    except ImportError as exc:
        raise unittest.SkipTest("PyQt6 fehlt: %s" % exc) from exc
    if _REAL_QT_APP is None:
        _REAL_QT_APP = QApplication.instance() or QApplication([])
    return {
        "Qt": Qt,
        "QEvent": QEvent,
        "QObject": _QtObject,
        "QThread": _SyncThread,
        "pyqtSignal": SignalDescriptor,
        "QDialog": QDialog,
        "QVBoxLayout": QVBoxLayout,
        "QHBoxLayout": QHBoxLayout,
        "QFormLayout": QFormLayout,
        "QLabel": QLabel,
        "QPushButton": QPushButton,
        "QCheckBox": QCheckBox,
        "QComboBox": QComboBox,
        "QGroupBox": QGroupBox,
        "QProgressBar": QProgressBar,
        "QLineEdit": QLineEdit,
        "QHeaderView": QHeaderView,
        "QTableWidget": QTableWidget,
        "QTableWidgetItem": QTableWidgetItem,
        "QStyledItemDelegate": QStyledItemDelegate,
        "QStyleOptionViewItem": QStyleOptionViewItem,
        "QInputDialog": QInputDialog,
        "QMessageBox": RecordingMessageBox,
        "QMenu": RecordingMenu,
        "QFileDialog": RecordingFileDialog,
    }


# --- Wiederverwendete GUI-Test-Doubles ---------------------------------


class LocalHTTP:
    """Echter HTTP-Server auf Loopback fuer Transport-Tests.

    `responder(method, path, headers, body)` liefert
    ``(status, headers, body)``. Jede Anfrage landet in ``response.requests``
    bzw. ``self.requests`` -- der Test beobachtet damit echte Sockets statt
    `urlopen` zu patchen.
    """

    def __init__(self, responder):
        self.responder = responder
        self.requests = []
        self._server = None
        self._thread = None

    def __enter__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self):
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                body = self.rfile.read(length) if length else b""
                outer.requests.append({
                    "method": self.command,
                    "path": self.path,
                    "headers": {key.casefold(): value
                                for key, value in self.headers.items()},
                    "body": body,
                })
                status, headers, payload = outer.responder(
                    self.command, self.path, self.headers, body)
                payload = payload or b""
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

            def log_message(self, *_args):
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01},
            daemon=True)
        self._thread.start()
        return self

    @property
    def origin(self):
        host, port = self._server.server_address
        return "http://%s:%d" % (host, port)

    def url(self, path):
        return self.origin + path

    def paths(self):
        return [request["path"] for request in self.requests]

    def __exit__(self, *_exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)
        return False


@contextlib.contextmanager
def local_tolino_endpoints(origin, partner_ids=(1, 4, 5, 8)):
    """Die absoluten Endpunkte des Clients auf den Testserver zeigen lassen.

    Das ist eine echte Konfigurationsumlenkung: der Produktivcode baut
    weiterhin `urllib`-Requests und spricht echte Sockets, nur eben mit
    Loopback-Adressen.
    """
    from .. import tolino as module
    from ..partners import PARTNERS

    partners = {pid: PARTNERS[pid]["token_url"] for pid in partner_ids}
    saved = (module.BASE_URL, module.TolinoClient.META_URL,
             module.TolinoClient.DEVICES_URL,
             module.TolinoClient.SYNC_DATA_URL)
    module.BASE_URL = origin + "/bosh/rest"
    module.TolinoClient.META_URL = module.BASE_URL + "/meta"
    module.TolinoClient.DEVICES_URL = (module.BASE_URL
                                       + "/handshake/devices/list")
    module.TolinoClient.SYNC_DATA_URL = (module.BASE_URL
                                         + "/sync-data?paths=publications")
    for partner_id in partner_ids:
        PARTNERS[partner_id]["token_url"] = origin + "/token/%d" % partner_id
    try:
        yield
    finally:
        (module.BASE_URL, module.TolinoClient.META_URL,
         module.TolinoClient.DEVICES_URL,
         module.TolinoClient.SYNC_DATA_URL) = saved
        for partner_id, value in partners.items():
            PARTNERS[partner_id]["token_url"] = value


class CalibreApi:
    """Calibres new_api als Grenze: merkt Schreibvorgaenge, kann scheitern."""

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.values = {}
        self.writes = []

    def has_id(self, book_id):
        return True

    def field_for(self, column, book_id):
        return self.values.get((column, book_id))

    def set_field(self, column, values):
        if column in self.fail:
            raise OSError("Spalte gesperrt")
        self.writes.append((column, dict(values)))
        for book_id, value in values.items():
            self.values[(column, book_id)] = value


class CalibreDatabase:
    """Nur die Calibre-Schnittstelle, die das Plugin benutzt.

    ``create_custom_column`` speichert die Spalte wie Calibre, laesst
    ``field_metadata`` aber unveraendert: dort erscheint sie erst nach
    einem Neustart.
    """

    def __init__(self, columns=None, fail=(), create_error=None, dbpath=""):
        self.field_metadata = dict(columns or {})
        self.dbpath = dbpath
        self.api = CalibreApi(fail)
        self.created = []
        self.create_error = create_error

    @property
    def new_api(self):
        return self.api

    def create_custom_column(self, label, name, datatype, is_multiple,
                             editable=True, display=None):
        if self.create_error is not None:
            raise self.create_error
        self.created.append(label)
        return len(self.created)


