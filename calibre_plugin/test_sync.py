"""Einstieg der Testsuite: laedt alle thematischen Module.

Der dokumentierte Aufruf bleibt ``python3 -m unittest calibre_plugin.test_sync``.
``python3 -m unittest discover -s calibre_plugin/tests -t .`` laedt dieselben
Dateien; die Qt-/Chromium-Stubs richten die Testmodule selbst in
``setUpModule`` ein.
"""
import os

_TESTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_tests(loader, tests, pattern):
    """Alle ``*_tests.py`` im Testpaket laden."""
    return loader.discover(_TESTS, pattern="*_tests.py", top_level_dir=_ROOT)
