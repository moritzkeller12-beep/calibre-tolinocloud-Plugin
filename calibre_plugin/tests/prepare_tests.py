"""Vorbereitung des Vergleichs: Cloud-Bestand, Lesewerte und Abbruch.

Der Client ist ein Doppel an der Netznaht; Vergleich, Zustandsfuehrung und
Kontospeicherung laufen im Produktivcode.
"""
import shutil
import tempfile
import unittest
import uuid
from unittest.mock import patch

from .. import config
from ..errors import TolinoAuthError, TolinoError
from ..run_context import RunContext
from ._support import real_qt_stub, ui_module_for

UUID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
BOOK = {"uuid": UUID, "title": "Der Schwarm", "authors": "Frank Schätzing",
        "isbn": "9780306406157", "formats": ["EPUB"], "ignored": False}
CLOUD = {"tolino_id": "d1", "title": "Der Schwarm",
         "authors": "Frank Schätzing", "isbn": "9780306406157"}


class FakeClient:
    """Liefert Bestand und optionale Lesewerte -- oder deren Fehler."""

    hardware = "hw-fake"
    access = "access"

    def __init__(self, inventory=(), inventory_error=None, read_ids=None,
                 read_error=None, progress=None, progress_error=None):
        self.inventory_rows = list(inventory)
        self.inventory_error = inventory_error
        self.read_result = set() if read_ids is None else read_ids
        self.read_error = read_error
        self.progress_result = {} if progress is None else progress
        self.progress_error = progress_error
        self.calls = []

    def inventory(self):
        self.calls.append("inventory")
        if self.inventory_error is not None:
            raise self.inventory_error
        return list(self.inventory_rows)

    def read_ids(self):
        self.calls.append("read_ids")
        if self.read_error is not None:
            raise self.read_error
        return set(self.read_result)

    def progress_by_id(self):
        self.calls.append("progress")
        if self.progress_error is not None:
            raise self.progress_error
        return dict(self.progress_result)


class PrepareCase(unittest.TestCase):
    """Frischer Einstellungsspeicher und geladenes UI-Modul je Test."""

    def setUp(self):
        self._original = config.PREFERENCES
        config.PREFERENCES = config.JSONConfig("test-%s" % uuid.uuid4().hex)
        self.addCleanup(setattr, config, "PREFERENCES", self._original)
        self.ui = ui_module_for(self, qt_stub=real_qt_stub())
        self.context = RunContext.from_values(
            {"account_name": "A", "preferred_formats": ["EPUB"]},
            columns={"read": True, "progress": True})

    def prepare(self, client, metadata=None, state=None):
        worker = self.ui.workers.PrepareWorker(
            self.context, {1: dict(BOOK)} if metadata is None else metadata,
            {} if state is None else state)
        emitted = {}
        worker.ready.connect(lambda payload: emitted.setdefault("ready", payload))
        worker.failed.connect(lambda message: emitted.setdefault("failed", message))
        with patch.object(self.ui.workers.TOKEN_VAULT, "login",
                          return_value=client):
            worker.run()
        return worker, emitted


class ComparisonTests(PrepareCase):
    """Der Vergleich bekommt echte Zeilen und speichert nur Bestaetigtes."""

    def test_the_inventory_is_compared_and_stored_in_the_account(self):
        config.save_account("A", {"refresh_token": "tok-A"})
        state = {}
        _worker, emitted = self.prepare(FakeClient([CLOUD]), state=state)
        payload = emitted["ready"]
        self.assertEqual("d1", payload["comparison"][0]["tolino_id"])
        self.assertTrue(payload["read_status_known"])
        self.assertTrue(payload["progress_status_known"])
        self.assertEqual({}, state, "Der Aufrufer-Zustand bleibt unveraendert")
        stored = config.account_by_name("A")["state"]
        self.assertEqual("d1", stored[UUID]["tolino_id"])
        self.assertEqual(1, stored[UUID]["calibre_id"])

    def test_a_failed_inventory_is_not_an_empty_cloud_library(self):
        config.save_account("A", {"refresh_token": "tok-A"})
        _worker, emitted = self.prepare(
            FakeClient(inventory_error=TolinoError("Bestand unlesbar")))
        self.assertNotIn("ready", emitted)
        self.assertTrue(emitted["failed"])
        self.assertEqual({}, config.account_by_name("A")["state"])

    def test_a_deleted_account_is_not_recreated_by_the_comparison(self):
        _worker, emitted = self.prepare(FakeClient([CLOUD]))
        self.assertIn("ready", emitted)
        self.assertEqual({}, config.account_by_name("A"))

    def test_a_failed_comparison_save_is_not_reported_as_ready(self):
        config.save_account("A", {"refresh_token": "tok-A"})
        broken = tempfile.mkdtemp(prefix="tolino-unreadable-")
        self.addCleanup(shutil.rmtree, broken, True)
        config.PREFERENCES.file_path = broken
        _worker, emitted = self.prepare(FakeClient([CLOUD]))
        self.assertNotIn("ready", emitted)
        self.assertIn("gespeichert", str(emitted.get("failed")))

    def test_a_concurrent_assignment_survives_the_comparison(self):
        config.save_account("A", {"refresh_token": "tok-A"})

        class ConcurrentClient(FakeClient):
            """Speichert waehrend des Bestandsabrufs eine fremde Zuordnung."""

            def inventory(self):
                config.save_account(
                    "A", {"state": {OTHER: {"tolino_id": "b"}}})
                return [CLOUD]

        _worker, emitted = self.prepare(ConcurrentClient([CLOUD]))
        self.assertIn("ready", emitted)
        stored = config.account_by_name("A")["state"]
        self.assertEqual("b", stored[OTHER]["tolino_id"])
        self.assertEqual("d1", stored[UUID]["tolino_id"])


