"""Gemeinsame Basis der aufgeteilten Testmodule.

Enthaelt nur, was mehrere Module brauchen: die Chromium-/Close-Stubs fuer
die ganze Suite, den Quelltext der Oberflaechenmodule, den UI-Loader mit
Qt-/Calibre-Stubs und kleine Helfer.
Die Testklassen selbst liegen thematisch in ``tests/*_tests.py``;
``calibre_plugin/test_sync.py`` sammelt sie wieder ein.
"""
import importlib
import sys
import types
from pathlib import Path
from unittest.mock import patch

import calibre_plugin.cdp as cdp_module
from calibre_plugin.comparison import compare_inventory as _compare_inventory
from calibre_plugin.inventory import normalize_inventory_record, parse_inventory

# setUpModule() stubs close_grabber_window; real close tests keep this.
_REAL_CLOSE_WINDOW = cdp_module.close_grabber_window


def parsed_inventory(value):
    """Return canonical inventory records from a raw fixture or record list."""
    if isinstance(value, list):
        return [normalize_inventory_record(item)
                for item in value if isinstance(item, dict)]
    records, problems, valid_envelope = parse_inventory(value)
    if not valid_envelope:
        raise ValueError("Invalid inventory fixture: %s"
                         % "; ".join(problems))
    return records


def compare_inventory(metadata, state, inventory, *args, **kwargs):
    """Comparison test helper: normalize raw Tolino fixtures at the boundary."""
    return _compare_inventory(metadata, state, parsed_inventory(inventory),
                              *args, **kwargs)


# Alle Oberflaechen-Module fuer Strukturtests. ist ui.py nur noch
# der Einstieg; Dialoge, Worker und Dashboard liegen in eigenen Modulen.
UI_SOURCE_FILES = ("ui.py", "ui_common.py", "ui_dialogs.py",
                   "ui_workers.py", "ui_auth_workers.py", "ui_settings.py",
                   "ui_dashboard.py", "dashboard_plan.py")


def _ui_source_text():
    base = Path(__file__).resolve().parent.parent
    return "\n".join((base / name).read_text(encoding="utf-8")
                      for name in UI_SOURCE_FILES)


def _full(exc):
    """Nutzertext plus Log-Detail eines TolinoError."""
    return "%s %s" % (exc, getattr(exc, "detail", ""))


def load_ui_module(get_icons_result, qt_stub=None):
    """Import ui.py with Calibre and Qt imports stubbed.

    `qt_stub` überschreibt einzelne Qt-Namen (z. B. einen ehrlichen
    QDialog/QTableWidget-Ersatz, damit der Aufbau eines Fensters
    wirklich durchlaufen werden kann).
    """

    class FakeIcon:
        def __init__(self, *args, **kwargs):
            self._null = True

        def isNull(self):
            return self._null

    qt_core = types.ModuleType("qt.core")
    qt_core.QIcon = FakeIcon
    for name in ("QCheckBox", "QComboBox", "QDialog", "QDialogButtonBox",
                 "QFormLayout", "QGroupBox", "QLabel", "QLineEdit",
                 "QMessageBox", "QProgressBar", "QPushButton",
                 "QProgressDialog", "QThread",
                 "QVBoxLayout", "QHBoxLayout", "QTableWidget",
                 "QTableWidgetItem", "QTextEdit", "QObject",
                 "QInputDialog", "QFileDialog", "QMenu", "Qt", "QTimer"):
        setattr(qt_core, name, type(name, (), {}))
    qt_core.pyqtSignal = lambda *a, **k: None
    qt_core.Signal = lambda *a, **k: None
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
    for module_name in ("calibre_plugin.ui", "calibre_plugin.ui_common",
                        "calibre_plugin.ui_dialogs",
                        "calibre_plugin.dashboard_plan",
                        "calibre_plugin.cdp_transport",
                        "calibre_plugin.browser_crypto",
                        "calibre_plugin.token_candidates",
                        "calibre_plugin.ui_workers",
                        "calibre_plugin.ui_auth_workers",
                        "calibre_plugin.ui_settings",
                        "calibre_plugin.ui_dashboard",
                        "calibre_plugin.dashboard_plan"):
        sys.modules.pop(module_name, None)
    import calibre_plugin
    # Das Paket hält sonst die Referenz auf das zuvor geladene
    # ui-Modul (anderer Qt-Stub) und `from ... import ui` gäbe genau
    # diese alte Fassung zurück.
    for attribute in ("ui", "ui_common", "ui_dialogs", "cdp_transport",
                      "token_candidates", "browser_crypto", "ui_workers",
                      "ui_auth_workers", "ui_settings", "ui_dashboard",
                      "dashboard_plan"):
        if getattr(calibre_plugin, attribute, None) is not None:
            delattr(calibre_plugin, attribute)
    modules_before_entry = set(sys.modules)
    ui_module = importlib.import_module("calibre_plugin.ui")
    loaded_by_entry = set(sys.modules) - modules_before_entry
    # Tests use these modules directly; production's Calibre entrypoint does
    # not re-export them or import them eagerly.
    ui_module.common = importlib.import_module("calibre_plugin.ui_common")
    ui_module.dashboard = importlib.import_module("calibre_plugin.ui_dashboard")
    ui_module.dialogs = importlib.import_module("calibre_plugin.ui_dialogs")
    ui_module.workers = importlib.import_module("calibre_plugin.ui_workers")
    ui_module.auth_workers = importlib.import_module("calibre_plugin.ui_auth_workers")
    ui_module.settings_dialog = importlib.import_module("calibre_plugin.ui_settings")
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
# pick_chromium() would succeed and launch_reader_window() would spawn a
# browser the tests never asked for -- every login test that asserts the
# no-Chromium fallback would then fail or hang. Tests that need a
# browser patch calibre_plugin.cdp.pick_chromium themselves.
# close_grabber_window() is stubbed as well: the real one
# talks to a DevTools endpoint on 127.0.0.1:9223 and must never close
# a developer's open grabber window while the suite runs. Tests that
# care about the close patch the name again with their own mock.
_PICK_CHROMIUM_PATCH = None
_CLOSE_WINDOW_PATCH = None


