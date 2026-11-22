"""Gemeinsame Basis der Testmodule.

Enthaelt nur, was die Suite wirklich braucht: der UI-Loader mit Qt-/Calibre-
Stubs, ein echtes PyQt6-Offscreen-Fenster, ein Loopback-HTTP-Server, die
Calibre-Datenbankgrenze und das Dashboard-Doppel.
"""
import base64
import contextlib
import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from ..errors import TolinoAuthError

# Feste Buch- und Kontokennungen fuer die Fixtures: dieselben Werte in allen
# Testmodulen, damit eine Buchzeile ueberall denselben Schluessel hat.
UUID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class PreferencesCase(unittest.TestCase):
    """Jeder Test bekommt einen eigenen Einstellungsspeicher."""

    def setUp(self):
        from .. import config

        self._original_preferences = config.PREFERENCES
        config.PREFERENCES = config.JSONConfig("test-%s" % uuid.uuid4().hex)
        self.addCleanup(setattr, config, "PREFERENCES",
                        self._original_preferences)


def no_crypto():
    """Steht fuer eine Installation ohne PyCryptodome."""
    return patch("calibre_plugin.browser_crypto._aes",
                 side_effect=TolinoAuthError("PyCryptodome fehlt",
                                             code="A01"))


def break_settings(case):
    """Einstellungs-Speicherort unschreibbar machen: ein Verzeichnis als Pfad.

    Lesen und Schreiben scheitern dann mit einem echten E/A-Fehler (kein
    ``FileNotFoundError``) -- das ist der Fall «Datei existiert, ist aber
    nicht benutzbar». ``case`` traegt das Aufraeumen.
    """
    from .. import config

    broken = tempfile.mkdtemp(prefix="tolino-unreadable-")
    case.addCleanup(shutil.rmtree, broken, True)
    config.PREFERENCES.file_path = broken


def jwt_token(claims):
    """JWT-foermiger Token mit frei waehlbaren Claims (ohne Signatur)."""
    segment = base64.urlsafe_b64encode(
        json.dumps(claims).encode("utf-8")).decode("ascii").rstrip("=")
    return "eyJhbGciOiJub25lIn0." + segment + ".sig"


def record(worker, *names):
    """Signale eines Workers mitschreiben: Name -> Argument(e)."""
    emitted = {}
    for name in names:
        getattr(worker, name).connect(
            lambda *args, name=name: emitted.setdefault(
                name, args[0] if len(args) == 1 else args))
    return emitted


def execute_worker(worker, client, ui, success="ready"):
    """Worker mit ersetztem Login ausfuehren, Signale mitschreiben."""
    emitted = record(worker, success, "failed")
    with patch.object(ui.workers.TOKEN_VAULT, "login", return_value=client):
        worker.run()
    return emitted


class FakeSignal:
    """Minimales Qt-Signal: Slots verbinden, ausloesen, ``fire``-Alias."""

    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


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


