"""Planung und Bestandsabgleich an ihren oeffentlichen Funktionen.

Diese Tests bilden die Entscheidungen ab, die der Nutzer im Vergleichsfenster
sieht: was hochgeladen, was gepatcht, was als Prueffall stehen bleibt und was
als identisch gilt. Die Auswahl des Nutzers wird zu echten Jobs.
"""
import os
import shutil
import tempfile
import types
import unittest

from ..comparison import (compare_inventory, comparison_progress_updates,
                          comparison_read_updates, content_fingerprint,
                          metadata_fingerprint, store_reading_state)
from ..inventory import normalize_inventory_record, parse_inventory
from ..library import normalize_formats
from ..matching import (authors_match, is_valid_isbn, isbn_from_identifier,
                        normalize_isbn)
from ..planning import (NOTHING_TO_DO_TEXT, local_metadata, metadata_update_fields,
                        nothing_to_do_text, plan_actions, plan_collection_updates,
                        plan_metadata_updates, plan_sync, remember_collection,
                        PlanCounts,
                        series_map_from_metadata, syncable_metadata)
from ..run_context import RunContext
from ._support import CalibreDatabase

UUID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def book(**extra):
    metadata = {"uuid": UUID, "title": "Der Schwarm", "authors": "Frank Schätzing",
                "isbn": "9780306406157", "formats": ["EPUB"], "ignored": False}
    metadata.update(extra)
    return metadata


def cloud(**extra):
    record = {"tolino_id": "d1", "title": "Der Schwarm",
              "authors": "Frank Schätzing", "isbn": "9780306406157"}
    record.update(extra)
    return record


class PlanSyncTests(unittest.TestCase):
    """Hochladen: nur ausgewaehlte Buecher im bevorzugten Format."""

    def test_only_selected_book_with_preferred_format_is_planned(self):
        metadata = {1: book(), 2: book(title="Nur PDF", formats=["PDF"]),
                    # EPUB, aber nicht ausgewaehlt: prueft den Auswahlfilter
                    # unabhaengig vom Formatfilter.
                    3: book(uuid=OTHER, title="Nicht gewählt")}
        uploads = plan_sync(metadata, {}, ["EPUB"], upload_book_ids={1, 2})
        self.assertEqual([(1, UUID, "EPUB", None)],
                         [(item.book_id, item.book_uuid, item.format_name,
                           item.old_id) for item in uploads])

    def test_previous_cloud_copy_is_carried_along(self):
        uploads = plan_sync({1: book()}, {UUID: {"tolino_id": "alt"}},
                            ["EPUB"], upload_book_ids={1})
        self.assertEqual("alt", uploads[0].old_id)

    def test_books_without_uuid_are_skipped(self):
        self.assertEqual([], plan_sync({1: book(uuid="keine-uuid")}, [],
                                       ["EPUB"], upload_book_ids={1}))

    def test_ignored_books_are_not_part_of_the_snapshot(self):
        # `plan_actions` prueft denselben Filter noch einmal am Eingang.
        self.assertEqual({2: book()},
                         syncable_metadata({1: book(ignored=True),
                                            2: book()}))


class MetadataUpdateTests(unittest.TestCase):
    """Metadaten: nur bei vorhandener Cloud-Kopie und nie zusaetzlich zum Upload."""

    def test_selected_book_with_cloud_copy_gets_its_fields(self):
        updates = plan_metadata_updates({1: book()}, {UUID: {"tolino_id": "d1"}},
                                        [], {1})
        self.assertEqual(1, len(updates))
        self.assertEqual((1, "d1"), (updates[0].book_id, updates[0].tolino_id))
        self.assertEqual({"title": "Der Schwarm",
                          "author": "Frank Schätzing",
                          "isbn": "9780306406157"}, updates[0].fields)

    def test_books_that_are_uploaded_are_not_patched(self):
        uploads = plan_sync({1: book()}, {}, ["EPUB"], upload_book_ids={1})
        self.assertEqual([], plan_metadata_updates(
            {1: book()}, {UUID: {"tolino_id": "d1"}}, uploads, {1}))

    def test_missing_values_and_unselected_books_are_skipped(self):
        empty = book(title="", authors="", isbn="")
        self.assertEqual({}, metadata_update_fields(empty))
        self.assertEqual([], plan_metadata_updates(
            {1: empty}, {UUID: {"tolino_id": "d1"}}, [], {1}))
        self.assertEqual([], plan_metadata_updates(
            {1: book()}, {UUID: {"tolino_id": "d1"}}, [], {2}))


