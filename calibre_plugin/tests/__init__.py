"""Thematisch aufgeteilte Tests des tolino-Cloud-Sync-Plugins.

Einstieg ist ``calibre_plugin.test_sync``; die Module hier lassen sich
auch einzeln starten, z. B. ``python3 -m unittest
calibre_plugin.tests.workers_tests``.
"""
import sys
import types


def _install_calibre_config_stub():
    """``calibre.utils.config.JSONConfig`` für Läufe ausserhalb von Calibre.

    Der Produktivcode importiert die Klasse unbedingt; ausserhalb einer
    Calibre-Installation fehlt das Paket. Der Ersatz liegt deshalb hier im
    Testpaket, das jeder dokumentierte Testeinstieg zuerst lädt.
    """
    try:
        import calibre.utils.config  # noqa: F401
    except ImportError:
        pass
    else:
        return
    calibre = sys.modules.setdefault("calibre", types.ModuleType("calibre"))
    utils = sys.modules.setdefault("calibre.utils",
                                   types.ModuleType("calibre.utils"))
    module = types.ModuleType("calibre.utils.config")

    class JSONConfig(dict):
        """In-memory stand-in for Calibre's JSON preferences store."""

        def __init__(self, _name=""):
            super().__init__()

        def commit(self):
            return None

    module.JSONConfig = JSONConfig
    utils.config = module
    calibre.utils = utils
    sys.modules["calibre.utils.config"] = module


_install_calibre_config_stub()