def ui_module_for(test_case, qt_stub):
    """Import ui.py with Calibre and Qt imports stubbed.

    `qt_stub` kommt aus `real_qt_stub()`: Widgets, Tabellen, Enums und
    Signale sind echt. Nur Icons, Pixmaps und Byte-Arrays bleiben Attrappen --
    keinen Test interessieren echte Bilddaten.
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
    # Der echte Stub zuletzt: er ersetzt jeden Namen, den die Attrappen
    # oben nur belegen.
    for name, value in qt_stub.items():
        setattr(qt_core, name, value)
    calibre = types.ModuleType("calibre")
    calibre_gui2 = types.ModuleType("calibre.gui2")
    calibre_gui2.error_dialog = None
    calibre_gui2.info_dialog = None
    calibre_gui2.get_icons = lambda name, context=None: None
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
                        "calibre_plugin.ui_dashboard_parts",
                        "calibre_plugin.sync_results"):
        sys.modules.pop(module_name, None)
    import calibre_plugin
    # Das Paket hält sonst die Referenz auf das zuvor geladene
    # ui-Modul (anderer Qt-Stub) und `from ... import ui` gäbe genau
    # diese alte Fassung zurück.
    for attribute in ("ui", "ui_common", "ui_dialogs", "cdp_transport",
                      "ui_workers",
                      "ui_auth_workers", "ui_dashboard",
                      "ui_dashboard_parts", "planning", "sync_results"):
        if getattr(calibre_plugin, attribute, None) is not None:
            delattr(calibre_plugin, attribute)
    ui_module = importlib.import_module("calibre_plugin.ui")
    # Tests use these modules directly; production's Calibre entrypoint does
    # not re-export them or import them eagerly.
    ui_module.common = importlib.import_module("calibre_plugin.ui_common")
    ui_module.dashboard = importlib.import_module("calibre_plugin.ui_dashboard")
    ui_module.dashboard_parts = importlib.import_module(
        "calibre_plugin.ui_dashboard_parts")
    ui_module.dialogs = importlib.import_module("calibre_plugin.ui_dialogs")
    ui_module.workers = importlib.import_module("calibre_plugin.ui_workers")
    ui_module.sync_results = importlib.import_module("calibre_plugin.sync_results")
    ui_module.auth_workers = importlib.import_module("calibre_plugin.ui_auth_workers")
    # Stubs stay active until restore_stubs() so that function-level
    # imports (from calibre.gui2 import get_icons) still resolve.
    def restore_stubs():
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    ui_module._restore_stubs_for_tests = restore_stubs
    test_case.addCleanup(ui_module._restore_stubs_for_tests)
    return ui_module


class UiCase(unittest.TestCase):
    """Geladenes UI-Modul mit echtem PyQt6 fuer einen Test."""

    def setUp(self):
        self.ui = ui_module_for(self, qt_stub=real_qt_stub())


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


class SyncThread:
    """Synchroner QThread-Ersatz: ``start()`` fuehrt den Worker aus.

    ``wait_result`` steuert, ob der Lauf beendet ist -- damit laesst sich der
    Timeout-Pfad ohne echte Wartezeit pruefen.
    """

    def __init__(self, parent=None, wait_result=True):
        self.started = FakeSignal()
        self.finished = FakeSignal()
        self.wait_result = wait_result
        self.stopped = False
        self.deleted = False

    def start(self):
        for slot in list(self.started.slots):
            slot()

    def quit(self, *_args):
        self.stopped = True

    def wait(self, *_args):
        return self.wait_result

    def isFinished(self):
        return self.wait_result

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
    """QFileDialog-Ersatz: ohne Auswahl kommt kein Pfad zurueck.

    ``answer`` laesst einen Test einen gewaehlten Speicherort vorgeben;
    der Standard ist der abgebrochene Dialog.
    """

    answer = ("", "")

    @classmethod
    def getSaveFileName(cls, *_args, **_kwargs):
        return cls.answer


def real_qt_stub():
    """``qt.core``-Ersatz aus echtem PyQt6 (offscreen).

    Widgets, Layouts, Tabellen, Delegates, Flags und Events kommen aus
    PyQt6. Doubles bleiben nur dort, wo echtes Qt den Testlauf stoeren
    wuerde: modale Dialoge (QMessageBox, QMenu, QFileDialog) blockieren,
    ein echter QThread macht die Worker nebenlaeufig, und Signale laufen
    als Python-Signal. Ohne PyQt6 ueberspringt der Test.
    """
    global _REAL_QT_APP
    # Der Ersatz sammelt prozessweit: jeder Dialogtest startet mit leerer Liste.
    RecordingMessageBox.calls = []
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
        "QThread": SyncThread,
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
    """Calibres new_api als Grenze: merkt Schreibvorgaenge, kann scheitern.

    Lesen kommt aus ``metadata`` (Buch-ID -> Calibre-Felder), ``paths``
    (Buch-ID/Format -> Datei) und ``cover``; damit bedienen die Planungs-
    und Worker-Tests dieselbe Grenze wie die Spaltentests.
    """

    def __init__(self, fail=(), books=None, metadata=None, paths=None,
                 cover=None):
        self.fail = set(fail)
        self.values = {}
        self.writes = []
        self.books = list(books or ())
        self.metadata = dict(metadata or {})
        self.paths = dict(paths or {})
        self.cover_data = cover

    def all_book_ids(self):
        return list(self.books)

    def all_field_for(self, field, book_ids):
        return {book_id: self.values.get((field, book_id), "")
                for book_id in book_ids}

    def has_id(self, book_id):
        return True

    def field_for(self, column, book_id):
        return self.values.get((column, book_id))

    def get_metadata(self, book_id):
        return dict(self.metadata.get(book_id) or {})

    def format_abspath(self, book_id, format_name):
        return self.paths.get((book_id, format_name), "")

    def cover(self, _book_id, as_file=False):
        return self.cover_data

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

    def __init__(self, columns=None, fail=(), create_error=None, dbpath="",
                 books=None, metadata=None, paths=None, cover=None):
        self.field_metadata = dict(columns or {})
        self.dbpath = dbpath
        self.api = CalibreApi(fail, books, metadata, paths, cover)
        self.created = []
        self.specs = []
        self.create_error = create_error

    @property
    def new_api(self):
        return self.api

    def create_custom_column(self, label, name, datatype, is_multiple,
                             editable=True, display=None):
        if self.create_error is not None:
            raise self.create_error
        self.created.append(label)
        self.specs.append((label, datatype))
        return len(self.created)