class CollectionTests(unittest.TestCase):
    """Serien: einmal uebertragen, dann im Zustand gemerkt."""

    def test_a_series_is_planned_once_and_then_remembered(self):
        series = series_map_from_metadata({1: book(series="Grenzwelten")})
        self.assertEqual({UUID: "Grenzwelten"}, series)
        state = {UUID: {"tolino_id": "d1"}}
        self.assertEqual([(UUID, "d1", "Grenzwelten")],
                         plan_collection_updates(series, state))
        self.assertTrue(remember_collection(state, UUID, "Grenzwelten"))
        self.assertFalse(remember_collection(state, UUID, "Grenzwelten"))
        self.assertEqual([], plan_collection_updates(series, state))

    def test_books_without_series_or_cloud_copy_are_ignored(self):
        self.assertEqual({}, series_map_from_metadata({1: book()}))
        self.assertEqual([], plan_collection_updates(
            {UUID: "Serie"}, {UUID: {}}))


class ComparisonTests(unittest.TestCase):
    """Der Abgleich ordnet konservativ zu und laesst Unklares zur Pruefung."""

    def test_stored_id_matches_and_reports_identical(self):
        rows = compare_inventory({1: book()}, {UUID: {"tolino_id": "d1"}},
                                 [cloud()], ["EPUB"])
        self.assertEqual([(1, "identical", "d1")],
                         [(row["book_id"], row["status"], row["tolino_id"])
                          for row in rows if row["book_id"]])

    def test_a_metadata_change_is_reported_as_changed(self):
        rows = compare_inventory({1: book(title="Neuer Titel")},
                                 {UUID: {"tolino_id": "d1"}}, [cloud()],
                                 ["EPUB"])
        self.assertEqual("changed", rows[0]["status"])

    def test_a_changed_file_is_reported_as_changed(self):
        before = book(format_signatures={"EPUB": "10:20"})
        state = {UUID: {"tolino_id": "d1",
                        "content_fp": content_fingerprint(before, "EPUB")}}
        rows = compare_inventory(
            {1: book(format_signatures={"EPUB": "30:40"})}, state,
            [cloud()], ["EPUB"])
        # Gleiche Metadaten, andere Datei: der Upload wird empfohlen.
        self.assertEqual("changed", rows[0]["status"])

    def test_isbn_or_title_and_author_match_without_stored_id(self):
        for metadata, reason in ((book(uuid="a" * 8 + "-1111-4111-8111-111111111111"), "isbn"),
                                 (book(isbn=""), "title_author")):
            with self.subTest(reason=reason):
                rows = compare_inventory({1: metadata}, {}, [cloud()], ["EPUB"])
                self.assertEqual(reason, rows[0]["match_reason"])
                self.assertEqual("d1", rows[0]["tolino_id"])

    def test_a_title_alone_never_matches(self):
        rows = compare_inventory({1: book(authors="Jemand anderes", isbn="")},
                                 {}, [cloud()], ["EPUB"])
        self.assertEqual(("review", "title_only"),
                         (rows[0]["status"], rows[0]["match_reason"]))
        self.assertEqual("", rows[0]["tolino_id"])

    def test_duplicate_cloud_titles_stay_for_review(self):
        rows = compare_inventory({1: book(isbn="")}, {},
                                 [cloud(tolino_id="d1"), cloud(tolino_id="d2")],
                                 ["EPUB"])
        self.assertEqual("review", rows[0]["status"])
        self.assertEqual("ambiguous", rows[0]["match_reason"])

    def test_new_and_cloud_only_books_are_both_visible(self):
        other = book(uuid="22222222-2222-4222-8222-222222222222",
                     title="Fremdes Buch", isbn="")
        rows = compare_inventory({1: other}, {}, [cloud()], ["EPUB"])
        statuses = {row["status"] for row in rows}
        self.assertEqual({"new_in_calibre", "only_tolino"}, statuses)

    def test_ignored_books_stay_visible_without_action(self):
        rows = compare_inventory({1: book(ignored=True)}, {}, [cloud()],
                                 ["EPUB"])
        self.assertEqual("ignored", rows[0]["group"])

    def test_an_isbn_match_never_steals_a_stored_assignment(self):
        other = "22222222-2222-4222-8222-222222222222"
        rows = compare_inventory(
            {1: book(), 2: book(uuid=other)},
            {other: {"tolino_id": "d1"}}, [cloud()], ["EPUB"])
        by_book = {row["book_id"]: row for row in rows if row["book_id"]}
        # Die gespeicherte Zuordnung bleibt beim späteren Buch; das frühere
        # bekommt keinen zweiten Anspruch auf dieselbe Kopie.
        self.assertEqual("d1", by_book[2]["tolino_id"])
        self.assertEqual("", by_book[1]["tolino_id"])

    def test_a_new_cloud_id_does_not_inherit_the_metadata_confirmation(self):
        state = {UUID: {"tolino_id": "gone",
                        "meta_verified_fp": metadata_fingerprint(book())}}
        rows = compare_inventory({1: book()}, state,
                                 [cloud(title="Ein anderer Titel")],
                                 ["EPUB"])
        self.assertEqual("d1", rows[0]["tolino_id"])
        self.assertEqual("changed", rows[0]["status"])
        self.assertNotIn("meta_verified_fp", state[UUID])

    def test_a_new_cloud_id_drops_the_values_of_the_old_copy(self):
        state = {UUID: {"tolino_id": "alt", "reading": {"read": True},
                        "collections": ["Serie"]}}
        rows = compare_inventory({1: book()}, state, [cloud()], ["EPUB"])
        self.assertEqual("d1", state[UUID]["tolino_id"])
        self.assertNotIn("reading", state[UUID])
        self.assertNotIn("collections", state[UUID])
        self.assertEqual("d1", rows[0]["tolino_id"])

    def test_initials_match_a_full_author_name(self):
        rows = compare_inventory({1: book(authors="J. Doe", isbn="")}, {},
                                 [cloud(authors="Jane Doe", isbn="")],
                                 ["EPUB"])
        self.assertEqual(("d1", "title_author"),
                         (rows[0]["tolino_id"], rows[0]["match_reason"]))