class OptionalDataTests(PrepareCase):
    """Fehlende Lesewerte bleiben «unbekannt», nicht «ungelesen» oder 0 %."""

    def test_an_unknown_read_status_is_not_reported_as_unread(self):
        _worker, emitted = self.prepare(
            FakeClient([CLOUD], read_error=TolinoError("Lesestatus weg")))
        payload = emitted["ready"]
        self.assertFalse(payload["read_status_known"])
        self.assertFalse(payload["comparison"][0]["read"].known)
        self.assertNotIn("reading", payload["state"].get(UUID, {}))

    def test_a_failed_progress_fetch_creates_no_progress_value(self):
        _worker, emitted = self.prepare(
            FakeClient([CLOUD], progress_error=TolinoError("Kein Bookmark")))
        payload = emitted["ready"]
        self.assertFalse(payload["progress_status_known"])
        self.assertFalse(payload["comparison"][0]["progress"].known)
        self.assertNotIn("progress", payload["state"].get(UUID, {})
                         .get("reading", {}))

    def test_a_rejected_login_while_reading_the_status_fails_the_run(self):
        """Ein abgelehnter Token ist kein «unbekannter» Lesewert."""
        config.save_account("A", {"refresh_token": "tok-A"})
        _worker, emitted = self.prepare(FakeClient(
            [CLOUD], read_error=TolinoAuthError("abgelehnt", code="A01")))
        self.assertNotIn("ready", emitted)
        self.assertIn("abgelehnt", emitted["failed"])
        self.assertEqual({}, config.account_by_name("A")["state"])

    def test_a_rejected_login_while_reading_the_progress_fails_the_run(self):
        config.save_account("A", {"refresh_token": "tok-A"})
        _worker, emitted = self.prepare(FakeClient(
            [CLOUD], progress_error=TolinoAuthError("abgelehnt",
                                                    code="A01")))
        self.assertNotIn("ready", emitted)
        self.assertIn("abgelehnt", emitted["failed"])
        self.assertEqual({}, config.account_by_name("A")["state"])

    def test_progress_is_not_requested_while_the_read_status_is_unknown(self):
        client = FakeClient([CLOUD], read_error=TolinoError("weg"))
        _worker, emitted = self.prepare(client)
        self.assertFalse(emitted["ready"]["progress_status_known"])
        self.assertNotIn("progress", client.calls)


class CancelAndStaleTests(PrepareCase):
    """Abbruch liefert nichts Halbes; veraltete IDs nur nach echtem Vergleich."""

    def test_a_cancel_before_the_cloud_call_reports_cancellation(self):
        client = FakeClient([CLOUD])
        worker = self.ui.workers.PrepareWorker(self.context, {1: dict(BOOK)}, {})
        emitted = {}
        worker.ready.connect(lambda payload: emitted.setdefault("ready", payload))
        worker.failed.connect(lambda message: emitted.setdefault("failed", message))
        worker.cancel()
        with patch.object(self.ui.workers.TOKEN_VAULT, "login",
                          return_value=client):
            worker.run()
        self.assertNotIn("ready", emitted)
        self.assertEqual(self.ui.common.CANCEL_MESSAGE, emitted["failed"])
        self.assertEqual([], client.calls)

    def test_a_stale_cloud_id_is_removed_after_a_successful_comparison(self):
        state = {UUID: {"tolino_id": "weg", "calibre_id": 1,
                        "reading": {"read": True, "progress": {"percent": 42}}}}
        _worker, emitted = self.prepare(FakeClient([]), state=state)
        entry = emitted["ready"]["state"][UUID]
        self.assertNotIn("tolino_id", entry)
        self.assertNotIn("reading", entry)

    def test_an_inventory_error_keeps_the_stale_mapping(self):
        state = {UUID: {"tolino_id": "weg", "calibre_id": 1}}
        _worker, emitted = self.prepare(
            FakeClient(inventory_error=TolinoError("weg")), state=state)
        self.assertNotIn("ready", emitted)
        self.assertEqual("weg", state[UUID]["tolino_id"])


if __name__ == "__main__":
    unittest.main()
