"""Ablauf eines Sync-Laufs: Upload, Teilfehler, Abbruch, Lesestatus.

Der Worker wird mit einem Client-Doppel an der Netznaht ausgeführt; alles
andere (Planung, Zustand, Signale, Bericht) ist Produktivcode.
"""
import unittest
import uuid
from unittest.mock import patch

from .. import config
from ..comparison import content_fingerprint
from ..errors import TolinoApiError, TolinoAuthError, TolinoError
from ..planning import MetaUpdateRecord, SyncJob
from ..run_context import RunContext
from ._support import (CalibreDatabase, break_settings, real_qt_stub,
                       ui_module_for)

UUID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class FakeClient:
    """Antwortet wie der tolino-Client, ohne Netz."""

    access = "acc"

    def __init__(self, fail_paths=(), upload_results=(), delete_fail=(),
                 inventory_rows=(), inventory_error=None, read_error=None,
                 on_upload=None, on_metadata=None, on_collection=None):
        self.fail_paths = set(fail_paths)
        self.upload_results = list(upload_results)
        self.delete_fail = set(delete_fail)
        self.inventory_rows = list(inventory_rows)
        self.inventory_error = inventory_error
        self.read_error = read_error
        self.on_upload = on_upload
        self.on_metadata = on_metadata
        self.on_collection = on_collection
        self.uploads = []
        self.covers = []
        self.metadata = []
        self.collections = []
        self.read_marks = []
        self.deleted = []
        self.inventory_calls = 0

    def login(self):
        return "rot"

    def upload(self, path):
        if path in self.fail_paths:
            raise TolinoApiError("Upload fehlgeschlagen", code="N03")
        self.uploads.append(path)
        if self.on_upload is not None:
            self.on_upload()
        if self.upload_results:
            return self.upload_results.pop(0)
        return "neu-%d" % len(self.uploads)

    def inventory(self):
        self.inventory_calls += 1
        if self.inventory_error is not None:
            raise self.inventory_error
        return list(self.inventory_rows)

    def upload_cover(self, deliverable_id, data, suffix=".jpg"):
        self.covers.append(deliverable_id)

    def update_metadata(self, tolino_id, fields):
        self.metadata.append((tolino_id, dict(fields)))
        if self.on_metadata is not None:
            self.on_metadata()

    def add_to_collection(self, book_id, name, revision=None):
        self.collections.append((book_id, name))
        if self.on_collection is not None:
            self.on_collection()

    def collection_pairs(self):
        return set()

    def mark_read_many(self, book_ids, finished=True):
        if self.read_error is not None:
            raise self.read_error
        self.read_marks.append((list(book_ids), finished))

    def delete(self, deliverable_id):
        self.deleted.append(deliverable_id)
        if deliverable_id in self.delete_fail:
            raise TolinoApiError("Löschen fehlgeschlagen", code="N03",
                                 detail="HTTP 400")
        return True


class WorkerCase(unittest.TestCase):
    """Laedt die Oberflaeche (fuer die echten Worker-Klassen) je Test neu."""

    def setUp(self):
        self._original_preferences = config.PREFERENCES
        config.PREFERENCES = config.JSONConfig("test-%s" % uuid.uuid4().hex)
        self.addCleanup(setattr, config, "PREFERENCES",
                        self._original_preferences)
        # Der Lauf sichert seinen Zwischenstand in genau diesem Konto.
        config.save_account("A", {"refresh_token": "tok-A",
                                  "hardware_id": "hw"})
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
                        "collections": ["Serie"],
                        "meta_verified_fp": "fp-alt"}}
        worker, emitted = self.run_worker(
            FakeClient(), jobs=[self.job(1, UUID, "/buch.epub", old_id="alt")],
            state=state)
        self.assertEqual([(1, "neu-1")], worker.updates)
        self.assertEqual("neu-1", worker.state[UUID]["tolino_id"])
        # Die neue Kopie startet ungelesen; Lesestand, Sammlungen und
        # Metadaten-Bestaetigung gehoerten zur ersetzten Kopie.
        self.assertEqual([(1, None)], worker.read_updates)
        self.assertEqual([(1, None)], worker.progress_updates)
        for field in ("reading", "collections", "meta_verified_fp"):
            self.assertNotIn(field, worker.state[UUID])
        self.assertEqual(1, worker.outcome["uploaded"])
        self.assertIn("completed", emitted)
        self.assertNotIn("failed", emitted)

    def test_an_upload_records_the_content_fingerprint_of_the_new_copy(self):
        metadata = {1: {"uuid": UUID, "title": "Titel",
                        "format_signatures": {"EPUB": "10:20"}}}
        worker, _emitted = self.run_worker(
            FakeClient(), jobs=[self.job(1, UUID, "/buch.epub")],
            metadata=metadata)
        entry = worker.state[UUID]
        self.assertEqual(content_fingerprint(metadata[1], "EPUB"),
                         entry["content_fp"])
        # Ein Upload ist kein Read-back: die Metadaten gelten nicht als geprueft.
        self.assertNotIn("meta_verified_fp", entry)

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