class NormalizationTests(unittest.TestCase):
    """Cloud-Datensaetze und Suchschluessel kommen in einer Form an."""

    def test_cloud_records_are_normalized(self):
        record = normalize_inventory_record(
            {"epubMetaData": {"deliverable": [{"deliverableId": "d1"}],
                              "title": "Der Schwarm",
                              "authors": ["Frank Schätzing"],
                              "isbn": "978-0-306-40615-7"}})
        self.assertEqual({"tolino_id": "d1", "title": "Der Schwarm",
                          "authors": "Frank Schätzing",
                          "isbn": "978-0-306-40615-7"}, record)

    def test_isbn_and_author_keys_are_tolerant_but_strict(self):
        self.assertEqual("9780306406157", normalize_isbn("978-0-306-40615-7"))
        self.assertTrue(is_valid_isbn("9780306406157"))
        self.assertFalse(is_valid_isbn("9780306406158"))
        self.assertEqual("9780306406157",
                         isbn_from_identifier("DT0400.9780306406157_A123"))
        self.assertTrue(authors_match("Frank Schätzing", "Frank Schaetzing"))
        self.assertFalse(authors_match("Frank Schätzing", "Frank Schmidt"))

    def test_formats_are_normalized(self):
        self.assertEqual(["EPUB"], normalize_formats("epub, PDF")[:1])
        self.assertEqual(["PDF"], normalize_formats(["pdf"]))

    def test_fingerprints_need_a_file_signature(self):
        self.assertEqual("", content_fingerprint(book(), "EPUB"))
        signed = book(format_signatures={"EPUB": "1234:5678"})
        self.assertNotEqual(content_fingerprint(signed, "EPUB"),
                            content_fingerprint(
                                book(format_signatures={"EPUB": "1234:9999"}),
                                "EPUB"))


