"""Ablauf eines Sync-Laufs: Upload, Teilfehler, Abbruch, Lesestatus.

Der Worker wird mit einem Client-Doppel an der Netznaht ausgeführt; alles
andere (Planung, Zustand, Signale, Bericht) ist Produktivcode.
"""
import unittest
from unittest.mock import patch

from ..errors import TolinoApiError
from ..planning import MetaUpdateRecord, SyncJob
from ..run_context import RunContext
from ._support import real_qt_stub, ui_module_for

UUID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class FakeClient:
    """Antwortet wie der tolino-Client, ohne Netz."""

    access = "acc"

    def __init__(self, fail_paths=()):
        self.fail_paths = set(fail_paths)
        self.uploads = []
        self.covers = []
        self.metadata = []
        self.collections = []
        self.read_marks = []
        self.deleted = []

    def login(self):
        return "rot"

    def upload(self, path):
        if path in self.fail_paths:
            raise TolinoApiError("Upload fehlgeschlagen", code="N03")
        self.uploads.append(path)
        return "neu-%d" % len(self.uploads)

    def upload_cover(self, deliverable_id, data, suffix=".jpg"):
        self.covers.append(deliverable_id)

    def update_metadata(self, tolino_id, fields):
        self.metadata.append((tolino_id, dict(fields)))

    def add_to_collection(self, book_id, name, revision=None):
        self.collections.append((book_id, name))

    def collection_pairs(self):
        return set()

    def mark_read_many(self, book_ids, finished=True):
        self.read_marks.append((list(book_ids), finished))

    def delete(self, deliverable_id):
        self.deleted.append(deliverable_id)
        return True


class WorkerCase(unittest.TestCase):
    """Laedt die Oberflaeche (fuer die echten Worker-Klassen) je Test neu."""

    def setUp(self):
        self.ui = ui_module_for(self, qt_stub=real_qt_stub())
        self.context = RunContext.from_values(
            {"account_name": "A", "preferred_formats": ["EPUB"]},
            columns={"id": True, "read": True, "progress": True})

    def run_worker(self, client, jobs=(), removals=(), **kwargs):
        worker = self.ui.workers.SyncWorker(self.context, list(jobs),
                                            list(removals), **kwargs)
        emitted = {}
        worker.completed.connect(
            lambda *args: emitted.setdefault("completed", args))
        worker.failed.connect(
            lambda message: emitted.setdefault("failed", message))
        with patch.object(self.ui.workers.TOKEN_VAULT, "login",
                          return_value=client):
            worker.run()
        return worker, emitted

    def job(self, book_id, book_uuid, path, title="Titel", old_id=None):
        return SyncJob(book_id, book_uuid, "EPUB", old_id, path, None, title)


class UploadTests(WorkerCase):
    """Ein Upload ist nicht rueckgaengig zu machen: er wird sofort bestaetigt."""

    def test_a_successful_upload_confirms_the_new_copy(self):
        state = {UUID: {"tolino_id": "alt", "reading": {"read": True},
                        "collections": ["Serie"]}}
        worker, emitted = self.run_worker(
            FakeClient(), jobs=[self.job(1, UUID, "/buch.epub", old_id="alt")],
            state=state)
        self.assertEqual([(1, "neu-1")], worker.updates)
        self.assertEqual("neu-1", worker.state[UUID]["tolino_id"])
        # Die neue Kopie startet ungelesen; der alte Lesestand gehoert zur
        # ersetzten Kopie.
        self.assertEqual([(1, None)], worker.read_updates)
        self.assertEqual([(1, None)], worker.progress_updates)
        self.assertNotIn("reading", worker.state[UUID])
        self.assertEqual(1, worker.outcome["uploaded"])
        self.assertIn("completed", emitted)
        self.assertNotIn("failed", emitted)

    def test_a_partial_failure_keeps_the_successful_upload(self):
        jobs = [self.job(1, UUID, "/gut.epub", "Gut"),
                self.job(2, OTHER, "/kaputt.epub", "Kaputt")]
        worker, emitted = self.run_worker(
            FakeClient(fail_paths={"/kaputt.epub"}), jobs=jobs)
        self.assertEqual([(1, "neu-1")], worker.updates)
        self.assertIn(UUID, worker.state)
        self.assertNotIn(OTHER, worker.state)
        self.assertEqual((1, 1), (worker.outcome["uploaded"],
                                  worker.outcome["failed"]))
        self.assertIn("completed", emitted)
        self.assertNotIn("failed", emitted)

    def test_a_cancel_stops_before_the_first_upload(self):
        client = FakeClient()
        worker = self.ui.workers.SyncWorker(
            self.context, [self.job(1, UUID, "/buch.epub")], [])
        emitted = {}
        worker.completed.connect(
            lambda *args: emitted.setdefault("completed", args))
        worker.cancel()
        with patch.object(self.ui.workers.TOKEN_VAULT, "login",
                          return_value=client):
            worker.run()
        self.assertEqual([], client.uploads)
        self.assertEqual([], worker.updates)
        self.assertTrue(worker.outcome["cancelled"])
        self.assertEqual("not_started", worker.operations[0]["status"])
        self.assertIn("completed", emitted)


class ReadingAndMetadataTests(WorkerCase):
    """Bestaetigte Cloud-Aenderungen landen im Zustand und in den Spalten."""

    def test_a_confirmed_read_change_reaches_the_column(self):
        state = {UUID: {"tolino_id": "t1", "calibre_id": 7}}
        client = FakeClient()
        worker, _emitted = self.run_worker(
            client, state=state, read_changes=[("t1", 7, True, UUID)])
        self.assertEqual([(["t1"], True)], client.read_marks)
        self.assertEqual([(7, True)], worker.read_updates)
        self.assertTrue(worker.state[UUID]["reading"]["read"])

    def test_a_metadata_patch_is_confirmed_with_its_fingerprint(self):
        record = MetaUpdateRecord(1, UUID, "d1", {"title": "Neu"})
        metadata = {1: {"uuid": UUID, "title": "Neu", "authors": "",
                        "isbn": "", "formats": ["EPUB"]}}
        state = {UUID: {"tolino_id": "d1"}}
        client = FakeClient()
        worker, _emitted = self.run_worker(
            client, meta_updates=[record], state=state, metadata=metadata)
        self.assertEqual([("d1", {"title": "Neu"})], client.metadata)
        self.assertEqual([(1, "d1")], worker.updates)
        self.assertTrue(worker.state[UUID]["meta_verified_fp"])

    def test_a_series_is_added_once_and_remembered(self):
        state = {UUID: {"tolino_id": "d1"}}
        client = FakeClient()
        worker, _emitted = self.run_worker(
            client, state=state, series_map={UUID: "Grenzwelten"})
        self.assertEqual([("d1", "Grenzwelten")], client.collections)
        self.assertIn("Grenzwelten", worker.state[UUID]["collections"])


if __name__ == "__main__":
    unittest.main()