class StatePersistenceTests(WorkerCase):
    """Ohne gesicherten Zwischenstand bleibt die alte Kopie stehen."""

    def test_a_failed_state_save_stops_before_the_old_copy_is_deleted(self):
        config.save_account("A", {"refresh_token": "tok-A",
                                  "hardware_id": "hw"})
        state = {UUID: {"tolino_id": "alt", "calibre_id": 7}}
        client = FakeClient(upload_results=["neu-1"])
        break_settings(self)
        worker, emitted = self.run_worker(
            client, jobs=[self.job(1, UUID, "/buch.epub", old_id="alt")],
            state=state)
        # Der Upload ist durch, die neue ID steht nur im Speicher: deshalb
        # darf die ersetzte Kopie nicht geloescht werden.
        self.assertEqual(["/buch.epub"], client.uploads)
        self.assertEqual([], client.deleted)
        self.assertEqual("neu-1", worker.state[UUID]["tolino_id"])
        self.assertIn("Zwischenstand", str(emitted.get("failed")))
        self.assertNotIn("completed", emitted)

    def test_a_cancelled_run_signals_once_even_when_saving_fails(self):
        worker = self.ui.workers.SyncWorker(
            self.context, [self.job(1, UUID, "/erst.epub"),
                           self.job(2, OTHER, "/zweit.epub")], [], state={})
        events = []
        worker.completed.connect(lambda *args: events.append("completed"))
        worker.failed.connect(lambda *args: events.append("failed"))
        client = FakeClient(upload_results=["neu-1"],
                            on_upload=worker.cancel)
        break_settings(self)
        with patch.object(self.ui.workers.TOKEN_VAULT, "login",
                          return_value=client):
            worker.run()
        self.assertEqual(["failed"], events)


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

    def test_a_cancelled_metadata_phase_keeps_the_confirmed_patch(self):
        records = [MetaUpdateRecord(1, UUID, "d1", {"title": "Neu"}),
                   MetaUpdateRecord(2, OTHER, "d2", {"title": "Neu"})]
        metadata = {1: {"uuid": UUID, "title": "Neu", "authors": "",
                        "isbn": "", "formats": ["EPUB"]}}
        worker = self.ui.workers.SyncWorker(
            self.context, [], [], meta_updates=records,
            metadata=metadata,
            state={UUID: {"tolino_id": "d1"}, OTHER: {"tolino_id": "d2"}})
        client = FakeClient(on_metadata=worker.cancel)
        with patch.object(self.ui.workers.TOKEN_VAULT, "login",
                          return_value=client):
            worker.run()
        # Der Abbruch beendet die Phase nach dem ersten Patch: der bestaetigte
        # Stand muss trotzdem im Konto stehen, sonst gilt das Buch wieder
        # als «changed».
        self.assertEqual([("d1", {"title": "Neu"})], client.metadata)
        self.assertTrue(
            config.account_by_name("A")["state"][UUID]["meta_verified_fp"])
        self.assertEqual("not_started", worker.operations[1]["status"])

    def test_a_series_is_added_once_and_remembered(self):
        state = {UUID: {"tolino_id": "d1"}}
        client = FakeClient()
        worker, _emitted = self.run_worker(
            client, state=state, series_map={UUID: "Grenzwelten"})
        self.assertEqual([("d1", "Grenzwelten")], client.collections)
        self.assertIn("Grenzwelten", worker.state[UUID]["collections"])

    def test_a_cancelled_collection_phase_keeps_the_confirmed_series(self):
        worker = self.ui.workers.SyncWorker(
            self.context, [], [],
            state={UUID: {"tolino_id": "d1"}, OTHER: {"tolino_id": "d2"}},
            series_map={UUID: "Erste", OTHER: "Zweite"})
        client = FakeClient(on_collection=worker.cancel)
        with patch.object(self.ui.workers.TOKEN_VAULT, "login",
                          return_value=client):
            worker.run()
        # Nur die bestaetigte Zuordnung ist gesichert; die offene bleibt offen.
        self.assertEqual([("d1", "Erste")], client.collections)
        stored = config.account_by_name("A")["state"]
        self.assertEqual(["Erste"], stored[UUID]["collections"])
        self.assertEqual([UUID], list(stored))


