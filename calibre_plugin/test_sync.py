"""Einstieg der Testsuite: sammelt die thematischen Module.

Die Tests liegen in ``calibre_plugin/tests/``; dieser Modulname bleibt der
dokumentierte Aufruf ``python3 -m unittest calibre_plugin.test_sync`` und
fuehrt alle Module aus. Einzelne Gruppen lassen sich direkt starten, z. B.
``python3 -m unittest calibre_plugin.tests.workers_tests``.
"""
import unittest

from .tests import (browser_tests, build_tests, dashboard_tests, dialogs_tests,
                    identifiers_tests, orchestration_tests, planning_tests,
                    read_status_tests, tolino_client_tests, transport_tests, ui_base_tests,
                    worker_base_tests, workers_tests)
from .tests._support import start_cdp_stubs, stop_cdp_stubs

_MODULES = (
    ui_base_tests,
    worker_base_tests,
    orchestration_tests,
    planning_tests,
    tolino_client_tests,
    transport_tests,
    browser_tests,
    build_tests,
    dialogs_tests,
    dashboard_tests,
    workers_tests,
    identifiers_tests,
    read_status_tests,
)


def setUpModule():
    start_cdp_stubs()


def tearDownModule():
    stop_cdp_stubs()


def load_tests(loader, tests, pattern):
    """Alle thematischen Module laden -- Reihenfolge bleibt fest."""
    suite = unittest.TestSuite()
    for module in _MODULES:
        suite.addTests(loader.loadTestsFromModule(module))
    return suite