class LocalMetadataTests(unittest.TestCase):
    """Der Calibre-Snapshot, aus dem der Vergleich entsteht."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="metadata-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.epub = os.path.join(self.tmp, "buch.epub")
        self.pdf = os.path.join(self.tmp, "buch.pdf")
        for path in (self.epub, self.pdf):
            with open(path, "wb") as handle:
                handle.write(b"inhalt")
        self.books = {
            1: {"uuid": UUID, "title": "Der Schwarm",
                "authors": ["Frank", "Schätzing"],
                "identifiers": {"isbn": "9780306406157"},
                "formats": "EPUB, PDF", "series": "Grenzwelten",
                "#tolino_ignore": "ja", "#tolino_read": "nein"},
            2: {"uuid": "keine-uuid", "title": "Ohne UUID",
                "authors": "Autor Zwei", "isbn": 4711,
                "formats": ["PDF"]},
        }
        self.paths = {(1, "EPUB"): self.epub, (1, "PDF"): self.pdf,
                      (2, "PDF"): os.path.join(self.tmp, "fehlt.pdf")}

    def metadata(self, columns=None):
        context = RunContext.from_values(
            {"preferred_formats": ["EPUB", "PDF"]},
            columns={"read": True} if columns is None else columns,
            library_scope="abcdef123456")
        database = CalibreDatabase(books=list(self.books), metadata=self.books,
                                   paths=self.paths)
        return local_metadata(database, context)

    def test_the_snapshot_carries_formats_isbn_series_and_flags(self):
        metadata = self.metadata()
        first = metadata[1]
        self.assertEqual("Der Schwarm", first["title"])
        self.assertEqual("Frank, Schätzing", first["authors"])
        # ISBN kommt aus den identifiers, wenn das Feld selbst leer ist.
        self.assertEqual("9780306406157", first["isbn"])
        self.assertEqual(["EPUB", "PDF"], first["formats"])
        self.assertEqual("Grenzwelten", first["series"])
        self.assertTrue(first["ignored"])
        self.assertFalse(first["calibre_read"])
        self.assertEqual({"EPUB", "PDF"}, set(first["format_signatures"]))
        self.assertIn(":", first["format_signatures"]["EPUB"])

        second = metadata[2]
        self.assertTrue(second["uuid"].startswith("calibre-abcdef123456-"))
        self.assertEqual("4711", second["isbn"])
        self.assertFalse(second["ignored"])
        # Fehlende Datei: keine Signatur, aber das Buch bleibt im Snapshot.
        self.assertEqual({}, second["format_signatures"])

    def test_the_read_column_is_only_read_when_it_is_configured(self):
        metadata = self.metadata(columns={})
        self.assertNotIn("calibre_read", metadata[1])


def selection(uploads=(), metadata=(), deletions=(), reads=()):
    """Auswahl aus dem Vergleichsfenster -- nur die vier Listen."""
    return {"uploads": set(uploads), "metadata": set(metadata),
            "cloud_deletions": list(deletions), "read_changes": list(reads)}


class PlanActionsTests(unittest.TestCase):
    """Von der Auswahl zu konkreten Jobs -- ohne zusaetzliche Aktionen."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="plan-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "book.epub")
        with open(self.path, "wb") as handle:
            handle.write(b"inhalt")
        self.context = types.SimpleNamespace(preferred_formats=["EPUB"])

    def plan(self, chosen, metadata, state=None, paths=None, cover=None):
        database = CalibreDatabase(
            books=[1], paths={(1, "EPUB"): self.path} if paths is None else paths,
            cover=b"\xff\xd8\xffJPEG" if cover is None else cover)
        dialog = types.SimpleNamespace(
            gui=types.SimpleNamespace(current_db=database))
        return plan_actions(dialog, self.context, metadata, state or {},
                            chosen)

    def test_a_selected_book_with_a_preferred_format_becomes_one_upload(self):
        result = self.plan(selection(uploads={1}), {1: book()})
        self.assertEqual(1, len(result["jobs"]))
        job = result["jobs"][0]
        self.assertEqual((1, UUID, "EPUB", None),
                         (job.book_id, job.book_uuid, job.format_name,
                          job.old_id))
        self.assertEqual(self.path, job.path)
        self.assertEqual(b"\xff\xd8\xffJPEG", job.cover)
        self.assertEqual([], result["skipped_uploads"])

    def test_a_book_without_the_preferred_format_is_skipped_with_a_reason(self):
        result = self.plan(selection(uploads={1}),
                           {1: book(formats=["PDF"])})
        self.assertEqual([], result["jobs"])
        self.assertIn("kein Format EPUB", result["skipped_uploads"][0])
        self.assertEqual(1, result["selected_without_action"]["uploads"])

    def test_an_ignored_book_never_reaches_the_plan(self):
        result = self.plan(selection(uploads={1}, metadata={1}),
                           {1: book(ignored=True)})
        self.assertEqual(([], []), (result["jobs"], result["meta_updates"]))

    def test_an_uploaded_book_gets_no_additional_metadata_patch(self):
        result = self.plan(selection(uploads={1}, metadata={1}), {1: book()},
                           state={UUID: {"tolino_id": "d1"}})
        self.assertEqual(1, len(result["jobs"]))
        self.assertEqual([], result["meta_updates"])

    def test_metadata_is_patched_only_for_a_selected_mapped_book(self):
        result = self.plan(selection(metadata={1}), {1: book()},
                           state={UUID: {"tolino_id": "d1"}})
        self.assertEqual([], result["jobs"])
        self.assertEqual([(1, "d1")], [(item.book_id, item.tolino_id)
                                        for item in result["meta_updates"]])

    def test_a_cloud_deletion_needs_a_concrete_deliverable_id(self):
        result = self.plan(selection(deletions=[
            (UUID, "d1", "Der Schwarm"), (UUID, "  ", "ohne ID")]),
            {1: book()})
        self.assertEqual([(UUID, "d1")], result["removals"])
        self.assertEqual({"d1": "Der Schwarm"}, result["removal_titles"])

    def test_read_changes_are_passed_through_unchanged(self):
        changes = [("d1", 1, True, UUID)]
        result = self.plan(selection(reads=changes), {1: book()})
        self.assertEqual(changes, result["read_changes"])

    def test_a_missing_local_file_is_skipped_without_a_cloud_action(self):
        os.remove(self.path)
        result = self.plan(selection(uploads={1}), {1: book()})
        self.assertEqual([], result["jobs"])
        self.assertIn("Datei nicht gefunden", result["skipped_uploads"][0])
        self.assertEqual([], result["removals"])

    def test_an_unknown_cover_does_not_block_the_upload(self):
        result = self.plan(selection(uploads={1}), {1: book()},
                           cover=b"kein-bild")
        self.assertEqual(1, len(result["jobs"]))
        self.assertIsNone(result["jobs"][0].cover)

    def test_counts_and_texts_describe_the_same_plan(self):
        preparation = {
            "removals": [(UUID, "d1")],
            "meta_updates": [(1, "d1")],
            "collections": 2,
            "read_changes": [("d1", 1, True, UUID), ("d2", 2, False, UUID)],
            "selected_without_action": {"uploads": 2, "metadata": 0},
        }
        counts = PlanCounts(preparation, uploads=1)
        self.assertEqual((1, 1, 1, 2, 1, 1, 7),
                         (counts.uploads, counts.removals, counts.metadata,
                          counts.collections, counts.read, counts.unread,
                          counts.steps))
        text = counts.text()
        for part in ("1 Upload", "1 Löschung", "1 Metadaten",
                     "2 Sammlungen", "1 gelesen", "1 ungelesen"):
            self.assertIn(part, text)
        self.assertIn("2 ausgewählte Aktionen",
                      nothing_to_do_text(preparation))
        self.assertEqual(NOTHING_TO_DO_TEXT, nothing_to_do_text({}))