class DeleteTests(WorkerCase):
    """Loeschen: nur die ausgewaehlte Kopie, und nur was wirklich weg ist."""

    def test_a_selected_cloud_copy_is_deleted_and_forgotten(self):
        state = {UUID: {"tolino_id": "d1", "calibre_id": 7}}
        client = FakeClient()
        worker, _emitted = self.run_worker(
            client, state=state, removals=[(UUID, "d1")])
        self.assertEqual(["d1"], client.deleted)
        self.assertEqual({}, worker.state)
        self.assertEqual([(7, "")], worker.updates)
        self.assertEqual(1, worker.outcome["deleted"])

    def test_every_rejected_deletion_reads_a_fresh_cloud_inventory(self):
        state = {UUID: {"tolino_id": "d1", "calibre_id": 1},
                 OTHER: {"tolino_id": "d2", "calibre_id": 2}}
        client = FakeClient(delete_fail={"d1", "d2"},
                            inventory_rows=[{"tolino_id": "d1"}])
        worker, _emitted = self.run_worker(
            client, state=state,
            removals=[(UUID, "d1"), (OTHER, "d2")])
        # Jede Ablehnung prueft den Bestand JETZT: d2 fehlt und ist damit
        # erledigt, d1 steht noch da.
        self.assertEqual(2, client.inventory_calls)
        self.assertEqual((1, 1), (worker.outcome["failed"],
                                  worker.outcome["skipped"]))

    def test_a_copy_already_gone_from_the_cloud_counts_as_skipped(self):
        state = {UUID: {"tolino_id": "d1", "calibre_id": 7}}
        client = FakeClient(delete_fail={"d1"}, inventory_rows=[])
        worker, _emitted = self.run_worker(
            client, state=state, removals=[(UUID, "d1")])
        self.assertEqual((0, 1), (worker.outcome["deleted"],
                                  worker.outcome["skipped"]))
        self.assertEqual({}, worker.state)
        self.assertEqual([(7, "")], worker.updates)

    def test_an_unreadable_inventory_is_not_an_already_gone_copy(self):
        state = {UUID: {"tolino_id": "d1", "calibre_id": 7}}
        client = FakeClient(delete_fail={"d1"},
                            inventory_error=TolinoError("Bestand weg"))
        worker, _emitted = self.run_worker(
            client, state=state, removals=[(UUID, "d1")])
        self.assertEqual(1, worker.outcome["failed"])
        self.assertEqual("d1", worker.state[UUID]["tolino_id"])

    def test_a_copy_still_in_the_cloud_keeps_the_error_and_the_state(self):
        state = {UUID: {"tolino_id": "d1", "calibre_id": 7}}
        client = FakeClient(delete_fail={"d1"},
                            inventory_rows=[{"tolino_id": "d1"}])
        worker, _emitted = self.run_worker(
            client, state=state, removals=[(UUID, "d1")])
        self.assertEqual(1, worker.outcome["failed"])
        self.assertEqual("d1", worker.state[UUID]["tolino_id"])

    def test_a_failed_deletion_does_not_skip_the_following_one(self):
        state = {UUID: {"tolino_id": "d1", "calibre_id": 1},
                 OTHER: {"tolino_id": "d2", "calibre_id": 2}}
        client = FakeClient(delete_fail={"d1"},
                            inventory_rows=[{"tolino_id": "d1"}])
        worker, _emitted = self.run_worker(
            client, state=state,
            removals=[(UUID, "d1"), (OTHER, "d2")])
        self.assertEqual((1, 1), (worker.outcome["failed"],
                                  worker.outcome["deleted"]))
        # Der gescheiterte Eintrag bleibt, der erfolgreiche ist weg.
        self.assertIn(UUID, worker.state)
        self.assertNotIn(OTHER, worker.state)