def start_cdp_stubs():
    """Browser-Start und echtes Fenster-Schliessen fuer die Suite stilllegen."""
    global _PICK_CHROMIUM_PATCH, _CLOSE_WINDOW_PATCH
    if _PICK_CHROMIUM_PATCH is None:
        _PICK_CHROMIUM_PATCH = patch("calibre_plugin.cdp.pick_chromium",
                                     return_value=None)
        _PICK_CHROMIUM_PATCH.start()
    if _CLOSE_WINDOW_PATCH is None:
        _CLOSE_WINDOW_PATCH = patch("calibre_plugin.cdp.close_grabber_window",
                                    return_value=False)
        _CLOSE_WINDOW_PATCH.start()


def stop_cdp_stubs():
    """Die Stubs wieder entfernen (auch nach einem Abbruch)."""
    global _PICK_CHROMIUM_PATCH, _CLOSE_WINDOW_PATCH
    if _PICK_CHROMIUM_PATCH is not None:
        _PICK_CHROMIUM_PATCH.stop()
        _PICK_CHROMIUM_PATCH = None
    if _CLOSE_WINDOW_PATCH is not None:
        _CLOSE_WINDOW_PATCH.stop()
        _CLOSE_WINDOW_PATCH = None


# --- Wiederverwendete GUI-Test-Doubles ---------------------------------


class FakeText:
    """QLabel/QLineEdit-Ersatz: merkt sich den angezeigten Text."""

    def __init__(self, text=""):
        self.value = text

    def setText(self, text):
        self.value = text

    def text(self):
        return self.value


class FakeCheckBox:
    """QCheckBox-Ersatz fuer die Zell-Widgets der Bestandsliste."""

    def __init__(self):
        self._checked = False
        self.enabled = True
        self.tooltip = ""

    def setChecked(self, value):
        self._checked = bool(value)

    def isChecked(self):
        return self._checked

    def setEnabled(self, value):
        self.enabled = value

    def setToolTip(self, text):
        self.tooltip = text

    def stateChanged(self):
        return None


class FakeMenu:
    """QMenu-Ersatz: sammelt Aktionen, wahlweise mit Auswahlprotokoll."""

    def __init__(self, _parent=None, selected=None, pick=0):
        self.actions = []
        self.selected = selected
        self.pick = pick

    def addAction(self, label):
        self.actions.append(label)
        return label

    def exec(self, _position=None):
        if self.selected is not None:
            self.selected.extend(self.actions)
        if self.pick is None or not self.actions:
            return None
        return self.actions[self.pick]


class FakeMessageBox:
    """QMessageBox-Ersatz: sammelt die Argumente je Aufruf."""

    Yes = 1
    No = 0

    def __init__(self, calls, question_result=0):
        self.calls = calls
        self.question_result = question_result

    def information(self, *args, **_kwargs):
        self.calls.append(args)

    def warning(self, *args, **_kwargs):
        self.calls.append(args)

    def question(self, *args, **_kwargs):
        self.calls.append(args)
        return self.question_result