class InventoryParsingTests(unittest.TestCase):
    """Ein defektes Inventar ist kein leeres Konto."""

    def test_a_valid_empty_library_is_empty_but_usable(self):
        self.assertEqual(([], [], True),
                         parse_inventory({"PublicationInventory":
                                          {"edata": []}}))

    def test_a_missing_or_unknown_container_is_not_an_empty_library(self):
        cases = ({},
                 {"PublicationInventory": {"unbekannt": []}},
                 {"PublicationInventory": "kein Objekt"})
        for data in cases:
            with self.subTest(data=data):
                records, problems, ok = parse_inventory(data)
                self.assertEqual([], records)
                self.assertTrue(problems)
                self.assertFalse(ok)

    def test_good_records_survive_a_broken_neighbour(self):
        records, problems, ok = parse_inventory({"PublicationInventory": {
            "edata": [{"deliverableId": "d1", "title": "Gut",
                       "authors": "Autor"},
                      {"title": "Ohne Kennung", "authors": "Autor"}]}})
        # Der gute Datensatz bleibt sichtbar; der defekte Nachbar erscheint
        # ohne Kennung. Der Bestand gilt trotzdem als unvollständig, damit
        # keine Zuordnung zu einem fehlenden Datensatz gelöscht wird.
        self.assertEqual(["d1", ""], [item["tolino_id"] for item in records])
        self.assertIn("Datensatz ohne Kennung", problems)
        self.assertFalse(ok)

    def test_purchase_deliverables_provide_the_deletable_id(self):
        for deliverable in ({"deliverableId": "d1"},
                            {"identifier": "DT0400.9780306406157_A1"}):
            with self.subTest(deliverable=deliverable):
                record = normalize_inventory_record({"epubMetaData": {
                    "deliverable": [deliverable], "title": "Gekauft"}})
                self.assertEqual(deliverable.get("deliverableId")
                                 or deliverable["identifier"],
                                 record["tolino_id"])

    def test_a_product_identifier_is_not_a_deletable_copy_id(self):
        record = normalize_inventory_record({"epubMetaData": {
            "identifier": "DT0400.9780306406157_A1", "title": "Gekauft"}})
        self.assertEqual("", record["tolino_id"])
        self.assertEqual("9780306406157", record["isbn"])

    def test_an_author_is_normalized_from_text_object_or_list(self):
        cases = (("Frank Schätzing", "Frank Schätzing"),
                 ({"name": "Frank Schätzing"}, "Frank Schätzing"),
                 ([{"name": "A"}, "B"], "A, B"))
        for author, expected in cases:
            with self.subTest(author=author):
                record = normalize_inventory_record(
                    {"deliverableId": "d1", "title": "T",
                     "authors": author})
                self.assertEqual(expected, record["authors"])