class ReplaceTests(WorkerCase):
    """Ersetzen: die alte Kopie wird nur nach echtem neuen Upload geloescht."""

    def test_a_new_copy_deletes_the_previous_cloud_copy(self):
        state = {UUID: {"tolino_id": "alt", "calibre_id": 1}}
        client = FakeClient(upload_results=["neu-1"])
        worker, _emitted = self.run_worker(
            client, jobs=[self.job(1, UUID, "/buch.epub", old_id="alt")],
            state=state)
        self.assertEqual(["alt"], client.deleted)
        self.assertEqual("neu-1", worker.state[UUID]["tolino_id"])
        self.assertEqual(1, worker.outcome["deleted"])

    def test_the_same_id_from_the_upload_is_never_deleted(self):
        state = {UUID: {"tolino_id": "d1", "calibre_id": 1}}
        client = FakeClient(upload_results=["d1"])
        worker, _emitted = self.run_worker(
            client, jobs=[self.job(1, UUID, "/buch.epub", old_id="d1")],
            state=state)
        self.assertEqual([], client.deleted)
        self.assertEqual("d1", worker.state[UUID]["tolino_id"])

    def test_a_read_mark_never_targets_the_replaced_copy(self):
        state = {UUID: {"tolino_id": "alt", "calibre_id": 1}}
        client = FakeClient(upload_results=["neu-1"])
        worker, _emitted = self.run_worker(
            client, jobs=[self.job(1, UUID, "/buch.epub", old_id="alt")],
            state=state, read_changes=[("alt", 1, True, UUID)])
        # Die Marke ginge an die gelöschte Kopie: sie entfällt, und die
        # Spalte bleibt auf dem Stand der neuen Kopie (ungelesen).
        self.assertEqual([], client.read_marks)
        self.assertEqual([(1, None)], worker.read_updates)
        self.assertNotIn("reading", worker.state[UUID])
        self.assertEqual(0, worker.outcome["read_marked"])
        read_op = [op for op in worker.operations if op["kind"] == "read"][0]
        self.assertEqual(("skipped", 0), (read_op["status"], read_op["count"]))

    def test_a_failing_old_copy_delete_keeps_the_new_upload(self):
        state = {UUID: {"tolino_id": "alt", "calibre_id": 1}}
        client = FakeClient(upload_results=["neu-1"], delete_fail={"alt"},
                            inventory_rows=[{"tolino_id": "alt"}])
        worker, emitted = self.run_worker(
            client, jobs=[self.job(1, UUID, "/buch.epub", old_id="alt")],
            state=state)
        self.assertEqual("neu-1", worker.state[UUID]["tolino_id"])
        self.assertEqual(1, worker.outcome["failed"])
        self.assertIn("completed", emitted)
        self.assertNotIn("failed", emitted)


