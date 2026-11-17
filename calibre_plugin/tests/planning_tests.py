"""Planung und Bestandsabgleich an ihren oeffentlichen Funktionen.

Diese Tests bilden die Entscheidungen ab, die der Nutzer im Vergleichsfenster
sieht: was hochgeladen, was gepatcht, was als Prueffall stehen bleibt und was
als identisch gilt.
"""
import unittest

from ..comparison import compare_inventory, content_fingerprint
from ..inventory import normalize_inventory_record
from ..library import normalize_formats
from ..matching import (authors_match, is_valid_isbn, isbn_from_identifier,
                        normalize_isbn)
from ..planning import (metadata_update_fields, plan_collection_updates,
                        plan_metadata_updates, plan_sync, remember_collection,
                        series_map_from_metadata, syncable_metadata)

UUID = "11111111-1111-4111-8111-111111111111"


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
        metadata = {1: book(), 2: book(title="Nur PDF", formats=["PDF"])}
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

    def test_ignored_books_never_reach_the_planner(self):
        metadata = {1: book(ignored=True)}
        self.assertEqual({}, syncable_metadata(metadata))
        self.assertEqual([], plan_sync(syncable_metadata(metadata), [],
                                       ["EPUB"], upload_book_ids={1}))


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


if __name__ == "__main__":
    unittest.main()
