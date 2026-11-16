"""Thematisch aufgeteilte Tests des tolino-Cloud-Sync-Plugins.

Einstieg ist ``calibre_plugin.test_sync``; die Module hier lassen sich
auch einzeln starten, z. B. ``python3 -m unittest
calibre_plugin.tests.workers_tests``.
"""
import atexit
import itertools
import json
import os
import shutil
import sys
import tempfile
import types


def _install_calibre_config_stub():
    """``calibre.utils.config.JSONConfig`` für Läufe ausserhalb von Calibre.

    Der Produktivcode importiert die Klasse unbedingt; ausserhalb einer
    Calibre-Installation fehlt das Paket. Der Ersatz liegt deshalb hier im
    Testpaket, das jeder dokumentierte Testeinstieg zuerst lädt.
    """
    try:
        __import__("calibre.utils.config")
    except ImportError:
        pass
    else:
        return True
    calibre = sys.modules.setdefault("calibre", types.ModuleType("calibre"))
    utils = sys.modules.setdefault("calibre.utils",
                                   types.ModuleType("calibre.utils"))
    module = types.ModuleType("calibre.utils.config")

    # Ein gemeinsamer Ordner fuer alle Store-Dateien, beim Prozessende weg.
    _STORE_ROOT = tempfile.mkdtemp(prefix="jsonconfig-")
    atexit.register(shutil.rmtree, _STORE_ROOT, True)
    _STORE_COUNT = itertools.count()

    class JSONConfig(dict):
        """Store-Ersatz mit denselben Semantiken wie Calibres ``JSONConfig``.

        Der Stand liegt im Speicher (Cache), ``refresh()`` liest die Datei
        neu und ``commit()`` schreibt die GANZE Datei. Genau daraus entstand
        der Live-Fehler mit verschwundenen Tokens: ein zweiter Prozess
        schreibt, der eigene Cache schreibt danach alles zurueck. Tests
        muessen dieses Verhalten nachstellen koennen.
        """

        def __init__(self, name=""):
            super().__init__()
            self.file_path = os.path.join(
                _STORE_ROOT, "%s-%d.json" % (
                    (name or "config").replace("/", "-"),
                    next(_STORE_COUNT)))
            self.refresh()

        def refresh(self):
            try:
                with open(self.file_path, encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                data = {}
            super().clear()
            super().update(data if isinstance(data, dict) else {})

        def __setitem__(self, key, value):
            # Wie Calibres Vorbild: ein direktes Setzen schreibt sofort.
            super().__setitem__(key, value)
            self._flush()

        def update(self, *args, **kwargs):
            # Calibres ``update`` committet nicht. Der Ersatz schreibt aber
            # sofort, damit ein geseedeter Stand einen ``refresh`` ueberlebt;
            # ``commit`` bleibt dabei der einzige gezaehlte Schreibvorgang.
            super().update(*args, **kwargs)
            self._flush()

        def commit(self):
            self._flush()

        def _flush(self):
            with open(self.file_path, "w", encoding="utf-8") as handle:
                json.dump(dict(self), handle)

    module.JSONConfig = JSONConfig
    utils.config = module
    calibre.utils = utils
    sys.modules["calibre.utils.config"] = module
    return False


def _isolate_real_config_store():
    """Echte Calibre-Laeufe schreiben nie in die Einstellungen des Nutzers.

    Kalibres ``JSONConfig`` liest und committet sofort. Ohne eigenen Ordner
    wuerde eine Suite, die unter ``calibre-debug`` laeuft, die echten
    Plugin-Einstellungen samt Konten und Tokens ueberschreiben.
    """
    from calibre.utils import config as calibre_config
    base = tempfile.mkdtemp(prefix="tolino-tests-")
    atexit.register(shutil.rmtree, base, True)
    original = calibre_config.JSONConfig

    class JSONConfig(original):
        """Kalibres Store -- immer in einem frischen eigenen Testordner."""

        def __init__(self, rel_path="", base_path=None, permissions=0o666):
            original.__init__(
                self, rel_path,
                base_path=base_path or tempfile.mkdtemp(
                    prefix="store-", dir=base),
                permissions=permissions)

    calibre_config.JSONConfig = JSONConfig


if _install_calibre_config_stub():
    _isolate_real_config_store()