class FailureTests(WorkerCase):
    """Auth-Fehler, Abbruch und Lesestatus hinterlassen den richtigen Zustand."""

    def test_an_authentication_error_stops_the_run(self):
        worker = self.ui.workers.SyncWorker(
            self.context, [self.job(1, UUID, "/buch.epub")], [])
        emitted = {}
        worker.completed.connect(
            lambda *args: emitted.setdefault("completed", args))
        worker.failed.connect(
            lambda message: emitted.setdefault("failed", message))
        with patch.object(self.ui.workers.TOKEN_VAULT, "login",
                          side_effect=TolinoAuthError("abgelaufen", code="A01")):
            worker.run()
        self.assertIn("failed", emitted)
        self.assertNotIn("completed", emitted)
        self.assertEqual([], worker.updates)

    def test_a_cancel_after_a_successful_upload_keeps_it(self):
        worker = self.ui.workers.SyncWorker(
            self.context,
            [self.job(1, UUID, "/erst.epub"),
             self.job(2, OTHER, "/zweit.epub")], [], state={})
        client = FakeClient(upload_results=["neu-1"],
                            on_upload=worker.cancel)
        emitted = []
        worker.completed.connect(lambda *args: emitted.append("completed"))
        worker.failed.connect(lambda message: emitted.append("failed"))
        with patch.object(self.ui.workers.TOKEN_VAULT, "login",
                          return_value=client):
            worker.run()
        self.assertTrue(worker.outcome["cancelled"])
        self.assertEqual([(1, "neu-1")], worker.updates)
        self.assertEqual("not_started", worker.operations[1]["status"])
        # Genau EIN terminales Signal -- und zwar kein Fehler.
        self.assertEqual(["completed"], emitted)

    def test_a_failed_read_change_is_not_reported_or_stored_as_done(self):
        state = {UUID: {"tolino_id": "d1", "calibre_id": 7}}
        client = FakeClient(
            read_error=TolinoApiError("Markieren fehlgeschlagen", code="N03"))
        worker, _emitted = self.run_worker(
            client, state=state, read_changes=[("d1", 7, True, UUID)])
        self.assertEqual([], worker.read_updates)
        self.assertNotIn("reading", worker.state[UUID])
        self.assertEqual(1, worker.outcome["failed"])