class ReadingStateTests(unittest.TestCase):
    """Lesestatus und Fortschritt: nur bekannte, bestaetigte Werte zaehlen."""

    def rows(self, read_ids=None, progress_by_id=None, state=None):
        return compare_inventory(
            {1: book()}, state or {UUID: {"tolino_id": "d1"}}, [cloud()],
            ["EPUB"], read_ids=read_ids, progress_by_id=progress_by_id)

    def test_known_values_become_confirmed_updates(self):
        rows = self.rows(read_ids={"d1"},
                         progress_by_id={"d1": {"percent": 42, "current": "3",
                                                 "last": "10"}})
        self.assertEqual([(1, True)], comparison_read_updates(rows))
        self.assertEqual([(1, 42)], comparison_progress_updates(rows))
        self.assertEqual("42 % · 3/10", rows[0]["progress"].text)

    def test_unknown_values_are_no_change_at_all(self):
        rows = self.rows()
        self.assertFalse(rows[0]["read"].changed)
        self.assertFalse(rows[0]["progress"].known)
        self.assertEqual("unbekannt", rows[0]["progress"].text)
        self.assertEqual(([], []),
                         (comparison_read_updates(rows),
                          comparison_progress_updates(rows)))

    def test_a_known_empty_bookmark_is_not_zero_progress(self):
        rows = self.rows(read_ids=set(), progress_by_id={})
        self.assertTrue(rows[0]["progress"].known)
        self.assertIsNone(rows[0]["progress"].percent)
        self.assertEqual("", rows[0]["progress"].text)

    def test_a_failed_fetch_keeps_the_stored_progress(self):
        state = {UUID: {"tolino_id": "d1",
                        "reading": {"progress": {"percent": 42}}}}
        store_reading_state(state, self.rows(state=state))
        self.assertEqual({"percent": 42},
                         state[UUID]["reading"]["progress"])

    def test_a_successful_fetch_without_bookmark_clears_the_progress(self):
        state = {UUID: {"tolino_id": "d1",
                        "reading": {"progress": {"percent": 42}}}}
        store_reading_state(state, self.rows(read_ids={"d1"},
                                             progress_by_id={}, state=state))
        self.assertIsNone(state[UUID]["reading"]["progress"]["percent"])

    def test_confirmed_reading_state_is_stored_under_the_book_uuid(self):
        state = {UUID: {"tolino_id": "d1"},
                 "calibre-1-abc": {"tolino_id": "alt"}}
        rows = self.rows(read_ids={"d1"},
                         progress_by_id={"d1": {"percent": 10}}, state=state)
        store_reading_state(state, rows)
        self.assertTrue(state[UUID]["reading"]["read"])
        self.assertEqual({"percent": 10}, state[UUID]["reading"]["progress"])
        self.assertEqual({"tolino_id": "alt"}, state["calibre-1-abc"])