class PersistenceTests(WorkerCase):
    """Der gespeicherte Kontostand -- nicht nur der Worker-Zustand."""

    def test_a_successful_upload_is_stored_in_the_account_immediately(self):
        config.save_account("A", {"refresh_token": "tok-A"})
        jobs = [self.job(1, UUID, "/gut.epub", "Gut"),
                self.job(2, OTHER, "/kaputt.epub", "Kaputt")]
        self.run_worker(FakeClient(fail_paths={"/kaputt.epub"}), jobs=jobs)
        stored = config.account_by_name("A")["state"]
        self.assertEqual("neu-1", stored[UUID]["tolino_id"])
        self.assertNotIn(OTHER, stored)

    def test_another_account_is_not_touched(self):
        config.save_account("A", {"refresh_token": "tok-A"})
        config.save_account("B", {"refresh_token": "tok-B",
                                  "state": {"fremd": {"tolino_id": "x"}}})
        self.run_worker(FakeClient(),
                        jobs=[self.job(1, UUID, "/buch.epub")])
        self.assertEqual({"fremd": {"tolino_id": "x"}},
                         config.account_by_name("B")["state"])
        self.assertEqual("neu-1",
                         config.account_by_name("A")["state"][UUID]["tolino_id"])

    def test_a_stale_state_copy_keeps_foreign_assignments(self):
        config.save_account("A", {"refresh_token": "tok-A",
                                  "state": {OTHER: {"tolino_id": "fremd"}}})
        # Der Lauf startet mit einer älteren Kopie, in der OTHER fehlt.
        self.run_worker(FakeClient(),
                        jobs=[self.job(1, UUID, "/buch.epub")], state={})
        stored = config.account_by_name("A")["state"]
        self.assertEqual("fremd", stored[OTHER]["tolino_id"])
        self.assertEqual("neu-1", stored[UUID]["tolino_id"])

    def test_a_later_save_keeps_a_foreign_change_to_an_earlier_book(self):
        config.save_account("A", {"refresh_token": "tok-A"})
        uploads = []

        def foreign_change():
            # Eine andere Instanz schreibt, nachdem der erste Upload gesichert
            # ist: derselbe Eintrag, ein anderes Feld.
            uploads.append(True)
            if len(uploads) < 2:
                return
            stored = config.account_by_name("A")["state"]
            stored[UUID]["tolino_id"] = "extern"
            config.save_account("A", {"state": stored})

        jobs = [self.job(1, UUID, "/erst.epub"),
                self.job(2, OTHER, "/zweit.epub")]
        self.run_worker(FakeClient(on_upload=foreign_change), jobs=jobs)
        self.assertEqual("extern",
                         config.account_by_name("A")["state"][UUID]["tolino_id"])

    def test_a_foreign_write_to_the_replaced_copy_is_not_inherited(self):
        """Waehrend `upload()` schreibt eine andere Instanz die ALTE ID.

        Deren Lesestand und Sammlungen gehoeren zur ersetzten Kopie und
        duerfen nicht im Eintrag mit der neuen tolino-ID landen.
        """
        start = {UUID: {"tolino_id": "alt", "calibre_id": 1}}
        config.save_account("A", {"refresh_token": "tok-A",
                                  "state": start})

        def foreign_change():
            stored = config.account_by_name("A")["state"]
            stored[UUID].update({"reading": {"read": True},
                                 "collections": ["Serie"]})
            config.save_account("A", {"state": stored})

        self.run_worker(
            FakeClient(upload_results=["neu-1"], on_upload=foreign_change),
            jobs=[self.job(1, UUID, "/buch.epub", old_id="alt")],
            state=start)
        stored = config.account_by_name("A")["state"][UUID]
        self.assertEqual("neu-1", stored["tolino_id"])
        self.assertNotIn("reading", stored)
        self.assertNotIn("collections", stored)

    def test_a_removed_account_is_not_created_by_a_late_callback(self):
        # Das letzte Konto laesst sich nicht entfernen: ein zweites muss her.
        config.save_account("B", {"refresh_token": "tok-B"})
        self.assertTrue(config.remove_account("A"))
        self.run_worker(FakeClient(),
                        jobs=[self.job(1, UUID, "/buch.epub")])
        self.assertEqual({}, config.account_by_name("A"))


class LocalSnapshotTests(WorkerCase):
    """Der lokale Snapshot meldet Metadaten oder einen Fehler -- nie beides."""

    def run_snapshot(self, database):
        worker = self.ui.workers.LocalMetadataWorker(
            database, self.context, {"marke": True})
        emitted = {}
        worker.ready.connect(
            lambda payload: emitted.setdefault("ready", payload))
        worker.failed.connect(
            lambda message: emitted.setdefault("failed", message))
        worker.run()
        return worker, emitted

    def database(self, books):
        return CalibreDatabase(books=list(books), metadata=books)

    def test_a_snapshot_carries_context_state_and_the_books(self):
        books = {1: {"uuid": UUID, "title": "Der Schwarm",
                     "authors": "Frank Schätzing", "formats": ["EPUB"]}}
        _worker, emitted = self.run_snapshot(self.database(books))
        self.assertEqual({"ready"}, set(emitted))
        payload = emitted["ready"]
        self.assertIs(self.context, payload["context"])
        self.assertEqual({"marke": True}, payload["state"])
        self.assertEqual("Der Schwarm", payload["metadata"][1]["title"])

    def test_a_failing_library_is_reported_instead_of_an_empty_snapshot(self):
        database = self.database({})

        def explode():
            raise OSError("Bibliothek gesperrt")

        database.api.all_book_ids = explode
        _worker, emitted = self.run_snapshot(database)
        self.assertEqual({"failed"}, set(emitted))
        self.assertIn("Bibliothek gesperrt", emitted["failed"])


if __name__ == "__main__":
    unittest.main()
