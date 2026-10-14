import base64
import importlib
import unittest
import ast
import json
import os
import tempfile
import time
import zipfile
import re
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from . import config
from .sync import (compare_inventory, cover_bytes, fingerprint, iter_book_ids,
                   normalize_formats, plan_collection_updates, plan_sync,
                   plan_metadata_updates, recorded_collections,
                   remember_collection, revert_unsynced_fingerprints,
                   series_map_from_metadata,
                   metadata_fingerprint, content_fingerprint,
                   metadata_update_fields, safe_format_path,
                   selected_book_ids, selected_table_rows, sync_summary,
                   unpack_plan_result, unpack_upload_record,
                   unpack_meta_update_record, redact_sensitive,
                   format_diagnostic_report, diagnose_preparation,
                   _diagnostic_formats, metadata_by_id,
                   normalize_title, normalize_inventory_item, custom_column_available,
                   metadata_tolino_id, update_tolino_ids, TOLINO_COLUMN)
from .tolino import (PARTNERS, TolinoAuthError,
                     callback_redirect_uri,
                     TolinoClient, hardware_id,
                     force_legacy_partner_id, normalize_hardware_id,
                     normalize_refresh_token, resolve_partner_id,
                     sanitize_error,
                     validate_callback, browser_login)
import calibre_plugin.tolino as tolino_module


# The login helpers must not open a REAL browser window during tests.
# On machines that have Chrome/Chromium installed (CI runners do),
# pick_chromium() would succeed and launch_reader_window() would spawn a
# browser the tests never asked for -- every login test that asserts the
# no-Chromium fallback would then fail or hang. Tests that need a
# browser patch calibre_plugin.cdp.pick_chromium themselves.
# close_grabber_window() is stubbed as well (0.9.30): the real one
# talks to a DevTools endpoint on 127.0.0.1:9223 and must never close
# a developer's open grabber window while the suite runs. Tests that
# care about the close patch the name again with their own mock.
_PICK_CHROMIUM_PATCH = None
_CLOSE_WINDOW_PATCH = None


def setUpModule():
    global _PICK_CHROMIUM_PATCH, _CLOSE_WINDOW_PATCH
    _PICK_CHROMIUM_PATCH = patch("calibre_plugin.cdp.pick_chromium",
                                 return_value=None)
    _PICK_CHROMIUM_PATCH.start()
    _CLOSE_WINDOW_PATCH = patch("calibre_plugin.cdp.close_grabber_window",
                                return_value=False)
    _CLOSE_WINDOW_PATCH.start()


def tearDownModule():
    global _PICK_CHROMIUM_PATCH, _CLOSE_WINDOW_PATCH
    if _PICK_CHROMIUM_PATCH is not None:
        _PICK_CHROMIUM_PATCH.stop()
        _PICK_CHROMIUM_PATCH = None
    if _CLOSE_WINDOW_PATCH is not None:
        _CLOSE_WINDOW_PATCH.stop()
        _CLOSE_WINDOW_PATCH = None


def _full(exc):
    """Nutzertext plus Log-Detail eines TolinoError."""
    return "%s %s" % (exc, getattr(exc, "detail", ""))


class SyncPlanTests(unittest.TestCase):
    def setUp(self):
        self.book = {"uuid": "u1", "title": "A", "formats": ["EPUB"], "last_modified": "1"}

    def test_new_book_is_uploaded(self):
        uploads, removals, state = plan_sync({1: self.book}, {}, ["EPUB"])
        self.assertEqual([(1, "u1", "EPUB", None)], uploads)
        self.assertFalse(removals)
        self.assertIn("u1", state)

    def test_iter_book_ids_uses_rows_and_deduplicates(self):
        class Row:
            def __init__(self, book_id):
                self.book_id = book_id

        class Data:
            def iterall(self):
                return iter((Row(4), Row(2), Row(4), Row(9)))

        class Database:
            data = Data()

        self.assertEqual([4, 2, 9], list(iter_book_ids(Database())))

    def test_iter_book_ids_prefers_all_book_ids_over_legacy_all_ids(self):
        class Modern:
            data = object()

            def all_book_ids(self):
                return (18, 5, 18)

            def all_ids(self):
                raise AssertionError("must prefer all_book_ids")

        self.assertEqual([18, 5], list(iter_book_ids(Modern())))

        class Legacy:
            data = object()

            def all_ids(self):
                return (8, 3, 8)

        self.assertEqual([8, 3], list(iter_book_ids(Legacy())))

    def test_iter_book_ids_uses_data_id_fallback_before_all_ids(self):
        class Data:
            def iterallids(self):
                return iter((7, 1, 7))

        class Database:
            data = Data()

            def all_ids(self):
                raise AssertionError("must prefer data.iterallids")

        self.assertEqual([7, 1], list(iter_book_ids(Database())))

    def test_iter_book_ids_keeps_only_real_book_ids(self):
        """Weder fremde Objekte noch kurze Tuple-Zeilen zählen als Buch-ID."""
        class Odd:
            def iterall(self):
                return iter((object(), {"title": "not a book id"}))

        class OddDatabase:
            data = Odd()

            def all_ids(self):
                raise AssertionError("must not use fallback after iterall")

        self.assertEqual([], list(iter_book_ids(OddDatabase())))

        class Tuples:
            def iterall(self):
                return iter(((12, "title"), (), (13,),
                             ("not-an-id", "title")))

        class TupleDatabase:
            data = Tuples()

        self.assertEqual([12, 13], list(iter_book_ids(TupleDatabase())))

    def test_metadata_by_id_uses_real_id_not_filtered_view_index(self):
        class Data:
            def iterall(self):
                return iter(((41, "first"), (99, "second")))

        class Database:
            data = Data()

            def has_id(self, book_id):
                return book_id in {41, 99}

            def get_metadata(self, book_id, index_is_id=False):
                if not index_is_id:
                    raise AssertionError("filtered-view index was passed")
                return {"uuid": "u%d" % book_id}

        database = Database()
        ids = list(iter_book_ids(database))
        self.assertEqual([41, 99], ids)
        self.assertEqual({"uuid": "u41"}, metadata_by_id(database, ids[0]))

    def test_metadata_by_id_rejects_missing_real_id(self):
        class Database:
            def has_id(self, book_id):
                return False

            def get_metadata(self, book_id, index_is_id=False):
                raise AssertionError("must validate before reading")

        with self.assertRaisesRegex(ValueError, "no book with id 7"):
            metadata_by_id(Database(), 7)

    def test_normalize_formats_handles_all_calibre_shapes(self):
        self.assertEqual(["EPUB", "PDF"], normalize_formats(("EPUB", "PDF")))
        self.assertEqual(["EPUB", "PDF"], normalize_formats("EPUB, PDF"))
        class FormatsList:
            def __iter__(self):
                return iter((".epub", "PDF", "epub"))

        self.assertEqual(["EPUB", "PDF"], normalize_formats(FormatsList()))
        self.assertEqual([], normalize_formats(None))
        self.assertEqual([], normalize_formats(("", None)))

    def test_plan_sync_accepts_calibre_formats_list_and_normalizes_case(self):
        class FormatsList:
            def __iter__(self):
                return iter((".epub",))

        metadata = {"uuid": "u1", "title": "A", "formats": FormatsList()}
        self.assertEqual(
            [(1, "u1", "EPUB", None)],
            plan_sync({1: metadata}, {}, (".EPUB",))[0],
        )

    def test_selected_book_without_preferred_format_has_contextual_warning(self):
        with self.assertRaisesRegex(ValueError, r"Buch 1 \(A\).*kein bevorzugtes Format"):
            plan_sync(
                {1: {"uuid": "u1", "title": "A", "formats": ("MOBI",)}},
                {}, ("EPUB",), upload_book_ids={1},
            )

    def test_selected_rows_handles_qmodelindex_and_short_values(self):
        class Index:
            def __init__(self, value):
                self.value = value

            def row(self):
                return self.value

        class Selection:
            def selectedRows(self):
                return (Index(2), Index(-1), object(), Index(0))

        class Table:
            def selectionModel(self):
                return Selection()

        self.assertEqual([2, 0], selected_table_rows(Table()))

    def test_plan_result_helpers_reject_short_records_with_context(self):
        with self.assertRaisesRegex(ValueError, "invalid sync plan"):
            unpack_plan_result(([], {}))
        with self.assertRaisesRegex(ValueError, "invalid upload record"):
            unpack_upload_record((1, "uuid"))
        self.assertEqual(
            {"book_id": 1, "book_uuid": "u1", "format_name": "EPUB", "old_id": None},
            unpack_upload_record((1, "u1", "EPUB", None)),
        )

    def test_complete_preparation_path_handles_empty_single_and_short_data(self):
        cases = (
            ({}, [], []),
            ({1: dict(self.book)}, [], [(1, "u1", "EPUB", None)]),
            ({2: {"uuid": "u2", "formats": ("EPUB",)}}, [], [(2, "u2", "EPUB", None)]),
        )
        for metadata, inventory, expected_uploads in cases:
            comparison = compare_inventory(metadata, {}, inventory, ("EPUB",))
            selected = selected_book_ids(comparison)
            plan = unpack_plan_result(
                plan_sync(metadata, {}, ("EPUB",), upload_book_ids=selected)
            )
            uploads, removals, current = plan
            self.assertEqual(expected_uploads, uploads)
            self.assertEqual([], removals)
            self.assertEqual(set(metadata), {item["calibre_id"] for item in current.values()})
            for record in uploads:
                self.assertEqual(expected_uploads[0][0] if expected_uploads else None,
                                 unpack_upload_record(record)["book_id"])

    def test_safe_format_path_accepts_wrapped_path_and_rejects_empty_result(self):
        class Database:
            def format_abspath(self, book_id, format_name, index_is_id=False):
                self.args = (book_id, format_name, index_is_id)
                return ("/library/book.epub", format_name)

        database = Database()
        self.assertEqual("/library/book.epub", safe_format_path(database, 1, "EPUB"))
        self.assertEqual((1, "EPUB", True), database.args)

        class EmptyDatabase:
            def format_abspath(self, book_id, format_name):
                return ()

        with self.assertRaises(ValueError):
            safe_format_path(EmptyDatabase(), 1, "EPUB")

    def test_safe_format_path_uses_real_id_for_legacy_view_wrapper(self):
        class Calibre76Database:
            def format_abspath(self, book_id, format_name, index_is_id=False):
                if not index_is_id:
                    raise IndexError("tuple index out of range")
                return "/library/book.epub"

        self.assertEqual(
            "/library/book.epub",
            safe_format_path(Calibre76Database(), 42, ".epub"),
        )

    def test_safe_format_path_falls_back_for_old_signature_and_stale_ids(self):
        class OldDatabase:
            def __init__(self):
                self.calls = []

            def format_abspath(self, book_id, format_name):
                self.calls.append((book_id, format_name))
                raise AssertionError("view-index resolver must not be called")

        class NewApi:
            def __init__(self):
                self.calls = []

            def format_abspath(self, book_id, format_name):
                self.calls.append((book_id, format_name))
                return ("/library/book.epub", format_name)

        database = OldDatabase()
        database.new_api = NewApi()
        self.assertEqual("/library/book.epub", safe_format_path(database, 2, "EPUB"))
        self.assertEqual([], database.calls)
        self.assertEqual([(2, "EPUB")], database.new_api.calls)

        with self.assertRaisesRegex(ValueError, "ID-safe resolver"):
            safe_format_path(OldDatabase(), 2, "EPUB")

        class BrokenDatabase:
            def format_abspath(self, book_id, format_name, index_is_id=False):
                raise TypeError("internal resolver failure")

        with self.assertRaisesRegex(TypeError, "internal resolver failure"):
            safe_format_path(BrokenDatabase(), 2, "EPUB")

        class StaleDatabase:
            def has_id(self, book_id):
                return False

            def format_abspath(self, book_id, format_name, index_is_id=False):
                raise AssertionError("stale ids must be rejected before lookup")

        with self.assertRaisesRegex(ValueError, "no book with id"):
            safe_format_path(StaleDatabase(), 99, "EPUB")

        for invalid_id in (None, 0, -1, True, "42"):
            with self.subTest(invalid_id=invalid_id), self.assertRaises(ValueError):
                safe_format_path(OldDatabase(), invalid_id, "EPUB")
        with self.assertRaises(ValueError):
            safe_format_path(OldDatabase(), 2, "../EPUB")

    def test_diagnosis_reports_found_and_missing_format_paths(self):
        with tempfile.NamedTemporaryFile(suffix=".epub") as book_file:
            class FormatsList:
                def __iter__(self):
                    return iter((".EPUB", "pdf"))

            class Database:
                def format_abspath(self, book_id, format_name):
                    raise AssertionError("view-index resolver must not be called")

            class NewApi:
                def format_abspath(self, book_id, format_name):
                    if format_name == "EPUB":
                        return book_file.name
                    return ()

            database = Database()
            database.new_api = NewApi()
            report = _diagnostic_formats(
                database, {1: {"formats": FormatsList()}})
            self.assertEqual(["EPUB", "PDF"], report["1"]["metadata_formats"])
            self.assertEqual("found", report["1"]["paths"]["EPUB"]["status"])
            self.assertEqual("missing", report["1"]["paths"]["PDF"]["status"])

    def test_cover_bytes_handles_bytes_paths_and_unexpected_values(self):
        class Database:
            def __init__(self, value):
                self.value = value

            def has_id(self, book_id):
                return True

            def cover(self, book_id, as_file=False, index_is_id=False):
                return self.value

        self.assertEqual(b"cover", cover_bytes(Database((b"cover", "jpg")), 1))
        self.assertIsNone(cover_bytes(Database(("missing.jpg",)), 1))
        with tempfile.NamedTemporaryFile(delete=False) as cover_file:
            cover_file.write(b"cover-file")
            path = cover_file.name
        try:
            self.assertEqual(b"cover-file", cover_bytes(Database((path, "jpg")), 1))
        finally:
            os.remove(path)

    def test_cover_bytes_uses_real_id_for_legacy_view_wrapper(self):
        class Calibre76Database:
            def cover(self, book_id, as_file=False, index_is_id=False):
                if not index_is_id:
                    raise IndexError("tuple index out of range")
                return b"cover-data"

        self.assertEqual(
            b"cover-data",
            cover_bytes(Calibre76Database(), 42),
        )

    def test_cover_bytes_falls_back_for_old_signature_and_stale_ids(self):
        class OldDatabase:
            def __init__(self):
                self.calls = []

            def cover(self, book_id, as_file=False):
                self.calls.append((book_id, as_file))
                raise AssertionError("view-index resolver must not be called")

        class NewApi:
            def __init__(self):
                self.calls = []

            def cover(self, book_id, as_file=False):
                self.calls.append((book_id, as_file))
                return b"cover-data"

        database = OldDatabase()
        database.new_api = NewApi()
        self.assertEqual(b"cover-data", cover_bytes(database, 2))
        self.assertEqual([], database.calls)
        self.assertEqual([(2, False)], database.new_api.calls)

        with self.assertRaisesRegex(ValueError, "ID-safe resolver"):
            cover_bytes(OldDatabase(), 2)

        class StaleDatabase:
            def has_id(self, book_id):
                return False

            def cover(self, book_id, as_file=False, index_is_id=False):
                raise AssertionError("stale ids must be rejected before lookup")

        with self.assertRaisesRegex(ValueError, "no book with id"):
            cover_bytes(StaleDatabase(), 99)

        for invalid_id in (None, 0, -1, True, "42"):
            with self.subTest(invalid_id=invalid_id), self.assertRaises(ValueError):
                cover_bytes(OldDatabase(), invalid_id)

    def test_inventory_comparison_matches_uuid_and_marks_changed(self):
        metadata = {
            1: {"uuid": "u1", "title": "A", "authors": "Author",
                "isbn": "123", "formats": ["EPUB"], "last_modified": "2"},
            2: {"uuid": "u2", "title": "B", "authors": "Writer",
                "isbn": "456", "formats": ["EPUB"], "last_modified": "1"},
        }
        state = {
            "u1": {"tolino_id": "t1", "fingerprint": fingerprint(metadata[1], "EPUB")},
            "u2": {"tolino_id": "t2", "fingerprint": "old"},
        }
        rows = compare_inventory(
            metadata, state,
            [{"deliverableId": "t2", "uuid": "u2", "title": "B", "authors": "Writer"},
             {"deliverableId": "t1", "uuid": "u1", "title": "A", "authors": "Author"}],
            ["EPUB"],
        )
        self.assertEqual(["identical", "changed"], [row["status"] for row in rows])
        self.assertEqual(set(), selected_book_ids(rows))

    def test_custom_column_value_is_a_matching_fallback(self):
        metadata = {1: {"uuid": "u1", "title": "Changed title",
                        "formats": ["EPUB"], "#tolino_id": "bosh_8_saved"}}
        rows = compare_inventory(
            metadata, {}, [{"deliverableId": "bosh_8_saved", "title": "Remote title"}],
            ["EPUB"],
        )
        self.assertEqual("changed", rows[0]["status"])
        self.assertEqual("bosh_8_saved", rows[0]["tolino_id"])
        self.assertEqual("stored_id_or_uuid", rows[0]["match_reason"])

    def test_custom_column_writes_only_when_available_and_enabled(self):
        """Geschrieben wird ueber set_field() -- nur bei vorhandener und
        eingeschalteter Spalte; ohne Spalte gibt es keinen SQL-Ersatzweg."""
        class Database:
            field_metadata = {TOLINO_COLUMN: {"datatype": "text"}}

            def set_field(self, field, values):
                self.field, self.values = field, values

        database = Database()
        self.assertTrue(custom_column_available(database))
        self.assertEqual(1, update_tolino_ids(database, [(7, "bosh_8_new")]))
        self.assertEqual(TOLINO_COLUMN, database.field)
        self.assertEqual({7: "bosh_8_new"}, database.values)

        class Disabled(Database):
            def set_field(self, field, values):
                raise AssertionError("disabled column must not be written")

        self.assertEqual(0, update_tolino_ids(
            Disabled(), [(7, "bosh_8_new")], enabled=False))

        class Missing:
            field_metadata = {}

        self.assertFalse(custom_column_available(Missing()))
        self.assertEqual(0, update_tolino_ids(Missing(), [(7, "bosh_8_new")]))

    def test_metadata_tolino_id_accepts_calibre_style_mapping(self):
        self.assertEqual("bosh_8_id", metadata_tolino_id({"#tolino_id": " bosh_8_id "}))

    def test_inventory_comparison_uses_isbn_and_reports_remote_only(self):
        metadata = {
            1: {"uuid": "u1", "title": "Local", "authors": "Author",
                "isbn": "978-1-2", "formats": ["EPUB"]},
        }
        rows = compare_inventory(
            metadata, {},
            [{"id": "t1", "title": "Remote title", "authors": "Other",
              "isbn": "97812"}, {"id": "t2", "title": "Cloud only"}],
            ["EPUB"],
        )
        self.assertEqual("changed", rows[0]["status"])
        self.assertEqual("t1", rows[0]["tolino_id"])
        self.assertEqual("only_tolino", rows[1]["status"])
        self.assertFalse(rows[1]["selected"])

    def test_diagnostic_inventory_shape_uses_nested_metadata_and_keeps_id_technical(self):
        item = {
            "deliverableId": "bosh_8_543897186788677497438396920",
            "epubMetaData": {
                "title": "Der echte Tolino-Titel",
                "author": [{"name": "Eine Autorin"}],
                "isbn": "978-3-1234-5678-9",
            },
            "publicationId": "pub-1",
            "resellerId": "8",
        }
        self.assertEqual({
            "title": "Der echte Tolino-Titel",
            "authors": "Eine Autorin",
            "isbn": "978-3-1234-5678-9",
            "tolino_id": "bosh_8_543897186788677497438396920",
        }, normalize_inventory_item(item))
        rows = compare_inventory(
            {1: {"uuid": "u1", "title": "Der echte Tolino-Titel",
                 "authors": "Eine Autorin", "isbn": "9783123456789",
                 "formats": ["EPUB"]}},
            {}, {"edata": [item]}, ["EPUB"],
        )
        self.assertEqual("identical", rows[0]["status"])
        self.assertEqual("Der echte Tolino-Titel", rows[0]["title"])
        self.assertEqual("bosh_8_543897186788677497438396920", rows[0]["tolino_id"])
        self.assertFalse(rows[0]["selected"])

    def test_inventory_without_title_is_not_matchable_and_does_not_show_id_as_title(self):
        rows = compare_inventory(
            {1: {"uuid": "u1", "title": "Local title", "formats": ["EPUB"]}},
            {}, [{"deliverableId": "bosh_8_543897186788677497438396920",
                  "epubMetaData": {"author": [{"name": "Author"}]}}],
            ["EPUB"],
        )
        self.assertEqual("new_in_calibre", rows[0]["status"])
        remote = rows[1]
        self.assertEqual("not_matchable", remote["status"])
        self.assertEqual("", remote["title"])
        self.assertEqual("bosh_8_543897186788677497438396920", remote["tolino_id"])
        self.assertIn("ohne Titel", remote["explanation"])

    def test_inventory_normalization_accepts_ebook_and_deliverable_shapes(self):
        rows = compare_inventory(
            {1: {"uuid": "u1", "title": "Nested title", "formats": ["EPUB"]}},
            {}, {"ebook": [{"deliverable": {
                "deliverableId": "bosh_8_2",
                "title": "Nested title",
                "author": {"name": "Nested author"},
                "isbn13": "978-1-2",
            }}]}, ["EPUB"],
        )
        self.assertEqual("identical", rows[0]["status"])
        self.assertEqual("Nested author", normalize_inventory_item({
            "deliverable": {"author": {"name": "Nested author"},
                            "isbn13": "978-1-2"}
        })["authors"])
        self.assertEqual("978-1-2", normalize_inventory_item({
            "deliverable": {"author": {"name": "Nested author"},
                            "isbn13": "978-1-2"}
        })["isbn"])

    def test_title_matching_normalizes_unicode_punctuation_and_extension(self):
        self.assertEqual(normalize_title("Der «Überblick» – Band 2.epub"),
                         normalize_title("DER Überblick - Band 2"))
        metadata = {1: {"uuid": "u1", "title": "Der «Überblick» – Band 2",
                        "authors": "Andere", "formats": ["EPUB"]}}
        rows = compare_inventory(
            metadata, {}, [{"id": "t1", "title": "DER Überblick - Band 2.pdf"}],
            ["EPUB"],
        )
        self.assertEqual("identical", rows[0]["status"])
        self.assertFalse(rows[0]["selected"])
        self.assertEqual("title", rows[0]["match_reason"])

    def test_title_match_is_used_without_author_and_duplicate_is_conservative(self):
        metadata = {
            1: {"uuid": "u1", "title": "The Book", "formats": ["EPUB"]},
            2: {"uuid": "u2", "title": "The Book", "formats": ["EPUB"]},
        }
        state = {}
        rows = compare_inventory(
            metadata, state,
            [{"id": "t1", "title": "the book"}, {"id": "t2", "title": "THE BOOK"}],
            ["EPUB"],
        )
        self.assertEqual(["identical", "identical"], [row["status"] for row in rows])
        self.assertEqual({"duplicate_title"}, {row["match_reason"] for row in rows})
        self.assertFalse(any(row["selected"] for row in rows))
        self.assertEqual({"t1", "t2"}, {state[key]["tolino_id"] for key in state})

    def test_upload_gate_follows_selection_and_fingerprint(self):
        """Geaendert heisst hochladen, unveraendert nur auf Wunsch."""
        changed = {"u1": {"tolino_id": "t1", "fingerprint": "old"}}
        # Ohne Auswahl geht nichts hoch.
        self.assertEqual([], plan_sync({1: self.book}, changed, ["EPUB"],
                                       upload_book_ids=set())[0])
        # Eine echte Aenderung laeuft ohne Auswahl mit.
        self.assertEqual("t1",
                         plan_sync({1: self.book}, changed, ["EPUB"])[0][0][3])
        same = {"u1": {"tolino_id": "t1",
                       "fingerprint": fingerprint(self.book, "EPUB")}}
        self.assertEqual([], plan_sync({1: self.book}, same, ["EPUB"])[0])
        # Ausdruecklich ausgewaehlt: derselbe Stand geht erneut hoch.
        self.assertEqual(
            [(1, "u1", "EPUB", "t1")],
            plan_sync({1: self.book}, same, ["EPUB"], upload_book_ids={1})[0],
        )

    def test_deletions_are_opt_in(self):
        state = {"gone": {"tolino_id": "t2", "fingerprint": "x"}}
        self.assertEqual([], plan_sync({}, state, ["EPUB"])[1])
        self.assertEqual([("gone", "t2")], plan_sync({}, state, ["EPUB"], True)[1])

    def test_reference_partners_and_stable_hardware_shape(self):
        for partner_id in (1, 2, 3, 4, 5, 6, 7):
            self.assertIn(partner_id, PARTNERS)
        # Hardware IDs use the web reader's 8-4-4-4-12 UUID format.
        self.assertRegex(hardware_id(), r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

    def test_reseller_id_matches_protocol_partner(self):
        # The protocol reseller IDs stay stable while plugin IDs are
        # consecutive; Orell Fuessli keeps reseller 8 (new plugin ID 4).
        self.assertEqual("8", PARTNERS[4]["reseller_id"])
        self.assertEqual("3", PARTNERS[1]["reseller_id"])
        self.assertEqual("13", PARTNERS[5]["reseller_id"])

    def test_legacy_partner_ids_are_resolved(self):
        for legacy, expected in ((3, 1), (4, 2), (6, 3), (8, 4),
                                 (13, 5), (23, 6), (30, 7)):
            self.assertEqual(expected, force_legacy_partner_id(legacy))
        # Runtime resolution is safe on overlapping IDs: current IDs win.
        self.assertEqual(4, resolve_partner_id(4))
        self.assertEqual(1, resolve_partner_id(1))

    def test_compact_hardware_id_is_normalized_to_uuid(self):
        self.assertEqual(
            "aff2e436-beb8-53e8-23b8-e02163f55220",
            normalize_hardware_id("aff2e436beb853e823b8e02163f55220"))
        self.assertEqual(
            "dc37788f-8ff3-4e8e-b0e3-5059c3c08ce1",
            normalize_hardware_id("dc37788f-8ff3-4e8e-b0e3-5059c3c08ce1"))
        self.assertEqual("", normalize_hardware_id(""))
        self.assertEqual("custom-device", normalize_hardware_id("custom-device"))

    def test_partner_8_refresh_configuration_matches_reference(self):
        self.assertEqual("webreader", PARTNERS[4]["client_id"])
        self.assertEqual("SCOPE_BOSH", PARTNERS[4]["scope"])
        self.assertEqual("https://www.orellfuessli.ch/auth/oauth2/token",
                         PARTNERS[4]["token_url"])
        self.assertEqual(
            "https://www.orellfuessli.ch/auth/oauth2/autologin",
            PARTNERS[4]["auth_url"])
        self.assertEqual("https://webreader.mytolino.com/library/",
                         PARTNERS[4]["reader_url"])
        self.assertEqual("37", PARTNERS[4]["x_buchde.mandant_id"])
        self.assertEqual("17", PARTNERS[4]["x_buchde.skin_id"])
        self.assertEqual("TOLINO_WEBREADER", PARTNERS[4]["client_type"])
        self.assertEqual("5.2.0", PARTNERS[4]["client_version"])
        self.assertEqual("https://webreader.mytolino.com",
                         PARTNERS[4]["token_headers"]["Origin"])
        self.assertEqual("https://webreader.mytolino.com/",
                         PARTNERS[4]["token_headers"]["Referer"])

    def test_refresh_token_normalization_reports_only_safe_metadata(self):
        token, info = normalize_refresh_token('  "refresh-secret-value"  ')
        self.assertEqual("refresh-secret-value", token)
        self.assertEqual(20, info["token_length"])
        self.assertEqual("refr...", info["token_prefix"])
        self.assertTrue(info["outer_quotes_removed"])
        self.assertTrue(info["surrounding_whitespace_removed"])

    def test_partner_8_authenticated_headers_match_web_reader(self):
        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"{}"

        def request(request, timeout):
            captured["headers"] = request.headers
            return Response()

        with patch("calibre_plugin.tolino._curl_binary", return_value=None), \
                patch("calibre_plugin.tolino._impersonate_session", return_value=None), \
                        patch("calibre_plugin.tolino.urlopen", request):
            client = TolinoClient(4, "3xxA-00BCD-EFGHI-JKLMN-OPQRh",
                                  "refresh-token")
            client.access = "access-token"
            client.expires_at = time.time() + 60
            client._request("https://bosh.pageplace.de/bosh/rest/inventory/delta")
        headers = {key.casefold(): value for key, value in captured["headers"].items()}
        self.assertEqual("8", headers["reseller_id"])
        self.assertEqual("TOLINO_WEBREADER", headers["client_type"])
        self.assertEqual("5.2.0", headers["client_version"])

    def test_partner_8_refresh_payload_is_url_encoded_and_not_access_token(self):
        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"access_token":"access-secret","expires_in":3600}'

        def request(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = request.data.decode("utf-8")
            captured["content_type"] = request.headers["Content-type"]
            captured["headers"] = request.headers
            return Response()

        with patch("calibre_plugin.tolino._curl_binary", return_value=None), \
                patch("calibre_plugin.tolino._impersonate_session", return_value=None), \
                        patch("calibre_plugin.tolino.urlopen", request):
            client = TolinoClient(4, "3xxA-00BCD-EFGHI-JKLMN-OPQRh",
                                  " refresh-token ")
            client.login()
        self.assertEqual(PARTNERS[4]["token_url"], captured["url"])
        self.assertEqual(
            "client_id=webreader&grant_type=refresh_token&"
            "refresh_token=refresh-token&scope=SCOPE_BOSH",
            captured["body"])
        self.assertEqual("application/x-www-form-urlencoded",
                         captured["content_type"])
        headers = {key.casefold(): value for key, value in captured["headers"].items()}
        self.assertEqual("https://webreader.mytolino.com", headers["origin"])
        self.assertEqual("https://webreader.mytolino.com/", headers["referer"])
        self.assertIn("scope=SCOPE_BOSH", captured["body"])
        self.assertNotIn("client_type", headers)
        self.assertNotIn("client_version", headers)
        self.assertNotIn("reseller_id", headers)
        self.assertNotIn("cookie", headers)
        self.assertNotIn("authorization", headers)
        self.assertEqual("refresh-token", client.refresh)
        self.assertNotEqual("access-secret", client.refresh)

    def test_rotated_refresh_token_is_returned_and_persisted_before_followup_work(self):
        captured = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return (b'{"access_token":"access-secret",'
                        b'"refresh_token":"rotated-refresh","expires_in":3600}')

        with patch("calibre_plugin.tolino._curl_binary", return_value=None), \
                patch("calibre_plugin.tolino._impersonate_session", return_value=None), \
                        patch("calibre_plugin.tolino.urlopen", return_value=Response()):
            client = TolinoClient(4, "hardware", "old-refresh",
                                  token_callback=captured.append)
            self.assertEqual("rotated-refresh", client.login())
            self.assertEqual(["rotated-refresh"], captured)
            self.assertEqual("rotated-refresh", client.refresh)
            # A second call uses the cached access token and cannot reuse the old grant.
            self.assertEqual("rotated-refresh", client.login())

    def test_reuse_exceeded_is_not_retried_and_gives_user_action(self):
        from urllib.error import HTTPError

        body = b'{"error":"invalid_grant","error_description":"Maximum allowed refresh token reuse exceeded"}'
        error = HTTPError("https://example.invalid/token", 400, "Bad Request", {}, None)
        error.read = lambda: body
        with patch("calibre_plugin.tolino._curl_binary", return_value=None), \
                patch("calibre_plugin.tolino._impersonate_session", return_value=None), \
                        patch("calibre_plugin.tolino.urlopen", side_effect=error) as request:
            with self.assertRaisesRegex(TolinoAuthError, "Zugang abgelehnt"):
                TolinoClient(4, "", "old-refresh").login()
        self.assertEqual(1, request.call_count)

    def test_403_with_invalid_grant_body_gives_user_action(self):
        from urllib.error import HTTPError

        body = b'{"error":"invalid_grant","error_description":"token reused"}'
        error = HTTPError("https://example.invalid/token", 403, "Forbidden", {}, None)
        error.read = lambda: body
        with patch("calibre_plugin.tolino._curl_binary", return_value=None), \
                patch("calibre_plugin.tolino._impersonate_session", return_value=None), \
                        patch("calibre_plugin.tolino.urlopen", side_effect=error):
            with self.assertRaisesRegex(TolinoAuthError, "Zugang abgelehnt"):
                TolinoClient(4, "", "old-refresh").login()

    def test_403_bot_protection_reports_response_detail(self):
        from urllib.error import HTTPError

        body = b"Access denied | bot protection"
        error = HTTPError("https://example.invalid/token", 403, "Forbidden", {}, None)
        error.read = lambda: body
        client = TolinoClient(4, "", "old-refresh")
        with patch("calibre_plugin.tolino._curl_binary", return_value=None), \
                patch("calibre_plugin.tolino._impersonate_session", return_value=None), \
                        patch("calibre_plugin.tolino.urlopen", side_effect=error), \
                        patch("calibre_plugin.tolino.time.sleep"):
            with self.assertRaisesRegex(TolinoAuthError, "Access denied"):
                client.login()
        self.assertEqual(403, client.last_http_status)
        self.assertIn("Access denied", client.last_error_text)
        self.assertEqual("urllib", client.last_transport)

    def test_waf_403_on_token_endpoint_waits_and_retries(self):
        """Feldbefund 0.9.38: der erste Token-Aufruf nach der Browser-
        Anmeldung traf 403 (Bot-Schutz-Laufzeitbahn), jeder spaetere
        Klick lief – jetzt wird kurz gewartet und zwei Mal wiederholt."""
        from urllib.error import HTTPError

        bot = HTTPError("https://example.invalid/token", 403, "Forbidden",
                        {}, None)
        bot.read = lambda: (b"<!doctype html><title>Zugriff geblockt"
                            b"</title>")
        calls = []
        sleeps = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return (b'{"access_token":"access-secret",'
                        b'"refresh_token":"rotated-refresh","expires_in":3600}')

        def fake_urlopen(request, timeout=None):
            calls.append(request.full_url)
            if len(calls) < 3:
                raise bot
            return Response()

        with patch("calibre_plugin.tolino._curl_binary", return_value=None), \
                patch("calibre_plugin.tolino._impersonate_session",
                      return_value=None), \
                patch("calibre_plugin.tolino.urlopen",
                      side_effect=fake_urlopen), \
                patch("calibre_plugin.tolino.time.sleep",
                      side_effect=sleeps.append):
            client = TolinoClient(4, "hardware", "old-refresh")
            self.assertEqual("rotated-refresh", client.login())
        self.assertEqual(3, len(calls))
        self.assertEqual([2.0, 5.0], sleeps)
        self.assertEqual("urllib", client.last_transport)
        self.assertEqual("rotated-refresh", client.refresh)

    def test_other_partner_refresh_payload_keeps_configured_scope(self):
        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"access_token":"access-secret","expires_in":3600}'

        def request(request, timeout):
            captured["body"] = request.data.decode("utf-8")
            return Response()

        with patch("calibre_plugin.tolino._curl_binary", return_value=None), \
                patch("calibre_plugin.tolino._impersonate_session", return_value=None), \
                        patch("calibre_plugin.tolino.urlopen", request):
            TolinoClient(1, "3xxA-00BCD-EFGHI-JKLMN-OPQRh",
                         "refresh-token").login()
        self.assertEqual(
            "client_id=webreader&grant_type=refresh_token&"
            "refresh_token=refresh-token&scope=SCOPE_BOSH",
            captured["body"])

    def test_auth_diagnostics_redacts_token_content(self):
        client = TolinoClient(4, "", '"secret-refresh"')
        diagnostics = client.auth_diagnostics()
        rendered = format_diagnostic_report([{
            "step": "auth", "status": "error", "value": diagnostics,
        }])
        self.assertIn("partner_id", rendered)
        self.assertIn("Books.ch / orellfuessli.ch", rendered)
        self.assertIn("token_length", rendered)
        self.assertIn("token_prefix", rendered)
        self.assertNotIn("secret-refresh", rendered)
        self.assertNotIn("authorization", rendered.casefold())

    def test_callback_requires_matching_state_and_fresh_timestamp(self):
        query = {"state": ["expected"], "code": ["opaque-code"]}
        self.assertEqual("opaque-code", validate_callback(query, "expected", 100, 120))
        with self.assertRaises(TolinoAuthError):
            validate_callback(query, "wrong", 100, 120)
        with self.assertRaises(TolinoAuthError):
            validate_callback(query, "expected", 100, 401)

    def test_callback_rejects_provider_errors_and_missing_code(self):
        with self.assertRaises(TolinoAuthError):
            validate_callback({"state": ["s"], "error": ["denied"]}, "s", 100, 101)
        with self.assertRaises(TolinoAuthError):
            validate_callback({"state": ["s"]}, "s", 100, 101)
        with self.assertRaises(TolinoAuthError):
            validate_callback({"state": [], "code": ["c"]}, "s", 100, 101)

    def test_redirect_uri_is_loopback_only(self):
        self.assertEqual("http://127.0.0.1:4321/callback", callback_redirect_uri(4321))

    def test_sync_summary_is_deterministic_for_dashboard(self):
        self.assertEqual({"uploads": 2, "deletions": 1, "errors": 0, "total": 3},
                         sync_summary(2, 1))

    def test_diagnostic_redaction_removes_credentials_and_headers(self):
        value = redact_sensitive({
            "refresh_token": "refresh-secret",
            "password": "password-secret",
            "headers": "Authorization: Bearer access-secret",
            "nested": {"access_token": "access-secret"},
        })
        rendered = format_diagnostic_report([{"step": "response", "status": "ok",
                                              "value": value}])
        self.assertNotIn("refresh-secret", rendered)
        self.assertNotIn("password-secret", rendered)
        self.assertNotIn("access-secret", rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_sanitize_error_redacts_configured_variants_jwt_and_traceback(self):
        refresh = '  "refresh-secret-value"  '
        access = "access-secret-value"
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature"
        traceback_text = (
            'Tolino HTTP 400: {"error":"invalid_grant",'
            '"error_description":"refresh_token=%s access_token=%s"}\n'
            "Authorization: Bearer %s\n"
            "Traceback: leaked refresh-secret-value"
        ) % (refresh.strip()[1:-1], access, jwt)
        rendered = sanitize_error(traceback_text, (refresh, access))
        self.assertIn("invalid_grant", rendered)
        self.assertNotIn("refresh-secret-value", rendered)
        self.assertNotIn(access, rendered)
        self.assertNotIn(jwt, rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_http_error_keeps_safe_fields_without_echoing_tokens(self):
        from urllib.error import HTTPError

        token = "refresh-secret-value"
        body = ('{"error":"invalid_grant","error_description":"bad %s",'
                '"refresh_token":"%s"}' % (token, token)).encode("utf-8")
        error = HTTPError("https://example.invalid/token", 400, "Bad Request",
                          {}, None)
        error.read = lambda: body
        client = TolinoClient(4, "", token)
        with patch("calibre_plugin.tolino._curl_binary", return_value=None), \
                patch("calibre_plugin.tolino._impersonate_session", return_value=None), \
                        patch("calibre_plugin.tolino.urlopen", side_effect=error):
            with self.assertRaisesRegex(TolinoAuthError, "Zugang abgelehnt"):
                client.login()
        self.assertIn("invalid_grant", client.last_error_text)
        self.assertIn("error_description", client.last_error_text)
        self.assertNotIn(token, client.last_error_text)

    def test_diagnosis_reports_steps_and_continues_after_metadata_error(self):
        class Row:
            def __init__(self, book_id):
                self.book_id = book_id

        class Data:
            def iterall(self):
                return (Row(1), Row(2))

        class Database:
            data = Data()

            def get_metadata(self, book_id, index_is_id=False):
                if book_id == 2:
                    raise IndexError("tuple index out of range")
                return {"uuid": "u1", "formats": ("EPUB",)}

            def format_abspath(self, book_id, format_name):
                return "/library/book.epub"

            def cover(self, book_id, as_file=False):
                return b"cover"

        results = diagnose_preparation(Database(), ["EPUB"], upload_covers=True)
        report = format_diagnostic_report(results)
        self.assertIn("Calibre-Version", report)
        self.assertIn("Erkannte Buch-ID-Anzahl", report)
        self.assertIn("IndexError", report)
        self.assertIn("tuple index out of range", report)
        self.assertIn("plan_sync-Ergebnisform", report)

    def test_first_install_prefers_epub_only(self):
        """Erstinstallation: nur EPUB als bevorzugtes Format (0.15.2)."""
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            self.assertEqual(["EPUB"], config.settings()["preferred_formats"])
        finally:
            config.PREFERENCES = original

    def test_empty_settings_are_migrated_without_calibre(self):
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            values = config.settings()
            self.assertEqual(config.DEFAULTS["partner_id"], values["partner_id"])
            self.assertEqual(config.DEFAULTS["preferred_formats"], values["preferred_formats"])
            self.assertEqual(config.DEFAULTS["state"], values["state"])
            self.assertTrue(values["use_tolino_column"])
            self.assertEqual(set(config.DEFAULTS), set(config.PREFERENCES))
        finally:
            config.PREFERENCES = original

    def test_series_collections_default_off_and_are_persisted(self):
        """Serien-Sammlungen sind standardmässig AUS (0.15.2) und schaltbar
        (0.12.0). Metadaten-Updates sind seit 0.13.0 eine Auswahl pro Buch
        und haben deshalb keine globale Option mehr."""
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            values = config.settings()
            self.assertFalse(values["sync_collections"])
            self.assertNotIn("sync_metadata_updates", values)
            self.assertEqual(set(config.DEFAULTS), set(config.PREFERENCES))
            config.save_settings({"sync_collections": True})
            self.assertTrue(config.settings()["sync_collections"])
            self.assertEqual(set(config.DEFAULTS), set(config.PREFERENCES))
        finally:
            config.PREFERENCES = original

    def test_existing_install_gets_the_new_defaults_once(self):
        """Auch eine bestehende Installation landet bei EPUB-only und
        «Serien aus» – aber nur einmal. Danach bleibt eine bewusste Wahl
        stehen, selbst wenn sie dem alten Standard entspricht (0.15.2)."""
        original = config.PREFERENCES
        try:
            stale = config.JSONConfig("test")
            stale.update({"preferred_formats": ["EPUB", "PDF"],
                          "sync_collections": True})
            config.PREFERENCES = stale
            values = config.settings()
            self.assertEqual(["EPUB"], values["preferred_formats"])
            self.assertFalse(values["sync_collections"])
            self.assertEqual(["EPUB"], stale["preferred_formats"])
            self.assertFalse(stale["sync_collections"])
            config.save_settings({"sync_collections": True})
            self.assertTrue(config.settings()["sync_collections"])
        finally:
            config.PREFERENCES = original

    def test_a_deliberate_format_choice_survives_the_migration(self):
        original = config.PREFERENCES
        try:
            stale = config.JSONConfig("test")
            stale.update({"preferred_formats": ["MOBI", "EPUB"]})
            config.PREFERENCES = stale
            self.assertEqual(["MOBI", "EPUB"],
                             config.settings()["preferred_formats"])
        finally:
            config.PREFERENCES = original

    def test_partial_settings_get_missing_defaults(self):
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            config.PREFERENCES["refresh_token"] = "configured-token"
            values = config.settings()
            self.assertEqual("configured-token", values["refresh_token"])
            self.assertEqual("", values["hardware_id"])
            self.assertFalse(values["enable_deletions"])
            self.assertTrue(values["use_tolino_column"])
            self.assertIsInstance(values["state"], dict)
        finally:
            config.PREFERENCES = original

    def test_optional_column_setting_survives_preferences_migration(self):
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            config.PREFERENCES["use_tolino_column"] = False
            values = config.settings()
            self.assertFalse(values["use_tolino_column"])
            self.assertFalse(config.PREFERENCES["use_tolino_column"])
        finally:
            config.PREFERENCES = original

    def test_legacy_settings_migrate_to_named_default_account(self):
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            config.PREFERENCES.update({
                "partner_id": 8, "hardware_id": "hw-a",
                "refresh_token": "token-a", "state": {"u": {"tolino_id": "a"}},
            })
            values = config.settings()
            self.assertEqual("default", values["active_account"])
            self.assertEqual([{"name": "default", "partner_id": 4,
                               "hardware_id": "hw-a", "refresh_token": "token-a",
                               "username": "", "password": "",
                               "state": {"u": {"tolino_id": "a"}}}],
                             values["accounts"])
            self.assertTrue(values["use_tolino_column"])
        finally:
            config.PREFERENCES = original

    def test_named_accounts_keep_tokens_and_state_isolated(self):
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            config.settings()
            config.save_account("alice", {"partner_id": 3, "refresh_token": "a",
                                          "state": {"u": {"tolino_id": "a"}}},
                                active="alice")
            config.save_account("bob", {"partner_id": 8, "refresh_token": "b",
                                        "state": {"u": {"tolino_id": "b"}}},
                                active="bob")
            values = config.settings()
            accounts = {item["name"]: item for item in values["accounts"]}
            self.assertEqual("bob", values["active_account"])
            self.assertEqual("a", accounts["alice"]["state"]["u"]["tolino_id"])
            self.assertEqual("b", accounts["bob"]["state"]["u"]["tolino_id"])
            self.assertEqual("a", accounts["alice"]["refresh_token"])
            self.assertEqual("b", accounts["bob"]["refresh_token"])
        finally:
            config.PREFERENCES = original

    def test_zip_entrypoint_uses_calibre_namespace_package(self):
        archive = Path(__file__).parent.parent / "tolino_cloud_sync.zip"
        if not archive.exists():
            self.skipTest("build_plugin.py has not been run")
        with zipfile.ZipFile(archive) as plugin:
            self.assertEqual({
                "__init__.py",
                "plugin-import-name-tolino_cloud_sync.txt",
                "ui.py",
                "config.py",
                "sync.py",
                "tolino.py",
                "cdp.py",
                "icons.py",
                "images/tolino_cloud_sync.png",
            }, set(plugin.namelist()))
            marker = next(name for name in plugin.namelist()
                          if name.startswith("plugin-import-name-") and name.endswith(".txt"))
            self.assertEqual("tolino_cloud_sync",
                             marker[len("plugin-import-name-"):-len(".txt")])
            self.assertEqual(b"", plugin.read(marker))
            metadata = ast.parse(plugin.read("__init__.py").decode("utf-8"))
            values = [
                node.value.value
                for node in ast.walk(metadata)
                if isinstance(node, ast.Assign)
                and any(getattr(target, "id", "") == "actual_plugin"
                        for target in node.targets)
                and isinstance(node.value, ast.Constant)
            ]
            self.assertEqual(["calibre_plugins.tolino_cloud_sync.ui:TolinoSyncAction"], values)

    def test_bundled_toolbar_icon_is_a_real_png(self):
        """The bundled image must be a real PNG."""
        with zipfile.ZipFile(Path(__file__).resolve().parents[1] / "tolino_cloud_sync.zip") as plugin:
            data = plugin.read("images/tolino_cloud_sync.png")
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"),
                        "bundled icon is not a PNG: %r" % data[:12])

    def test_browser_login_works_for_all_partners_with_auth_url(self):
        with patch("calibre_plugin.tolino.webbrowser.open", return_value=False):
            for partner_id in (1, 2, 4, 5, 6, 7):
                partner = PARTNERS[partner_id]
                if partner.get("auth_url") and partner.get("token_url"):
                    try:
                        browser_login(partner_id, "test_hardware")
                    except TolinoAuthError as exc:
                        error_msg = str(exc)
                        self.assertNotIn("local browser callback is not supported", error_msg)
                        self.assertNotIn("only registers its Web Reader redirect URI", error_msg)

    def test_browser_login_routes_keycloak_partner_to_assisted_login(self):
        """Orell Füssli (reseller 8) nutzt den Live-Grab, nie den
        System-Browser."""
        opened = []
        with patch("calibre_plugin.tolino.webbrowser.open",
                   side_effect=lambda url: opened.append(url) or True), \
             patch("calibre_plugin.tolino.grab_live_refresh",
                   return_value=("rotated", "hw-live")) as grab:
            self.assertEqual(("rotated", "hw-live"),
                             browser_login(4, "test_hardware"))
        grab.assert_called_once()
        self.assertEqual([], opened)


    def test_browser_login_keeps_localhost_callback_without_shared_reader(self):
        """Nur Shops OHNE den gemeinsamen Web Reader behalten den
        lokalen Callback-Server (127.0.0.1). Fuer Thalia, Hugendubel,
        eBook.de, buecher.de, Osiander und Orell Fuestli (alle mit
        webreader.mytolino.com) laeuft jetzt der gefuehrte Weg – deren
        OAuth-Redirect zeigt auf den Reader, nie auf localhost.
        """
        opened = []
        synthetic = {
            9: {"name": "Testladen ohne Reader",
                "key": 99,
                "reseller_id": "99",
                "client_id": "webshop01",
                "scope": "SCOPE_BOSH",
                "token_url": "https://example.invalid/oauth2/token",
                "auth_url": "https://example.invalid/oauth2/authorize"},
        }
        with patch.dict("calibre_plugin.tolino.PARTNERS", synthetic):
            with patch("calibre_plugin.tolino.webbrowser.open",
                       side_effect=lambda url: opened.append(url) or True):
                with self.assertRaises(TolinoAuthError):
                    browser_login(9, "test_hardware", timeout=0.2)
        self.assertEqual(1, len(opened))
        self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1", opened[0])


    def test_jwt_helpers_parse_iat_and_age(self):
        """JWT payload parsing extracts iat and shape."""
        import base64 as b64
        from .tolino import _refresh_token_iat, _jwt_shaped

        def make_jwt(payload):
            head = b64.urlsafe_b64encode(b'{"alg":"HS512","typ":"JWT"}')
            body = b64.urlsafe_b64encode(json.dumps(payload).encode())
            return "%s.%s.sig" % (head.decode().rstrip("="),
                                  body.decode().rstrip("="))

        now = time.time()
        fresh = make_jwt({"iat": now - 30, "typ": "Refresh"})
        self.assertEqual(now - 30, _refresh_token_iat(fresh))
        self.assertIsNone(_refresh_token_iat("plain-not-a-jwt"))
        self.assertTrue(_jwt_shaped(fresh))
        self.assertFalse(_jwt_shaped("short.not.jwt"))


    def test_keepalive_refresh_rotates_and_persists(self):
        """Keep-alive must silently rotate the stored token and persist it."""
        persisted = []

        class FakeClient:
            def __init__(self, partner, hw, token, username, password,
                         token_callback=None):
                self.token = token
                self.token_callback = token_callback

            def login(self):
                self.refresh = self.token + "-rotated"
                if self.token_callback:
                    self.token_callback(self.refresh)

        creds = {"partner_id": 4, "hardware_id": "hw", "refresh_token": "tok",
                 "username": "u", "password": "p"}
        with patch("calibre_plugin.tolino.TolinoClient", FakeClient):
            result = tolino_module.keepalive_refresh(
                lambda: creds, persisted.append)
        self.assertEqual("tok-rotated", result)
        self.assertEqual(["tok-rotated"], persisted)

    def test_keepalive_refresh_skips_when_there_is_nothing_to_do(self):
        """Ohne Token und bei laufender Rotation passiert nichts."""
        with patch("calibre_plugin.tolino.TolinoClient") as client_mock:
            self.assertIsNone(
                tolino_module.keepalive_refresh(lambda: {}, None))
            client_mock.assert_not_called()
            with tolino_module._KEEPALIVE_LOCK:
                self.assertIsNone(tolino_module.keepalive_refresh(
                    lambda: {"refresh_token": "tok"}, None))
            client_mock.assert_not_called()

class MetadataUpdatePlanTests(unittest.TestCase):
    """In-place Metadaten-Updates (/meta) nur für ausgewählte Bücher (0.13.0).

    Jede Zeile im Bestandsvergleich hat zwei Auswahlfelder: "Hochladen" lädt die
    Buchdatei, "Metadaten" gleicht nur Titel/Autor(en)/ISBN in der Cloud ab.
    Es gibt keine stille Umwandlung des einen in das andere.
    """

    def _book(self, **extra):
        book = {"uuid": "u1", "title": "A", "authors": "Fred Vargas",
                "isbn": "123", "series": "Adamsberg",
                "formats": ["EPUB"], "last_modified": "1",
                "format_signatures": {"EPUB": "100:1000"}}
        book.update(extra)
        return book

    def _state(self, book, **extra):
        entry = {"tolino_id": "d1",
                 "content_fp": content_fingerprint(book, "EPUB"),
                 "meta_fp": metadata_fingerprint(book)}
        entry.update(extra)
        return {"u1": entry}

    def test_selected_book_is_updated_in_place(self):
        book = self._book()
        state = self._state(book, meta_fp="stale")
        kept, updates = plan_metadata_updates({1: book}, state, [], ["EPUB"],
                                              only_book_ids={1})
        self.assertEqual([], kept)
        self.assertEqual(1, len(updates))
        record = unpack_meta_update_record(updates[0])
        self.assertEqual(1, record["book_id"])
        self.assertEqual("u1", record["book_uuid"])
        self.assertEqual("d1", record["tolino_id"])
        self.assertEqual({"title": "A", "author": "Fred Vargas", "isbn": "123"},
                         record["fields"])

    def test_unchecked_books_are_never_touched(self):
        """Ohne Auswahl kein /meta-Schreibvorgang – auch wenn sich die
        Metadaten unterscheiden (genau das war vorher unsichtbar)."""
        book = self._book()
        state = self._state(book, meta_fp="stale")
        for only in (set(), {"2"}):
            kept, updates = plan_metadata_updates({1: book}, state, [], ["EPUB"],
                                                  only_book_ids=only)
            self.assertEqual([], kept)
            self.assertEqual([], updates)

    def test_selected_upload_is_never_silently_converted(self):
        """"Hochladen" heisst hochladen – kein heimliches /meta."""
        book = self._book()
        state = self._state(book, meta_fp="stale")
        uploads = [(1, "u1", "EPUB", "d1")]
        kept, updates = plan_metadata_updates({1: book}, state, uploads, ["EPUB"],
                                              only_book_ids={1})
        self.assertEqual(uploads, kept)
        self.assertEqual([], updates)

    def test_replaced_file_stays_a_full_upload(self):
        book = self._book()
        state = self._state(book, content_fp="other-file", meta_fp="stale")
        uploads = [(1, "u1", "EPUB", "d1")]
        kept, updates = plan_metadata_updates({1: book}, state, uploads, ["EPUB"],
                                              only_book_ids={1})
        self.assertEqual(uploads, kept)
        self.assertEqual([], updates)

    def test_selected_metadata_update_does_not_need_the_book_file(self):
        """Ein reiner Metadaten-Abgleich braucht die Datei nicht – der
        Nutzer hat ausdrücklich nur die Metadaten ausgewählt."""
        book = self._book(format_signatures={})
        state = {"u1": {"tolino_id": "d1", "meta_fp": "stale"}}
        kept, updates = plan_metadata_updates({1: book}, state, [], ["EPUB"],
                                              only_book_ids={1})
        self.assertEqual([], kept)
        self.assertEqual(1, len(updates))

    def test_legacy_state_without_fingerprints_is_updated_when_selected(self):
        """Ohne gespeicherte Fingerabdrücke ist der Cloud-Stand unbekannt --
        wer auswählt, bekommt den Abgleich."""
        book = self._book()
        state = {"u1": {"tolino_id": "d1", "fingerprint": "x"}}
        kept, updates = plan_metadata_updates({1: book}, state, [], ["EPUB"],
                                              only_book_ids={1})
        self.assertEqual([], kept)
        self.assertEqual(1, len(updates))

    def test_without_selection_every_qualifying_book_is_planned(self):
        book = self._book()
        state = self._state(book, meta_fp="stale")
        kept, updates = plan_metadata_updates({1: book}, state, [], ["EPUB"])
        self.assertEqual([], kept)
        self.assertEqual(1, len(updates))
        self.assertEqual((1, "u1", "d1"), (updates[0][0], updates[0][1],
                                           updates[0][2]))

    def test_unchanged_metadata_plans_nothing(self):
        book = self._book()
        state = self._state(book)
        kept, updates = plan_metadata_updates({1: book}, state, [], ["EPUB"])
        self.assertEqual([], updates)

    def test_book_without_cloud_copy_is_skipped(self):
        book = self._book()
        kept, updates = plan_metadata_updates({1: book}, {}, [], ["EPUB"])
        self.assertEqual([], updates)

    def test_series_change_is_updated_without_sending_the_series(self):
        """Serien stehen nicht im /meta-Payload (dafür gibt es die
        Sammlungs-Zuordnung), zählen aber als Änderung."""
        old_book = self._book()
        new_book = self._book(series="Neue Reihe")
        state = {"u1": {"tolino_id": "d1",
                        "content_fp": content_fingerprint(new_book, "EPUB"),
                        "meta_fp": metadata_fingerprint(old_book)}}
        kept, updates = plan_metadata_updates({1: new_book}, state, [], ["EPUB"],
                                              only_book_ids={1})
        self.assertEqual([], kept)
        self.assertEqual(1, len(updates))
        self.assertNotIn("series", updates[0][3])

    def test_meta_update_record_validation(self):
        with self.assertRaisesRegex(ValueError, "invalid metadata update"):
            unpack_meta_update_record((1, "u1"))
        with self.assertRaisesRegex(ValueError, "incomplete metadata update"):
            unpack_meta_update_record((1, "u1", "d1", {}))
        with self.assertRaisesRegex(ValueError, "incomplete metadata update"):
            unpack_meta_update_record((1, "u1", "", {"title": "A"}))

    def test_plan_sync_state_carries_content_and_meta_hashes(self):
        book = self._book()
        uploads, removals, current = plan_sync({1: book}, {}, ["EPUB"])
        self.assertEqual([(1, "u1", "EPUB", None)], uploads)
        self.assertEqual([], removals)
        self.assertEqual(content_fingerprint(book, "EPUB"),
                         current["u1"]["content_fp"])
        self.assertEqual(metadata_fingerprint(book), current["u1"]["meta_fp"])

    def test_unselected_book_keeps_the_old_fingerprints(self):
        """Ohne Auswahl darf der Zustand nicht «abgeglichen» behaupten.

        `plan_sync` schreibt den neuen Stand für jedes Buch – auch für die,
        die der Nutzer nicht ausgewählt hat. Ohne Rücknahme hielte der nächste
        Vergleich die Cloud für aktuell, und ein späteres «Metadaten» wäre
        ein stiller Leerlauf.
        """
        old_book = self._book()
        new_book = self._book(title="Neu",
                              format_signatures={"EPUB": "200:2000"})
        previous = self._state(old_book)
        _uploads, _removals, current = plan_sync({1: new_book}, previous,
                                                 ["EPUB"])
        self.assertNotEqual(previous["u1"]["meta_fp"],
                            current["u1"]["meta_fp"])
        self.assertNotEqual(previous["u1"]["content_fp"],
                            current["u1"]["content_fp"])
        revert_unsynced_fingerprints(current, previous, [])
        self.assertEqual(previous["u1"]["meta_fp"],
                         current["u1"]["meta_fp"])
        self.assertEqual(previous["u1"]["content_fp"],
                         current["u1"]["content_fp"])
        self.assertNotIn("fingerprint", current["u1"])

    def test_synced_book_keeps_the_new_fingerprints(self):
        old_book = self._book()
        new_book = self._book(title="Neu",
                              format_signatures={"EPUB": "200:2000"})
        previous = self._state(old_book)
        _uploads, _removals, current = plan_sync({1: new_book}, previous,
                                                 ["EPUB"])
        revert_unsynced_fingerprints(current, previous, {"u1"})
        self.assertEqual(metadata_fingerprint(new_book),
                         current["u1"]["meta_fp"])
        self.assertEqual(content_fingerprint(new_book, "EPUB"),
                         current["u1"]["content_fp"])
        self.assertEqual(fingerprint(new_book, "EPUB"),
                         current["u1"]["fingerprint"])
        self.assertEqual("d1", current["u1"]["tolino_id"])

    def test_legacy_state_without_hashes_stays_clean_when_not_selected(self):
        """Alter Zustand ohne Fingerabdrücke: die Rücknahme darf weder
        Hashes noch Fremdfelder erfinden."""
        previous = {"u1": {"tolino_id": "d1"}}
        book = self._book()
        _uploads, _removals, current = plan_sync({1: book}, previous, ["EPUB"])
        revert_unsynced_fingerprints(current, previous, [])
        self.assertNotIn("fingerprint", current["u1"])
        self.assertNotIn("meta_fp", current["u1"])
        self.assertNotIn("content_fp", current["u1"])
        self.assertEqual("d1", current["u1"]["tolino_id"])
        # Ein Buch ganz ohne Vorgeschichte bleibt unangetastet: es gibt
        # nichts, worauf zurückgesetzt werden könnte.
        _uploads, _removals, fresh = plan_sync({1: book}, previous, ["EPUB"])
        revert_unsynced_fingerprints(fresh, {}, [])
        self.assertIn("meta_fp", fresh["u1"])

    def test_metadata_update_fields_skips_empty_values(self):
        self.assertEqual({}, metadata_update_fields({"title": " ", "isbn": ""}))
        self.assertEqual({"title": "T", "author": "A, B"},
                         metadata_update_fields({"title": " T ",
                                                 "authors": ["A", "B"]}))


class CollectionPlanTests(unittest.TestCase):
    """Serien → Sammlungen (0.12.0): Planung ohne Cloud-Zugriff.

    Der Abgleich selbst ist idempotent – hier wird nur geprüft, welche
    Paare überhaupt in Frage kommen.
    """

    def test_series_map_keeps_only_books_with_a_series(self):
        metadata = {
            1: {"uuid": "u1", "series": "Adamsberg"},
            2: {"uuid": "u2", "series": "   "},
            3: {"uuid": "", "series": "Ohne UUID"},
            4: {"uuid": "u4"},
        }
        self.assertEqual({"u1": "Adamsberg"},
                         series_map_from_metadata(metadata))
        self.assertEqual({}, series_map_from_metadata(None))

    def test_pairs_only_include_books_already_in_the_cloud(self):
        series_map = {"u1": "Adamsberg", "u2": "Adamsberg", "u3": "Neu"}
        state = {"u1": {"tolino_id": "t1"}, "u2": {"tolino_id": ""},
                 "u3": {}}
        self.assertEqual([("u1", "t1", "Adamsberg")],
                         plan_collection_updates(series_map, state))

    def test_pairs_are_sorted_and_deduplicated(self):
        series_map = {"u1": "SciFi", "u2": "Adamsberg"}
        state = {"u1": {"tolino_id": "t2"}, "u2": {"tolino_id": "t1"}}
        self.assertEqual([("u2", "t1", "Adamsberg"), ("u1", "t2", "SciFi")],
                         plan_collection_updates(series_map, state))

    def test_pairs_are_empty_for_missing_input(self):
        self.assertEqual([], plan_collection_updates(None, {"u": {}}))
        self.assertEqual([], plan_collection_updates({"u": "Serie"}, None))
        self.assertEqual([], plan_collection_updates({}, {}))

    def test_remembered_collection_is_not_planned_again(self):
        """Feldbefund: «Serie in der Cloud wird jedes Mal ausgeführt».
        Einmal geschrieben heisst gemerkt – der nächste Sync plant nichts
        und fragt die Cloud gar nicht erst."""
        series_map = {"u1": "Adamsberg"}
        state = {"u1": {"tolino_id": "t1"}}
        self.assertEqual([("u1", "t1", "Adamsberg")],
                         plan_collection_updates(series_map, state))
        self.assertTrue(remember_collection(state, "u1", "Adamsberg"))
        self.assertEqual(["Adamsberg"], state["u1"]["collections"])
        self.assertEqual([], plan_collection_updates(series_map, state))
        # Doppeltes Merken ändert nichts.
        self.assertFalse(remember_collection(state, "u1", "Adamsberg"))
        # Eine zweite Serie wird weiterhin geplant.
        series_map["u2"] = "Neu"
        state["u2"] = {"tolino_id": "t2"}
        self.assertEqual([("u2", "t2", "Neu")],
                         plan_collection_updates(series_map, state))

    def test_recorded_collections_ignore_broken_state(self):
        state = {"u1": {"tolino_id": "t1", "collections": "kaputt"}}
        self.assertEqual([("u1", "t1", "Adamsberg")],
                         plan_collection_updates({"u1": "Adamsberg"}, state))
        self.assertEqual(set(), recorded_collections(None))
        self.assertEqual(set(), recorded_collections({"collections": ["  "] }))
        self.assertFalse(remember_collection(None, "u1", "Adamsberg"))
        self.assertFalse(remember_collection({}, "", "Adamsberg"))
        self.assertFalse(remember_collection({}, "u1", "   "))

    def test_recorded_collections_survive_plan_sync(self):
        """Der Zustand wird bei jedem Sync neu aufgebaut – die gemerkten
        Zuordnungen dürfen dabei nicht verloren gehen."""
        book = {"uuid": "u1", "title": "A", "authors": "A", "isbn": "1",
                "formats": ["EPUB"], "last_modified": "1",
                "format_signatures": {"EPUB": "1:1"}}
        state = {"u1": {"tolino_id": "t1", "collections": ["Adamsberg"]}}
        _uploads, _removals, current = plan_sync({1: book}, state, ["EPUB"])
        self.assertEqual(["Adamsberg"], current["u1"]["collections"])
        self.assertEqual("t1", current["u1"]["tolino_id"])


class LiveGrabTests(unittest.TestCase):
    """v0.9.17: Der Live-Grab (CDP) ist der Primaerpfad der Browser-Anmeldung."""

    def _jwt(self, iat, sid=None):
        head = base64.urlsafe_b64encode(b'{"alg":"HS512","typ":"JWT"}')
        payload = {"iat": int(iat), "typ": "Refresh"}
        if sid:
            payload["sid"] = sid
        body = base64.urlsafe_b64encode(
            json.dumps(payload).encode("utf-8"))
        return "%s.%s.sig" % (head.decode().rstrip("="),
                              body.decode().rstrip("="))

    def test_chromium_candidates_detects_flatpak_installs(self):
        """Flatpak-Browser werden als `flatpak run`-Launcher erkannt."""
        import tempfile
        from unittest.mock import patch as _patch
        from . import cdp as cdp_module

        with tempfile.TemporaryDirectory() as home:
            for app_id in ("com.brave.Browser", "org.chromium.Chromium"):
                os.makedirs(os.path.join(home, ".var", "app", app_id))
            fake_flatpak = os.path.join(home, "bin", "flatpak")
            os.makedirs(os.path.dirname(fake_flatpak))
            with open(fake_flatpak, "w") as handle:
                handle.write("#!/bin/sh\nexit 0\n")
            os.chmod(fake_flatpak, 0o755)
            with _patch("shutil.which",
                        side_effect=lambda name: fake_flatpak
                        if name == "flatpak" else None):
                candidates = cdp_module.chromium_candidates(home=home)
        labels = [label for _argv, label in candidates]
        self.assertIn("Brave (Flatpak)", labels)
        self.assertIn("Chromium (Flatpak)", labels)
        brave = dict((label, argv) for argv, label in candidates)[
            "Brave (Flatpak)"]
        self.assertEqual(["run"], brave[1:2])
        self.assertIn("--command=brave", brave[2])
        self.assertEqual("com.brave.Browser", brave[3])

    def test_chromium_candidates_skips_flatpak_without_launcher(self):
        """Ohne `flatpak`-Binary im PATH gibt es keinen Flatpak-Kandidaten."""
        import tempfile
        from unittest.mock import patch as _patch
        from . import cdp as cdp_module

        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, ".var", "app", "com.brave.Browser"))
            with _patch("shutil.which", return_value=None), \
                 _patch.object(cdp_module, "_EXTRA_BROWSER_PATHS", ()):
                candidates = cdp_module.chromium_candidates(home=home)
        self.assertEqual([], candidates)

    def test_launch_reader_window_appends_flags_to_argv_prefix(self):
        """Der Launcher-Prefix (z. B. flatpak run) wird vor die Flags
        gesetzt, nicht damit verschmolzen."""
        import tempfile
        from unittest.mock import patch as _patch
        from . import cdp as cdp_module

        spawned = {}

        class FakePopen(object):
            def __init__(self, args, **kwargs):
                spawned["args"] = list(args)

        with tempfile.TemporaryDirectory() as home:
            prefix = ["/usr/bin/flatpak", "run", "--command=brave",
                      "com.brave.Browser"]
            with _patch.object(cdp_module, "pick_chromium",
                               return_value=(prefix, "Brave (Flatpak)")), \
                 _patch.object(cdp_module, "_clean_profile_dir"), \
                 _patch.object(cdp_module, "_profile_dir",
                               return_value=os.path.join(home, "prof")), \
                 _patch.object(cdp_module.subprocess, "Popen", FakePopen), \
                 _patch.object(cdp_module, "_http_get_json",
                               side_effect=[None, {"webSocketDebuggerUrl":
                                                   "ok"}]), \
                 _patch.object(cdp_module.time, "sleep"):
                _binary, port = cdp_module.launch_reader_window(4)
        args = spawned["args"]
        self.assertEqual(prefix, args[:4])
        self.assertIn("--remote-debugging-port=%d" % port, args)
        self.assertTrue(any(a.startswith("--user-data-dir=") for a in args))
        self.assertIn("mytolino.com", args[-1])

    def test_filter_live_candidates_drops_stale_jwts(self):
        """JWT-Kandidaten aelter als 2 Minuten werden verworfen."""
        from .tolino import _filter_live_candidates
        now = time.time()
        fresh = self._jwt(now - 10, "sess-1")
        stale = self._jwt(now - 3600, "sess-1")  # 1 h: ueber dem 30-min-Cutoff
        undated = "plainopaquevalue-not-a-jwt"
        kept = _filter_live_candidates([fresh, stale, undated])
        self.assertEqual([fresh, undated], kept)

    def test_grab_live_refresh_exchanges_freshest_token_once(self):
        """Der frischeste Live-Kandidat wird GENAU EINMAL getauscht."""
        from .tolino import grab_live_refresh

        now = time.time()
        fresh = self._jwt(now - 5, "sess-live")
        stale = self._jwt(now - 30, "sess-live")
        logins = []

        class FakeClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = "fake-hw"

            def _login(self):
                logins.append(self.refresh)
                self.refresh = "rotated-token"

        grabbed = {"refresh": [stale, fresh], "hardware": ["hw-live"]}
        with patch("calibre_plugin.tolino.TolinoClient", FakeClient), \
             patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value=grabbed), \
             patch("calibre_plugin.cdp.close_grabber_window") as closer:
            refresh, hardware = grab_live_refresh(4, "cfg-hw", timeout=1)
        self.assertEqual("rotated-token", refresh)
        self.assertEqual("fake-hw", hardware)
        self.assertEqual([fresh], logins)
        # Nach dem Tausch gehört die Sitzung Calibre: das Anmeldefenster
        # wird geschlossen, damit der Reader die übernommene Kopie nicht
        # weiter rotiert (0.9.30).
        closer.assert_called_once_with()

    def test_grab_live_refresh_rejects_only_stale_candidates(self):
        """Nur veraltete Kandidaten => Wartephase ohne Nachschub =>
        klare Fehlermeldung statt Replay. Das Fenster geht dabei zu: der
        nächste Versuch startet mit frischem Profil statt derselben toten
        Kopie (Feldbefund: invalid_grant-Loop, 0.9.30)."""
        from .tolino import grab_live_refresh
        stale = self._jwt(time.time() - 7200, "sess-old")  # 2 h alt
        with patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [stale], "hardware": []}), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   return_value=None), \
             patch("calibre_plugin.cdp.close_grabber_window") as closer, \
             patch("calibre_plugin.tolino._NEW_TOKEN_ATTEMPTS", 2), \
             patch("calibre_plugin.tolino._NEW_TOKEN_INTERVAL", 0):
            with self.assertRaises(TolinoAuthError) as ctx:
                grab_live_refresh(4, "cfg-hw", timeout=1)
        self.assertIn(ctx.exception.code, ("B01", "B02"))
        closer.assert_called_once_with()
        message = _full(ctx.exception)
        self.assertIn("wurde geschlossen", message)
        self.assertIn("Jetzt bei Tolino anmelden", message)
        self.assertIn("Zugang verbraucht", message)
        # Alter des Kandidaten (ohne Token-Wert) steht im Log-Detail.
        self.assertIn("Sekunden alt", ctx.exception.detail)
        self.assertNotIn(stale, message)

    def test_grab_live_refresh_rejection_recommends_fresh_grab(self):
        """Wird der Live-Token abgelehnt, wird KEIN zweiter Kandidat
        replayt – der Fehler verweist auf einen erneuten Grab."""
        from .tolino import grab_live_refresh
        logins = []

        class FailingClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = hw

            def _login(self):
                logins.append(self.refresh)
                raise TolinoAuthError(
                    'Tolino HTTP 400: {"error": "invalid_grant"}')

        fresh = self._jwt(time.time() - 5, "sess-live")
        other = self._jwt(time.time() - 8, "sess-other")
        with patch("calibre_plugin.tolino.TolinoClient", FailingClient), \
             patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [fresh, other],
                                 "hardware": []}), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   return_value=None), \
             patch("calibre_plugin.tolino._NEW_TOKEN_ATTEMPTS", 2), \
             patch("calibre_plugin.tolino._NEW_TOKEN_INTERVAL", 0):
            with self.assertRaises(TolinoAuthError) as ctx:
                grab_live_refresh(4, "cfg-hw", timeout=1)
        message = str(ctx.exception)
        self.assertIn("Jetzt bei Tolino anmelden", message)
        # Genau ein Login-Versuch: der zweite Kandidat wird bewusst
        # NICHT mehr probiert (kein Replay-Feuerwerk).
        self.assertEqual([fresh], logins)

    def test_keycloak_assisted_login_uses_live_grab_first(self):
        """browser_login(4) geht ueber den Live-Grab und oeffnet nie den
        System-Browser."""
        from .tolino import _keycloak_assisted_login
        with patch("calibre_plugin.tolino.grab_live_refresh",
                   return_value=("rotated", "hw-live")) as grab, \
             patch("calibre_plugin.tolino.webbrowser.open",
                   side_effect=AssertionError("webbrowser.open called")):
            result = _keycloak_assisted_login(4, "test_hardware")
        self.assertEqual(("rotated", "hw-live"), result)
        grab.assert_called_once()


    def test_grab_live_refresh_never_retries_a_rejected_candidate(self):
        """Ein am Endpunkt abgelehnter Kandidat wird nie wieder probiert
        (Keycloak-Reuse-Schutz) – auch nicht, wenn der Seiten-Speicher
        dieselbe Kopie erneut liefert. Statt einer erzwungenen Rotation
        wartet das Plugin auf eine NEU geschriebene Kopie; kommt keine,
        endet es mit einer klaren Fehlermeldung.
        """
        from .tolino import grab_live_refresh
        spent = self._jwt(time.time() - 10, "sess-live")
        logins = []

        class FailingClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = hw

            def _login(self):
                logins.append(self.refresh)
                raise TolinoAuthError("Tolino HTTP 400: invalid_grant")

        with patch("calibre_plugin.tolino.TolinoClient", FailingClient), \
             patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [spent], "hardware": []}), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   return_value={"refresh": [spent], "hardware": [],
                                 "idb": []}), \
             patch("calibre_plugin.tolino._NEW_TOKEN_ATTEMPTS", 2), \
             patch("calibre_plugin.tolino._NEW_TOKEN_INTERVAL", 0):
            with self.assertRaises(TolinoAuthError) as ctx:
                grab_live_refresh(4, "cfg-hw", timeout=1)
        self.assertEqual([spent], logins)
        self.assertIn("Letzter Fehler", _full(ctx.exception))

    def test_ws_recv_json_parses_rfc6455_header_correctly(self):
        """Der minimale WS-Client liest den Länge+MASK-Header korrekt."""
        import socket as socket_module
        import threading
        from .cdp import _Ws

        server_ready = threading.Event()
        bound = {}

        def server():
            listener = socket_module.socket()
            listener.setsockopt(socket_module.SOL_SOCKET,
                                socket_module.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            bound["port"] = listener.getsockname()[1]
            server_ready.set()
            conn, _addr = listener.accept()
            # Handshake minimal beantworten (Key egal, Client prueft nur 101)
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                         b"Upgrade: websocket\r\n"
                         b"Connection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: x\r\n\r\n")
            # Masked client frame empfangen
            def read_exact(n):
                data = b""
                while len(data) < n:
                    chunk = conn.recv(n - len(data))
                    if not chunk:
                        break
                    data += chunk
                return data
            header = read_exact(2)
            assert (header[0] & 0x0F) == 0x1
            length = header[1] & 0x7F
            assert header[1] & 0x80, "client frames must be masked"
            mask = read_exact(4)
            payload = read_exact(length)
            unmasked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            # Unmaskierte JSON-Antwort als UNMASKED Server-Frame zurueck
            # (auf > 125 Byte gepolstert, damit der 126er-Extended-
            # Length-Pfad mitgetestet wird)
            value = unmasked.decode() + "|" + "p" * 60
            reply = json.dumps({"id": 1, "result": {"value": value}}).encode()
            assert len(reply) > 125, "reply too short: %d" % len(reply)
            # Server-Frame korrekt kodieren: ab 126 Byte Extended Length.
            import struct as struct_module
            frame = bytearray([0x81, 126])
            frame += struct_module.pack(">H", len(reply))
            conn.sendall(bytes(frame) + reply)
            conn.close()
            listener.close()

        thread = threading.Thread(target=server, daemon=True)
        thread.start()
        server_ready.wait(5)

        # Der Server-Thread meldet seinen gebundenen Port zurueck.
        port = bound["port"]

        ws = _Ws.connect("127.0.0.1", port, "/devtools/page/test", timeout=5)
        try:
            ws.send_json({"id": 1, "method": "Runtime.evaluate",
                          "params": {"expression": "1+1"}})
            message = ws.recv_json(timeout=5)
        finally:
            ws.close()
        self.assertEqual(1, message.get("id"))
        # Value ist das vom Server korrekt entmaskierte Echo des Requests:
        self.assertTrue(message["result"]["value"].startswith(
            '{"id": 1, "method": "Runtime.evaluate"'))

    def test_filter_live_cutoff_accepts_tokens_from_older_signin(self):
        """Tokens bis 30 Minuten Alter ueberleben den Live-Cutoff."""
        from .tolino import _filter_live_candidates
        now = time.time()
        ten_minutes_old = self._jwt(now - 600, "sess-1")
        kept = _filter_live_candidates([ten_minutes_old])
        self.assertEqual([ten_minutes_old], kept)
        # Ausdruecklich mit dem Default aufgerufen bleibt es bei 1800s:
        self.assertIn("1800",
                      _filter_live_candidates.__defaults__[0].__str__())

    def test_try_live_grab_first_returns_none_without_window(self):
        """Ohne laufendes Grabber-Fenster -> None (kein Live-Grab)."""
        from .tolino import try_live_grab_first
        with patch("calibre_plugin.cdp.devtools_port_alive",
                   return_value=False), \
             patch("calibre_plugin.tolino.grab_live_refresh",
                   side_effect=AssertionError("grab called")):
            self.assertIsNone(try_live_grab_first(4, "hw"))

    def test_try_live_grab_first_grabs_when_window_alive(self):
        """Laufendes Fenster -> EIN Seiten-Lesen, EIN Tausch, Ergebnis."""
        from .tolino import try_live_grab_first
        fresh = self._jwt(time.time() - 5, "sess-live")

        class FakeClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = fresh
                self.hardware = "hw-from-grab"

            def _login(self):
                self.refresh = "rotated-token"

        with patch("calibre_plugin.cdp.devtools_port_alive",
                   return_value=True), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   return_value={"refresh": [fresh], "hardware": ["hw-live"],
                                 "idb": []}), \
             patch("calibre_plugin.cdp.describe_grab_state",
                   return_value="ok"), \
             patch("calibre_plugin.cdp.close_grabber_window") as closer, \
             patch("calibre_plugin.tolino.TolinoClient", FakeClient):
            result = try_live_grab_first(4, "hw")
        self.assertEqual(("rotated-token", "hw-from-grab"), result)
        # Auch dieser Weg schliesst das Fenster nach dem Übernehmen.
        closer.assert_called_once_with()

    def test_try_live_grab_first_raises_when_window_dead_yields_nothing(self):
        """Fenster lebt, liefert aber nichts -> klare Meldung statt Leere."""
        from .tolino import try_live_grab_first
        with patch("calibre_plugin.cdp.devtools_port_alive",
                   return_value=True), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   return_value={"refresh": [], "hardware": [], "idb": []}), \
             patch("calibre_plugin.cdp.describe_grab_state",
                   return_value="0 Refresh-Kandidat(en)"):
            with self.assertRaises(TolinoAuthError) as ctx:
                try_live_grab_first(4, "hw")
        self.assertIn("kein gültiger Zugang", str(ctx.exception))

    def test_exchange_prefers_refresh_typ_over_newer_access_token(self):
        """Access-JWTs (kurzlebig) werden nicht vor dem Refresh-JWT
        verbraten, auch wenn ihr iat neuer ist."""
        from .tolino import _exchange_grabbed_token
        now = time.time()
        access = self._jwt(now - 2, "sess-live")
        # Access-Variante: gleicher Aufbau, aber typ nicht "Refresh"
        head = base64.urlsafe_b64encode(b'{"alg":"HS512","typ":"JWT"}')
        body = base64.urlsafe_b64encode(json.dumps(
            {"iat": int(now - 2), "typ": "Bearer"}).encode())
        access = "%s.%s.sig" % (head.decode().rstrip("="),
                                body.decode().rstrip("="))
        refresh = self._jwt(now - 30, "sess-live")
        logins = []

        class FakeClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = hw

            def _login(self):
                logins.append(self.refresh)
                self.refresh = "rotated"

        grabbed = {"refresh": [access, refresh], "hardware": []}
        with patch("calibre_plugin.tolino.TolinoClient", FakeClient):
            result = _exchange_grabbed_token(4, "hw", grabbed,
                                             [access, refresh])
        self.assertEqual([refresh], logins)
        self.assertEqual("rotated", result[0])

    def test_try_live_grab_first_error_includes_page_state(self):
        """Fehlermeldung enthaelt die redigierte Seiten-Zusammenfassung."""
        from .tolino import try_live_grab_first
        with patch("calibre_plugin.cdp.devtools_port_alive",
                   return_value=True), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   return_value=None), \
             patch("calibre_plugin.cdp.describe_grab_state",
                   return_value="kein Web-Reader-Tab"):
            with self.assertRaises(TolinoAuthError) as ctx:
                try_live_grab_first(4, "hw")
        self.assertIn("kein Web-Reader-Tab", _full(ctx.exception))

    def test_grab_live_refresh_picks_up_token_written_during_wait(self):
        """Leerer erster Grab => das Plugin wartet, bis der Reader selbst
        einen Token schreibt, und tauscht diesen -- ohne erzwungene
        Rotation, ohne Netzwerk-Interception (beides 0.9.28 entfernt)."""
        from .tolino import grab_live_refresh
        later = self._jwt(time.time() - 1, "sess-late")

        class FakeClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = hw

            def _login(self):
                self.refresh = "rotated-after-wait"

        with patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [], "hardware": [],
                                 "idb": []}), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   return_value={"refresh": [later],
                                 "hardware": ["hw-live"], "idb": []}), \
             patch("calibre_plugin.tolino._NEW_TOKEN_INTERVAL", 0), \
             patch("calibre_plugin.tolino.TolinoClient", FakeClient):
            refresh, hardware = grab_live_refresh(4, "cfg-hw", timeout=1)
        self.assertEqual("rotated-after-wait", refresh)
        self.assertEqual("hw-live", hardware)

    def test_try_live_grab_first_fails_fast_after_rejection(self):
        """Ein abgelehnter Kandidat blockiert den GUI-Thread NICHT mit
        einer Lauscherphase – sofortige, klare Fehlermeldung."""
        from .tolino import try_live_grab_first
        fresh = self._jwt(time.time() - 5, "sess-live")
        logins = []

        class FailingClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = hw

            def _login(self):
                logins.append(self.refresh)
                raise TolinoAuthError("Tolino HTTP 400: invalid_grant")

        with patch("calibre_plugin.cdp.devtools_port_alive",
                   return_value=True), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   return_value={"refresh": [fresh], "hardware": [],
                                 "idb": []}), \
             patch("calibre_plugin.cdp.describe_grab_state",
                   return_value="1 Refresh-Kandidat(en)"), \
             patch("calibre_plugin.tolino.TolinoClient", FailingClient):
            with self.assertRaises(TolinoAuthError) as ctx:
                try_live_grab_first(4, "hw")
        self.assertIn("Letzter Fehler", _full(ctx.exception))
        self.assertEqual([fresh], logins)

    def test_exchange_refresh_in_browser_parses_page_reply(self):
        """In-page-fetch-Antwort (status/body) wird korrekt geparst."""
        from . import cdp as cdp_module
        page_reply = json.dumps({
            "status": 200,
            "body": json.dumps({"access_token": "a", "refresh_token": "r"}),
        })
        with patch("calibre_plugin.cdp._evaluate_raw_in_target",
                   return_value={"ok": True, "value": page_reply,
                                 "exception": None}):
            status, body = cdp_module.exchange_refresh_in_browser(
                "ws://127.0.0.1:9223/devtools/page/x",
                "https://www.orellfuessli.ch/auth/oauth2/token",
                "client_id=webreader&grant_type=refresh_token")
        self.assertEqual(200, status)
        self.assertIn("refresh_token", body)

    def test_exchange_refresh_in_browser_reports_page_failure(self):
        """Ein fehlgeschlagener Page-Evaluate gibt status=0 zurueck."""
        from . import cdp as cdp_module
        with patch("calibre_plugin.cdp._evaluate_raw_in_target",
                   return_value={"ok": False, "value": None,
                                 "exception": "page blew up"}):
            status, body = cdp_module.exchange_refresh_in_browser(
                "ws://127.0.0.1:9223/devtools/page/x", "https://x/token",
                "a=b")
        self.assertEqual(0, status)
        self.assertIn("page blew up", body)

    def test_exchange_grabbed_token_prefers_browser_exchange(self):
        """200er Browser-Tausch schlaegt den Plugin-POST – kein WAF-403."""
        from .tolino import _exchange_grabbed_token
        fresh = self._jwt(time.time() - 5, "sess-live")
        logins = []

        def fake_browser_exchange(ws_url, token_url, form_body, timeout=25):
            self.assertIn("grant_type=refresh_token", form_body)
            return 200, json.dumps({
                "access_token": "a", "refresh_token": "rotated-browser"})

        with patch("calibre_plugin.cdp.reader_ws_url",
                   return_value="ws://127.0.0.1:9223/devtools/page/x"), \
             patch("calibre_plugin.cdp.exchange_refresh_in_browser",
                   side_effect=fake_browser_exchange), \
             patch("calibre_plugin.tolino.TolinoClient") as client_cls:
            client_cls.return_value.refresh = fresh
            client_cls.return_value.hardware = "hw-grab"
            client_cls.return_value._apply_token_response.side_effect = \
                lambda data: logins.append(data) or setattr(
                    client_cls.return_value, "refresh",
                    data["refresh_token"])
            refresh, hardware = _exchange_grabbed_token(
                4, "hw-grab", {"refresh": [fresh], "hardware": ["hw-grab"]},
                [fresh])
        self.assertEqual("rotated-browser", refresh)
        self.assertEqual("hw-grab", hardware)
        # Der Plugin-Client wurde NIE _login (Plugin-POST) aufgerufen:
        self.assertEqual(1, len(logins))

    def test_exchange_grabbed_token_falls_back_to_plugin_post(self):
        """Browser-Tausch fehlgeschlagen (0/nicht-200) -> Plugin-POST."""
        from .tolino import _exchange_grabbed_token
        fresh = self._jwt(time.time() - 5, "sess-live")

        class FakeClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = fresh
                self.hardware = hw

            def _login(self):
                self.refresh = "rotated-plugin"

        with patch("calibre_plugin.cdp.reader_ws_url",
                   return_value="ws://127.0.0.1:9223/devtools/page/x"), \
             patch("calibre_plugin.cdp.exchange_refresh_in_browser",
                   return_value=(403, "Zugriff geblockt")), \
             patch("calibre_plugin.tolino.TolinoClient", FakeClient):
            refresh, hardware = _exchange_grabbed_token(
                4, "hw", {"refresh": [fresh], "hardware": []}, [fresh])
        self.assertEqual("rotated-plugin", refresh)

    def test_grab_live_refresh_adopts_new_candidate_and_reader_hardware(self):
        """Nach einer Ablehnung uebernimmt das Plugin die NEU geschriebene
        Kopie des Readers – und die Hardware-ID der aktuellen Sitzung
        gewinnt gegen die konfigurierte (Nutzerfall: gespeichert
        da284d4b..., Reader-Sitzung lief mit eb22e4cf...)."""
        from .tolino import grab_live_refresh
        spent = self._jwt(time.time() - 10, "sess-live")
        fresh = self._jwt(time.time() - 5, "sess-live")
        logins = []

        class FirstFails(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = hw

            def _login(self):
                logins.append(self.refresh)
                raise TolinoAuthError("Tolino HTTP 400: invalid_grant")

        class SecondWorks(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = hw

            def _login(self):
                logins.append(self.refresh)
                self.refresh = "rotated-after-wait"

        clients = [FirstFails, SecondWorks]

        def fake_client(partner_id, hw):
            return clients.pop(0)(partner_id, hw)

        reader_hw = "eb22e4cf-bb01-4550-bff7-334446bb20b1"
        with patch("calibre_plugin.tolino.TolinoClient", fake_client), \
             patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [spent], "hardware": []}), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   return_value={"refresh": [fresh],
                                 "hardware": [reader_hw],
                                 "idb": []}), \
             patch("calibre_plugin.tolino._NEW_TOKEN_INTERVAL", 0):
            refresh, hardware = grab_live_refresh(4, "cfg-hw", timeout=1)
        self.assertEqual("rotated-after-wait", refresh)
        self.assertEqual(reader_hw, hardware)
        self.assertEqual([spent, fresh], logins)

    def test_exchange_uses_grabbed_hardware_over_configured(self):
        """Die aus dem Reader mitgelesene Hardware-ID wird beim Tausch
        verwendet – nicht die alte konfigurierte (Nutzerfall: Plugin
        speicherte da284d4b..., Reader-Sitzung lief mit eb22e4cf...)."""
        from .tolino import _exchange_grabbed_token
        fresh = self._jwt(time.time() - 5, "sess-live")
        seen_hw = []

        class FakeClient(object):
            def __init__(self, partner_id, hw):
                seen_hw.append(hw)
                self.refresh = fresh
                self.hardware = hw

            def _login(self):
                self.refresh = "rotated"

        with patch("calibre_plugin.cdp.reader_ws_url", return_value=None), \
             patch("calibre_plugin.tolino.TolinoClient", FakeClient):
            _exchange_grabbed_token(
                4, "da284d4b-6348-43c8-b0ab-17edcddfb25d",
                {"refresh": [fresh],
                 "hardware": ["eb22e4cf-bb01-4550-bff7-334446bb20b1"]},
                [fresh])
        self.assertEqual(
            ["eb22e4cf-bb01-4550-bff7-334446bb20b1"], seen_hw)

    def test_grab_live_refresh_still_fails_when_no_new_candidate_appears(self):
        """Weder Storage noch Nachschub waehrend der Wartephase => klare
        Fehlermeldung mit dem ersten Endpoint-Fehler."""
        from .tolino import grab_live_refresh
        spent = self._jwt(time.time() - 10, "sess-live")

        class FailingClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = hw

            def _login(self):
                raise TolinoAuthError("Tolino HTTP 400: invalid_grant")

        with patch("calibre_plugin.tolino.TolinoClient", FailingClient), \
             patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [spent], "hardware": []}), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   return_value=None), \
             patch("calibre_plugin.tolino._NEW_TOKEN_ATTEMPTS", 2), \
             patch("calibre_plugin.tolino._NEW_TOKEN_INTERVAL", 0):
            with self.assertRaises(TolinoAuthError) as ctx:
                grab_live_refresh(4, "cfg-hw", timeout=1)
        message = _full(ctx.exception)
        self.assertIn("Zugang verbraucht", message)
        self.assertIn("Letzter Fehler", message)
        # Die Meldung nennt nur das ALTER des geprüften Kandidaten
        # («5 Sekunden alt» vs. «3700 Sekunden alt»), nie seinen Wert.
        self.assertIn("Sekunden alt", message)
        self.assertNotIn(spent, message)

    def test_keycloak_assisted_login_propagates_grab_errors(self):
        """Andere Grab-Fehler (Timeout, abgelehnt) werden weitergereicht
        – kein stiller Wechsel auf einen anderen Weg."""
        from .tolino import _keycloak_assisted_login
        with patch("calibre_plugin.tolino.grab_live_refresh",
                   side_effect=TolinoAuthError(
                       "Timeout: Im Anmeldefenster wurde keine Web-"
                       "Reader-Anmeldung erkannt.")):
            with self.assertRaises(TolinoAuthError) as ctx:
                _keycloak_assisted_login(4, "test_hardware")
        self.assertIn("Timeout", str(ctx.exception))


    def test_collect_grab_waits_while_window_is_on_partner_login_page(self):
        """Feldbefund 0.9.27: Waehrend der Anmeldung verlaesst der Tab
        mytolino.com zugunsten der Keycloak-Seite des Partners (andere
        Domain) – das darf NIEMALS als "Fenster geschlossen" gemeldet
        werden, solange der DevTools-Endpunkt antwortet."""
        from . import cdp as cdp_module
        pages = [
            [{"type": "page",
              "url": "https://www.orellfuessli.ch/keycloak/realms/37/"
                     "protocol/openid-connect/auth",
              "webSocketDebuggerUrl": "ws://x"}],
            [{"type": "page",
              "url": "https://webreader.mytolino.com/library/index.html",
              "webSocketDebuggerUrl":
                  "ws://127.0.0.1:9223/devtools/page/x"}],
        ]
        messages = []
        with patch("calibre_plugin.cdp._http_get_json",
                   side_effect=pages), \
             patch("calibre_plugin.cdp._grab_page_tokens",
                   return_value={"refresh": ["fresh-from-reader-token"],
                                 "hardware": [], "idb": []}), \
             patch("calibre_plugin.cdp.time.sleep", lambda _s: None):
            result = cdp_module.collect_grab(4, 9223, timeout=30,
                                             progress=messages.append)
        self.assertEqual(["fresh-from-reader-token"], result["refresh"])
        self.assertTrue(any("offen" in text for text in messages))
        self.assertTrue(all("geschlossen" not in text
                            for text in messages))

    def test_collect_grab_reports_closed_only_when_endpoint_is_gone(self):
        """Erst wenn das DevTools-Endpunkt mehrfach in Folge nicht
        antwortet, ist das Fenster wirklich zu (kein Einzel-Aussetzer)."""
        from . import cdp as cdp_module
        with patch("calibre_plugin.cdp._http_get_json",
                   return_value=None), \
             patch("calibre_plugin.cdp.time.sleep", lambda _s: None):
            with self.assertRaises(TolinoAuthError) as ctx:
                cdp_module.collect_grab(4, 9223, timeout=30,
                                        progress=lambda _t: None)
        self.assertIn("wurde geschlossen", str(ctx.exception))

    def test_collect_grab_timeout_without_reader_tab_has_login_hint(self):
        """Bleibt der Tab die ganze Zeit ohne Web-Reader (z. B. Anmeldung
        nicht abgeschlossen), gibt es einen Timeout-Hinweis – keine
        geschlossene-Fenster-Meldung."""
        from . import cdp as cdp_module
        with self.assertRaises(TolinoAuthError) as ctx:
            cdp_module.collect_grab(4, 9223, timeout=0)
        message = str(ctx.exception)
        self.assertIn("Zeitüberschreitung", message)
        self.assertIn("nicht abgeschlossen", message)
        self.assertNotIn("wurde geschlossen", message)

    def _ws_run_server(self, thread_state, handler, connections=1):
        """Minimaler In-Process-WS-Server (Schema der 0.9.19-Tests):
        bindet, meldet den Port via thread_state["ready"] und beantwortet
        bis zu `connections` aufeinanderfolgende Verbindungen per
        `handler(message, send_raw)` – jeder CDP-Aufruf oeffnet eine
        eigene Verbindung.
        """
        import socket as socket_module
        import struct as struct_module
        srv = socket_module.socket()
        srv.setsockopt(socket_module.SOL_SOCKET,
                       socket_module.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        thread_state["port"] = srv.getsockname()[1]
        thread_state["ready"].set()
        try:
            for _ in range(connections):
                conn, _addr = srv.accept()
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                             b"Upgrade: websocket\r\n"
                             b"Connection: Upgrade\r\n"
                             b"Sec-WebSocket-Accept: x\r\n\r\n")

                def read_exact(n, conn=conn):
                    data = b""
                    while len(data) < n:
                        chunk = conn.recv(n - len(data))
                        if not chunk:
                            break
                        data += chunk
                    return data

                def send_raw(body, conn=conn):
                    frame = bytearray([0x81])
                    if len(body) < 126:
                        frame.append(len(body))
                    else:
                        frame += bytes([126]) + struct_module.pack(
                            ">H", len(body))
                    conn.sendall(bytes(frame) + body)

                try:
                    while True:
                        header = read_exact(2)
                        if len(header) < 2:
                            break
                        length = header[1] & 0x7F
                        if length == 126:
                            length = struct_module.unpack(
                                ">H", read_exact(2))[0]
                        elif length == 127:
                            length = struct_module.unpack(
                                ">Q", read_exact(8))[0]
                        if header[1] & 0x80:
                            mask = read_exact(4)
                            payload = bytes(b ^ mask[i % 4] for i, b in
                                            enumerate(read_exact(length)))
                        else:
                            payload = read_exact(length)
                        try:
                            message = json.loads(payload.decode())
                        except ValueError:
                            continue
                        handler(message, send_raw)
                finally:
                    conn.close()
        finally:
            srv.close()

    def _ws_start_server(self, handler, connections=1):
        """Server-Thread starten; Rueckgabe: State mit ready/port."""
        import threading
        state = {"ready": threading.Event(), "port": None}
        threading.Thread(target=self._ws_run_server,
                         args=(state, handler, connections),
                         daemon=True).start()
        return state

    @staticmethod
    def _send_cdp_value(send_raw, message_id, value):
        send_raw(json.dumps({
            "id": message_id,
            "result": {"result": {"value": value}}}).encode())

    def test_evaluate_in_target_parses_json_string_replies(self):
        """Regression 0.9.27: Das Grab-Snippet antwortet mit
        JSON.stringify(out) – einem STRING. _evaluate_in_target muss
        diese Antwort parsten, sonst bleibt jeder Grab leer ("kein
        frischer Token"); frueher antworteten nur die Test-Server mit
        dict, wodurch die Regression unsichtbar blieb.
        """
        from . import cdp as cdp_module

        def handler(message, send_raw):
            if message.get("method") != "Runtime.evaluate":
                return None
            payload = {"refresh": ["string-reply-caught-token-xyz"],
                       "hardware": [], "idb": []}
            self._send_cdp_value(send_raw, message["id"],
                                 json.dumps(payload))

        state = self._ws_start_server(handler)
        self.assertTrue(state["ready"].wait(5))
        result = cdp_module._evaluate_in_target(
            "ws://127.0.0.1:%d/devtools/page/x" % state["port"],
            "JSON.stringify(out)", timeout=5)
        self.assertIsNotNone(result)
        self.assertEqual(["string-reply-caught-token-xyz"],
                         result["refresh"])

    def test_grab_page_tokens_merges_indexeddb_databases(self):
        """_grab_page_tokens liest die vom Haupt-Snippet gemeldeten
        IndexedDB-Datenbanken mit: der Reader legt sein aktuelles
        Token-Set oft genau dort ab (0.9.21), und ohne den Merge ist er
        fuer den Grab unsichtbar."""
        from . import cdp as cdp_module
        reader_hw = "eb22e4cf-bb01-4550-bff7-334446bb20b1"

        def handler(message, send_raw):
            expr = str((message.get("params") or {}).get("expression", ""))
            if "objectStoreNames" in expr:
                payload = {"refresh": ["idb-token-xyz"],
                           "hardware": [reader_hw]}
                self._send_cdp_value(send_raw, message["id"],
                                     json.dumps(payload))
                return None
            payload = {"refresh": ["storage-token-abc"],
                       "hardware": [], "idb": ["reader-token-db"]}
            self._send_cdp_value(send_raw, message["id"],
                                 json.dumps(payload))

        state = self._ws_start_server(handler, connections=2)
        self.assertTrue(state["ready"].wait(5))
        grabbed = cdp_module._grab_page_tokens(
            "ws://127.0.0.1:%d/devtools/page/x" % state["port"], timeout=5)
        self.assertEqual(["storage-token-abc", "idb-token-xyz"],
                         grabbed["refresh"])
        self.assertEqual([reader_hw], grabbed["hardware"])
        self.assertEqual(["reader-token-db"], grabbed["idb"])

    def test_grab_snippets_parse_json_blob_store_values(self):
        """Top-Level-Speicher-Werte sind Strings: JSON-Blobs
        (oidc.user:...) muessen geparst werden, sonst ist der aktuelle
        Token unsichtbar (Feldbefund 0.9.27 "kein frischer Token")."""
        from .cdp import _GRAB_SNIPPET, _IDB_SNIPPET
        for snippet in (_GRAB_SNIPPET, _IDB_SNIPPET):
            self.assertIn("tryParse(value)", snippet)

    def test_browser_login_forwards_progress_into_live_grab(self):
        """Die Dialog-Fortschrittstexte (Fenster offen, Rotation
        erzwungen) muessen bis zum Live-Grab durchreichen."""
        progress_cb = lambda _text: None
        with patch("calibre_plugin.tolino.grab_live_refresh",
                   return_value=("rotated", "hw-live")) as grab:
            result = browser_login(4, "test_hardware",
                                   progress=progress_cb)
        self.assertEqual(("rotated", "hw-live"), result)
        grab.assert_called_once()
        self.assertIs(progress_cb, grab.call_args[1].get("progress"))


class ToolbarIconTests(unittest.TestCase):
    """The toolbar action must receive an icon in real Calibre runs."""

    @staticmethod
    def _load_ui_module(get_icons_result, qt_stub=None):
        """Import ui.py with Calibre and Qt imports stubbed.

        `qt_stub` überschreibt einzelne Qt-Namen (z. B. einen ehrlichen
        QDialog/QTableWidget-Ersatz, damit der Aufbau eines Fensters
        wirklich durchlaufen werden kann).
        """
        import sys
        import types

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
                     "QInputDialog", "QFileDialog", "Qt", "QTimer"):
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
        saved = {name: sys.modules.get(name)
                 for name in ("calibre", "calibre.gui2",
                              "calibre.gui2.actions", "qt.core", "qt",
                              "calibre_plugin.ui")}
        for name, module in (("calibre", calibre),
                             ("calibre.gui2", calibre_gui2),
                             ("calibre.gui2.actions", calibre_actions),
                             ("qt", types.ModuleType("qt")),
                             ("qt.core", qt_core)):
            sys.modules[name] = module
        sys.modules.pop("calibre_plugin.ui", None)
        import calibre_plugin
        # Das Paket hält sonst die Referenz auf das zuvor geladene
        # ui-Modul (anderer Qt-Stub) und `from ... import ui` gäbe genau
        # diese alte Fassung zurück.
        if getattr(calibre_plugin, "ui", None) is not None:
            delattr(calibre_plugin, "ui")
        ui_module = importlib.import_module("calibre_plugin.ui")
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

    def test_get_icons_result_is_set_on_action(self):
        set_calls = []

        class Icon:
            def isNull(self):
                return False

        class QAction:
            def setIcon(self, icon):
                set_calls.append(icon)

        ui_module = self._load_ui_module(Icon())
        try:
            action = QAction()
            ui_module._apply_toolbar_icon(action)
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual(1, len(set_calls))
        self.assertIsInstance(set_calls[0], Icon)

    def test_null_icon_leaves_action_untouched(self):
        set_calls = []

        class Icon:
            def isNull(self):
                return True

        class QAction:
            def setIcon(self, icon):
                set_calls.append(icon)

        ui_module = self._load_ui_module(Icon())
        try:
            action = QAction()
            ui_module._apply_toolbar_icon(action)
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual([], set_calls)


class TolinoClientFeatureTests(unittest.TestCase):
    """Tests for the cloud client: device list, download, sync-data
    collections/read-state and refresh expiry tracking."""

    def _client(self):
        return TolinoClient(4, "hardware", "refresh-token")

    def _login_client(self, token_response=None):
        client = self._client()

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return token_response or (
                    b'{"access_token":"access-secret",'
                    b'"refresh_token":"rotated","expires_in":3600,'
                    b'"refresh_expires_in":36000}')

        with patch("calibre_plugin.tolino._curl_binary", return_value=None), \
                patch("calibre_plugin.tolino._impersonate_session", return_value=None), \
                        patch("calibre_plugin.tolino.urlopen", return_value=Response()):
            client.login()
        return client

    def test_refresh_expiry_is_tracked_from_token_response(self):
        client = self._login_client()
        self.assertGreater(client.refresh_expires_at, time.time())
        self.assertLessEqual(client.refresh_expires_in, 36000)
        diagnostics = client.auth_diagnostics()
        self.assertIn("refresh_expires_in", diagnostics)
        self.assertLessEqual(diagnostics["refresh_expires_in"], 36000)

    def test_refresh_expiry_unknown_when_response_lacks_field(self):
        client = self._login_client(
            b'{"access_token":"access-secret","expires_in":3600}')
        self.assertEqual(0, client.refresh_expires_at)
        self.assertEqual(0, client.refresh_expires_in)

    def _device_list_client(self, devices_payload):
        client = self._login_client()
        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return devices_payload

        def request(url_request, timeout):
            captured["url"] = url_request.full_url
            captured["body"] = url_request.data.decode("utf-8")
            return Response()

        with patch("calibre_plugin.tolino._curl_binary", return_value=None), \
                patch("calibre_plugin.tolino._impersonate_session", return_value=None), \
                        patch("calibre_plugin.tolino.urlopen", request):
            hardware = client.fetch_hardware_id()
        self.assertIn("handshake/devices/list", captured["url"])
        self.assertIn("deviceListRequest", captured["body"])
        self.assertIn("auth_token", captured["body"])
        return hardware

    def test_fetch_hardware_id_returns_most_recent_device(self):
        payload = (b'{"deviceListResponse":{"devices":[{'
                   b'"deviceId":"older","deviceLastUsage":"100"},{'
                   b'"deviceId":"newest","deviceLastUsage":"200"}]}}')
        self.assertEqual("newest", self._device_list_client(payload))

    def test_fetch_hardware_id_rejects_empty_device_list(self):
        from .tolino import TolinoApiError
        client = self._login_client()

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"deviceListResponse":{"devices":[]}}'

        with patch("calibre_plugin.tolino._curl_binary", return_value=None), \
                patch("calibre_plugin.tolino._impersonate_session", return_value=None), \
                        patch("calibre_plugin.tolino.urlopen", return_value=Response()):
            with self.assertRaises(TolinoApiError):
                client.fetch_hardware_id()

    def _patching_client(self, responses):
        """Client whose _request returns queued dict responses."""
        client = self._login_client()
        queue = list(responses)
        calls = []

        def fake_request(url, method="GET", data=None, form=False,
                         authenticated=True, content_type=None,
                         _auth_retry=True, _hw_retry=True,
                         _waf_rounds=0):
            calls.append({"url": url, "method": method, "data": data})
            return queue.pop(0)

        client._request = fake_request
        return client, calls

    def test_add_to_collection_sends_tag_add_patch(self):
        """Die Zuordnung wird mit der aktuellen Revision geschrieben.

        Ohne Revision könnte der PATCH den vorhandenen Stand ersetzen und
        damit fremde Marken (Lese-Status, andere Sammlungen) verlieren.
        """
        client, calls = self._patching_client([
            {"revision": 7, "patches": []},   # Revision lesen
            {"revision": 8},                  # add-Patch
        ])
        revision = client.add_to_collection("book-1", "SciFi")
        self.assertEqual(8, revision)
        self.assertEqual(2, len(calls))
        self.assertEqual(7, calls[1]["data"]["revision"])
        self.assertEqual("PATCH", calls[1]["method"])
        self.assertIn("sync-data", calls[1]["url"])
        patch = calls[1]["data"]["patches"][0]
        self.assertEqual("add", patch["op"])
        self.assertEqual("/publications/book-1/tags", patch["path"])
        self.assertEqual("SciFi", patch["value"]["name"])
        self.assertEqual("collection", patch["value"]["category"])

    def test_add_to_collection_reuses_a_given_revision(self):
        """Mehrere Zuordnungen brauchen nur ein Lesen."""
        client, calls = self._patching_client([{"revision": 5}])
        revision = client.add_to_collection("book-1", "SciFi", revision=4)
        self.assertEqual(5, revision)
        self.assertEqual(1, len(calls))
        self.assertEqual(4, calls[0]["data"]["revision"])

    def test_remove_from_collection_requires_existing_tag(self):
        from .tolino import TolinoApiError
        existing = {
            "op": "add",
            "path": "/publications/book-1/tags/6438286",
            "value": {"name": "SciFi", "category": "collection",
                      "revision": 3, "modified": 1},
        }
        client, calls = self._patching_client([
            {"revision": 7, "patches": [existing]},   # get_sync_data
            {"revision": 8},                          # remove patch
        ])
        revision = client.remove_from_collection("book-1", "SciFi")
        self.assertEqual(8, revision)
        patch = calls[1]["data"]["patches"][0]
        self.assertEqual("remove", patch["op"])
        self.assertEqual(3, patch["value"]["revision"])
        # Der Pfad muss die Marken-ID tragen -- sonst antwortet der Server
        # mit HTTP 400 „missing mandatory unitid fragment“ (0.15.3).
        self.assertEqual("/publications/book-1/tags/6438286", patch["path"])

        client2, _ = self._patching_client([
            {"revision": 7, "patches": []}])
        with self.assertRaises(TolinoApiError):
            client2.remove_from_collection("book-1", "SciFi")

    def test_mark_read_uses_system_tag(self):
        """Ein Buch: eine System-Marke, geschrieben mit der aktuellen
        Revision – Sammlungen und andere Marken bleiben unberührt."""
        client, calls = self._patching_client([
            {"revision": 9, "patches": []},
            {"revision": 10},
        ])
        revision = client.mark_read("book-2", finished=True)
        self.assertEqual(10, revision)
        self.assertEqual(9, calls[1]["data"]["revision"])
        patch = calls[1]["data"]["patches"][0]
        self.assertEqual("system", patch["value"]["category"])
        self.assertEqual("collection_finished_readings_name",
                         patch["value"]["name"])

    def test_mark_read_many_writes_one_patch_for_all_books(self):
        """Mehrere Bücher: ein Lesevorgang, ein PATCH, und schon markierte
        Bücher werden nicht erneut geschrieben."""
        already = {"op": "add", "path": "/publications/book-1/tags/6438286",
                   "value": {"name": "collection_finished_readings_name",
                             "category": "system", "revision": 3,
                             "modified": 1}}
        client, calls = self._patching_client([
            {"revision": 12, "patches": [already]},
            {"revision": 13},
        ])
        revision = client.mark_read_many(["book-1", "book-2"])
        self.assertEqual(13, revision)
        self.assertEqual(2, len(calls))
        self.assertEqual(12, calls[1]["data"]["revision"])
        self.assertEqual(["/publications/book-2/tags"],
                         [item["path"] for item in calls[1]["data"]["patches"]])

    def test_mark_read_many_clears_only_existing_marks(self):
        """Ungelesen setzen: der Pfad trägt die Marken-ID der Cloud-Marke.

        Feldbefund 0.15.3: ohne ID antwortet der Server mit HTTP 400
        «missing mandatory unitid fragment inside property path for remove».
        """
        marked = {"op": "add", "path": "/publications/book-1/tags/6438286",
                  "value": {"name": "collection_finished_readings_name",
                            "category": "system", "revision": 3,
                            "modified": 1}}
        client, calls = self._patching_client([
            {"revision": 12, "patches": [marked]},
            {"revision": 13},
        ])
        client.mark_read_many(["book-1", "book-2"], finished=False)
        patches = calls[1]["data"]["patches"]
        self.assertEqual(["remove"], [item["op"] for item in patches])
        self.assertEqual(3, patches[0]["value"]["revision"])
        self.assertEqual("/publications/book-1/tags/6438286",
                         patches[0]["path"])
        self.assertEqual(12, calls[1]["data"]["revision"])

    def test_mark_read_many_mixes_add_and_remove_in_one_patch(self):
        """Zwei Bücher, zwei Richtungen: «gelesen» ohne ID (der Server
        vergibt sie), «ungelesen» mit der ID der vorhandenen Marke."""
        marked = {"op": "add", "path": "/publications/book-1/tags/6438286",
                  "value": {"name": "collection_finished_readings_name",
                            "category": "system", "revision": 3,
                            "modified": 1}}
        client, calls = self._patching_client([
            {"revision": 12, "patches": [marked]},   # lesen
            {"revision": 13},                        # entfernen
            {"revision": 14, "patches": [marked]},   # wieder lesen
            {"revision": 15},                        # hinzufügen
        ])
        client.mark_read_many(["book-1"], finished=False)
        client.mark_read_many(["book-3"], finished=True)
        removed = calls[1]["data"]["patches"]
        added = calls[3]["data"]["patches"]
        self.assertEqual(["/publications/book-1/tags/6438286"],
                         [item["path"] for item in removed])
        self.assertEqual(["/publications/book-3/tags"],
                         [item["path"] for item in added])

    def test_removing_a_mark_without_an_id_fails_loudly(self):
        """Liefert der Server keine Marken-ID, gibt es kein Raten: der
        Fehler kommt sofort und im Klartext, nicht als HTTP 400."""
        from .tolino import TolinoApiError
        marked = {"op": "add", "path": "/publications/book-1/tags",
                  "value": {"name": "collection_finished_readings_name",
                            "category": "system", "revision": 3,
                            "modified": 1}}
        client, calls = self._patching_client([
            {"revision": 12, "patches": [marked]},
            {"revision": 13},
        ])
        with self.assertRaises(TolinoApiError) as caught:
            client.mark_read_many(["book-1"], finished=False)
        self.assertIn("Marken-ID", str(caught.exception))
        self.assertEqual(1, len(calls))   # kein Schreibversuch

    def test_mark_read_many_requires_ids(self):
        from .tolino import TolinoApiError
        client, calls = self._patching_client([])
        with self.assertRaises(TolinoApiError):
            client.mark_read_many([])
        self.assertEqual([], calls)

    def test_update_metadata_merges_and_puts_upload_metadata(self):
        existing = {"metadata": {"deliverableId": "b-1", "identifier": "b-1",
                                 "title": "Alt"}}
        client, calls = self._patching_client([existing, {"ok": True}])
        result = client.update_metadata("b-1", {"title": "Neu", "isbn": "123"})
        self.assertEqual("Neu", result["title"])
        self.assertEqual("123", result["isbn"])
        self.assertEqual("b-1", result["deliverableId"])
        self.assertEqual("GET", calls[0]["method"])
        self.assertIn("/meta", calls[0]["url"])
        self.assertIn("deliverableId=b-1", calls[0]["url"])
        self.assertEqual("PUT", calls[1]["method"])
        payload = calls[1]["data"]
        self.assertEqual("Neu", payload["uploadMetaData"]["title"])
        self.assertEqual("b-1", payload["uploadMetaData"]["deliverableId"])

    def test_update_metadata_requires_fields_and_metadata_response(self):
        from .tolino import TolinoApiError
        client, _ = self._patching_client([])
        with self.assertRaises(TolinoApiError):
            client.update_metadata("b-1", {})
        client2, _ = self._patching_client([{"unexpected": {}}])
        with self.assertRaises(TolinoApiError):
            client2.update_metadata("b-1", {"title": "x"})

    def test_update_metadata_leaves_tags_and_read_state_alone(self):
        """Titel/Autor/ISBN wandern über /meta – Sammlungen und die
        Lese-Marke in der Cloud werden dabei nicht angefasst."""
        existing = {"metadata": {
            "deliverableId": "b-1", "title": "Alt",
            "tags": [{"name": "Adamsberg", "category": "collection"},
                     {"name": "collection_finished_readings_name",
                      "category": "system"}]}}
        client, calls = self._patching_client([existing, {"ok": True}])
        client.update_metadata("b-1", {"title": "Neu"})
        self.assertEqual(["GET", "PUT"], [call["method"] for call in calls])
        self.assertEqual([], [call for call in calls
                              if "sync-data" in call["url"]])
        sent = calls[1]["data"]["uploadMetaData"]
        self.assertEqual("Neu", sent["title"])
        # Die Marken aus der Cloud-Antwort bleiben unverändert erhalten.
        self.assertEqual(
            [{"name": "Adamsberg", "category": "collection"},
             {"name": "collection_finished_readings_name", "category": "system"}],
            sent["tags"])

    def test_read_ids_reads_the_system_tag(self):
        """Gelesen-Marken des Kontos – nur die System-Marke zählt, und eine
        remove-Marke nimmt das Buch wieder heraus (0.15.0)."""
        patches = [
            {"op": "add", "path": "/publications/b-1/tags",
             "value": {"name": "collection_finished_readings_name",
                       "category": "system"}},
            {"op": "add", "path": "/publications/b-2/tags",
             "value": {"name": "Adamsberg", "category": "collection"}},
            {"op": "add", "path": "/publications/b-3/tags",
             "value": {"name": "collection_finished_readings_name",
                       "category": "system"}},
            {"op": "remove", "path": "/publications/b-3/tags",
             "value": {"name": "collection_finished_readings_name",
                       "category": "system"}},
        ]
        client, calls = self._patching_client(
            [{"revision": 5, "patches": patches}])
        self.assertEqual({"b-1"}, client.read_ids())
        self.assertEqual(1, len(calls))

    def test_snapshot_serves_collections_and_read_state_once(self):
        """Sammlungen und Lese-Status liegen in denselben Daten: EIN
        Lesevorgang reicht (Feldbefund: nicht bei jedem Sync alles neu)."""
        patches = [
            {"op": "add", "path": "/publications/b-1/tags",
             "value": {"name": "Adamsberg", "category": "collection"}},
            {"op": "add", "path": "/publications/b-1/tags",
             "value": {"name": "collection_finished_readings_name",
                       "category": "system"}},
        ]
        client, calls = self._patching_client(
            [{"revision": 5, "patches": patches}])
        self.assertEqual({("b-1", "Adamsberg")}, client.collection_pairs())
        self.assertEqual({"b-1"}, client.read_ids())
        self.assertEqual(1, len(calls))

    def test_snapshot_is_dropped_after_a_write(self):
        """Nach einem Schreibvorgang wird neu gelesen – keine veraltete
        Revision, kein zweiter Schreibversuch auf altem Stand."""
        client, calls = self._patching_client([
            {"revision": 5, "patches": []},   # snapshot
            {"revision": 6},                  # add_to_collection
            {"revision": 6, "patches": []},   # neuer snapshot
        ])
        client.collection_pairs()
        client.add_to_collection("b-1", "SciFi")
        client.read_ids()
        self.assertEqual(3, len(calls))
        self.assertEqual("PATCH", calls[1]["method"])

    def test_mark_read_many_uses_the_snapshot(self):
        """Der Bulk-Aufruf liest den Stand einmal und schreibt einmal."""
        client, calls = self._patching_client([
            {"revision": 7, "patches": []},
            {"revision": 8},
        ])
        client.mark_read_many(["b-1", "b-2"])
        self.assertEqual(2, len(calls))

    def test_collection_pairs_reads_sync_data_patches(self):
        patches = [
            {"op": "add", "path": "/publications/b-1/tags",
             "value": {"name": "Adamsberg", "category": "collection"}},
            {"op": "add", "path": "/publications/b-2/tags",
             "value": {"name": "SciFi", "category": "collection"}},
            {"op": "remove", "path": "/publications/b-2/tags",
             "value": {"name": "SciFi", "category": "collection"}},
            {"op": "add", "path": "/publications/b-1/tags",
             "value": {"name": "read", "category": "system"}},
            {"op": "add", "path": "/somewhere/else",
             "value": {"name": "X", "category": "collection"}},
        ]
        client, calls = self._patching_client(
            [{"revision": 5, "patches": patches}])
        self.assertEqual({("b-1", "Adamsberg")}, client.collection_pairs())
        self.assertEqual("PATCH", calls[0]["method"])
        self.assertIn("sync-data", calls[0]["url"])

    def test_download_resolves_content_url_and_returns_metadata(self):
        client, calls = self._patching_client([
            {"DownloadInfo": {"contentUrl": "https://cdn.example/epub"}},
            {"PublicationInventory": {"edata": [],
                                      "ebook": [{"deliverableId": "b-9",
                                                 "epubMetaData": {"title": "T"}}]}},
        ])
        content = b"EPUB-BYTES"

        class RawResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return content

        with patch("calibre_plugin.tolino._curl_binary", return_value=None), \
                patch("calibre_plugin.tolino._impersonate_session", return_value=None), \
                        patch("calibre_plugin.tolino.urlopen",
                   return_value=RawResponse()):
            data, metadata = client.download("b-9")
        self.assertEqual(content, data)
        self.assertEqual("T", metadata["epubMetaData"]["title"])
        self.assertIn("downloadinfo", calls[0]["url"])


class CurlTransportTests(unittest.TestCase):
    """The token POST may be routed through curl to dodge bot protection."""

    def _client(self):
        client = TolinoClient(4, "hw-curl", refresh="r-1")
        client.hardware = "hw-curl"
        return client

    def test_token_post_prefers_curl_transport(self):
        import calibre_plugin.tolino as tolino_module

        client = self._client()
        calls = []

        def fake_curl(url, body, headers, timeout):
            calls.append({"url": url, "body": body, "headers": dict(headers)})
            return 200, json.dumps({
                "access_token": "a1", "refresh_token": "r2",
                "refresh_expires_in": 36000, "expires_in": 3600,
            }).encode("utf-8")

        with patch.object(tolino_module, "_curl_binary", return_value="/usr/bin/curl"), \
                patch.object(tolino_module, "_impersonate_session", return_value=None), \
                patch.object(tolino_module, "_http_post_via_curl", fake_curl):
            client.login()

        self.assertEqual(PARTNERS[4]["token_url"], calls[0]["url"])
        self.assertEqual("refresh_token", dict(urllib.parse.parse_qsl(
            calls[0]["body"].decode()))["grant_type"])
        self.assertEqual("r-1", dict(urllib.parse.parse_qsl(
            calls[0]["body"].decode()))["refresh_token"])
        self.assertEqual("SCOPE_BOSH", dict(urllib.parse.parse_qsl(
            calls[0]["body"].decode()))["scope"])
        self.assertEqual("a1", client.access)
        self.assertEqual("r2", client.refresh)
        # Origin/Referer token headers from the partner config are forwarded.
        self.assertEqual("https://webreader.mytolino.com",
                         calls[0]["headers"].get("Origin"))
        self.assertEqual("https://webreader.mytolino.com/",
                         calls[0]["headers"].get("Referer"))

    def test_token_post_falls_back_to_urllib_without_curl(self):
        import calibre_plugin.tolino as tolino_module

        client = self._client()
        requests_seen = []

        class FakeResponse:
            status = 200

            def read(self):
                return json.dumps({
                    "access_token": "a2", "refresh_token": "r3",
                }).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def fake_urlopen(request, timeout=None):
            requests_seen.append(request)
            return FakeResponse()

        with patch.object(tolino_module, "_curl_binary", return_value=None), \
                patch.object(tolino_module, "_impersonate_session", return_value=None), \
                patch.object(tolino_module, "urlopen", fake_urlopen):
            client.login()

        self.assertEqual(1, len(requests_seen))
        self.assertEqual("a2", client.access)
        self.assertEqual("r3", client.refresh)

    def test_token_post_sends_browser_sec_fetch_header_family(self):
        import calibre_plugin.tolino as tolino_module

        client = self._client()
        captured = {}

        def fake_curl(url, body, headers, timeout):
            captured["headers"] = dict(headers)
            return 200, json.dumps({
                "access_token": "a7", "refresh_token": "r7",
            }).encode("utf-8")

        with patch.object(tolino_module, "_impersonate_session", return_value=None), \
                patch.object(tolino_module, "_curl_binary", return_value="/usr/bin/curl"), \
                patch.object(tolino_module, "_http_post_via_curl", fake_curl):
            client.login()

        headers = captured["headers"]
        # The web reader's own token POST carries this family; the WAF
        # answers requests without it with the "Zugriff geblockt" page.
        self.assertEqual("empty", headers.get("Sec-Fetch-Dest"))
        self.assertEqual("cors", headers.get("Sec-Fetch-Mode"))
        self.assertEqual("cross-site", headers.get("Sec-Fetch-Site"))
        self.assertIn("Chromium", headers.get("Sec-CH-UA", ""))
        self.assertEqual("?0", headers.get("Sec-CH-UA-Mobile"))
        self.assertEqual("*/*", headers.get("Accept"))
        # Partner Origin/Referer must survive the merge.
        self.assertEqual("https://webreader.mytolino.com",
                         headers.get("Origin"))
        self.assertEqual("https://webreader.mytolino.com/",
                         headers.get("Referer"))

    def test_auth_diagnostics_reports_curl_cffi_availability(self):
        import calibre_plugin.tolino as tolino_module

        client = TolinoClient(4, "", "old-refresh")
        with patch.object(tolino_module, "_impersonate_session", return_value=None):
            self.assertFalse(client.auth_diagnostics()["curl_cffi"])
        with patch.object(tolino_module, "_impersonate_session",
                          return_value=object()):
            self.assertTrue(client.auth_diagnostics()["curl_cffi"])

    def test_compact_error_text_strips_html_bot_check_page(self):
        import calibre_plugin.tolino as tolino_module

        page = "<!DOCTYPE html><html><head><title>Zugriff geblockt</title></head>" \
               "<body>layout-fehlerseite  Zugang verweigert</body></html>"
        self.assertEqual(
            "Zugriff geblockt layout-fehlerseite Zugang verweigert",
            tolino_module._compact_error_text(page))

    def test_http_post_via_curl_parses_status_and_body(self):
        import calibre_plugin.tolino as tolino_module

        class Completed:
            returncode = 0
            stdout = b'{"ok": true}\n200'
            stderr = b""

        with patch.object(tolino_module.subprocess, "run", return_value=Completed()):
            status, raw = tolino_module._http_post_via_curl(
                "https://example.test/token", "grant_type=x", {}, 5)

        self.assertEqual(200, status)
        self.assertEqual(b'{"ok": true}', raw)

    def test_token_post_prefers_curl_cffi_impersonation(self):
        import calibre_plugin.tolino as tolino_module

        client = self._client()
        calls = []

        class FakeSession:
            def post(self, url, data=None, headers=None, timeout=None,
                     allow_redirects=None):
                calls.append({"url": url, "data": data, "headers": dict(headers)})
                response = type("R", (), {})()
                response.status_code = 200
                response.content = json.dumps({
                    "access_token": "a4", "refresh_token": "r5",
                }).encode("utf-8")
                return response

        def must_not_run(*args, **kwargs):
            raise AssertionError("plain curl must not run when curl_cffi exists")

        with patch.object(tolino_module, "_impersonate_session",
                          return_value=FakeSession()), \
                patch.object(tolino_module, "_curl_binary", return_value="/usr/bin/curl"), \
                patch.object(tolino_module, "_http_post_via_curl", must_not_run):
            client.login()

        self.assertEqual("curl_cffi", client.last_transport)
        self.assertEqual("a4", client.access)
        self.assertEqual("r5", client.refresh)
        sent = dict(urllib.parse.parse_qsl(calls[0]["data"]))
        self.assertEqual("refresh_token", sent["grant_type"])
        self.assertEqual("r-1", sent["refresh_token"])

    def test_curl_cffi_failure_falls_back_to_curl_binary(self):
        import calibre_plugin.tolino as tolino_module

        client = self._client()

        class BrokenSession:
            def post(self, *_args, **_kwargs):
                raise RuntimeError("curl_cffi exploded")

        curl_calls = []

        def fake_curl(url, body, headers, timeout):
            curl_calls.append(url)
            return 200, json.dumps({
                "access_token": "a6", "refresh_token": "r6",
            }).encode("utf-8")

        with patch.object(tolino_module, "_impersonate_session",
                          return_value=BrokenSession()), \
                patch.object(tolino_module, "_curl_binary", return_value="/usr/bin/curl"), \
                patch.object(tolino_module, "_http_post_via_curl", fake_curl):
            client.login()

        self.assertEqual("curl", client.last_transport)
        self.assertEqual(1, len(curl_calls))
        self.assertEqual("a6", client.access)

    def test_token_403_bot_page_falls_through_to_urllib(self):
        """Feldbefund 0.9.38: curl_cffi UND System-curl werden von der
        Bot-Schutz-Laufzeitbahn mit 403 geblockt – dann entscheidet
        urllib (Browser-Header), statt dass der Aufruch abbricht."""
        import calibre_plugin.tolino as tolino_module

        bot = b"<!doctype html><title>Zugriff geblockt</title>"
        transports = []

        def fake_cffi(*_args, **_kwargs):
            transports.append("curl_cffi")
            return 403, bot

        def fake_curl(url, body, headers, timeout):
            transports.append("curl")
            return 403, bot

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({
                    "access_token": "a9", "refresh_token": "r9",
                    "expires_in": 3600, "refresh_expires_in": 3598,
                }).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            transports.append("urllib")
            return Response()

        client = self._client()
        with patch.object(tolino_module, "_impersonate_session",
                          return_value=object()), \
                patch.object(tolino_module, "_http_post_via_curl_cffi",
                             fake_cffi), \
                patch.object(tolino_module, "_curl_binary",
                             return_value="/usr/bin/curl"), \
                patch.object(tolino_module, "_http_post_via_curl",
                             fake_curl), \
                patch.object(tolino_module, "urlopen", fake_urlopen):
            client.login()
        self.assertEqual(["curl_cffi", "curl", "urllib"], transports)
        self.assertEqual("urllib", client.last_transport)
        self.assertEqual("a9", client.access)
        self.assertEqual("r9", client.refresh)

    def test_token_oauth_error_never_falls_through(self):
        """invalid_grant (400) darf nie den naechsten Transport probieren
        – ein replizierter Grant koennte Keycloak's Reuse-Schutz
        ausloesen. Sofortiger Abbruch wie gehabt."""
        import calibre_plugin.tolino as tolino_module

        def must_not_run(*_args, **_kwargs):
            raise AssertionError("replay not allowed")

        def fake_cffi(*_args, **_kwargs):
            return 400, (b'{"error":"invalid_grant",'
                         b'"error_description":"Token expired"}')

        with patch.object(tolino_module, "_impersonate_session",
                          return_value=object()), \
                patch.object(tolino_module, "_http_post_via_curl_cffi",
                             fake_cffi), \
                patch.object(tolino_module, "_curl_binary",
                             return_value="/usr/bin/curl"), \
                patch.object(tolino_module, "_http_post_via_curl",
                             must_not_run), \
                patch.object(tolino_module, "urlopen", must_not_run):
            with self.assertRaisesRegex(TolinoAuthError,
                                        "Zugang abgelehnt"):
                self._client().login()

    def test_http_post_via_curl_raises_without_binary(self):
        import calibre_plugin.tolino as tolino_module

        with patch.object(tolino_module, "_curl_binary", return_value=None):
            with self.assertRaises(RuntimeError):
                tolino_module._http_post_via_curl(
                    "https://example.test/token", "grant_type=x", {}, 5)


class LoginWindowWaitTests(unittest.TestCase):
    """0.9.32: die Wartezeit nach einer Ablehnung unterscheidet, OB das
    Anmeldefenster noch lebt und WAS es zeigt.
    """

    def _refresh_jwt(self, iat):
        head = base64.urlsafe_b64encode(b'{"alg":"HS512","typ":"JWT"}')
        body = base64.urlsafe_b64encode(
            json.dumps({"iat": int(iat), "typ": "Refresh",
                        "sid": "sess-wait"}).encode("utf-8"))
        return "%s.%s.sig" % (head.decode().rstrip("="),
                              body.decode().rstrip("="))

    def test_describe_grab_state_separates_login_page_from_vanished(self):
        """Verschwundenes Fenster und offene Anmeldeseite sind
        verschiedene Zustände mit unterschiedlicher Handlungsanweisung."""
        from . import cdp
        with patch.object(cdp, "_http_get_json", return_value=None):
            self.assertEqual("kein Web-Reader-Tab im Anmeldefenster offen",
                             cdp.describe_grab_state())
        login_targets = [{
            "type": "page",
            "url": "https://shop.example.tld/realms/x/protocol/openid-"
                   "connect/auth?client_id=reader",
        }]
        with patch.object(cdp, "_http_get_json", return_value=login_targets):
            state = cdp.describe_grab_state()
        self.assertIn("Anmeldefenster offen", state)
        self.assertIn("Anmeldeseite", state)

    def test_describe_grab_state_reports_counts_with_reader_tab(self):
        """Mit Reader-Tab bleibt die Zähl-Ausgabe (Kandidaten + IDB)."""
        from . import cdp
        reader_targets = [{
            "type": "page",
            "url": "https://webreader.mytolino.com/library/index.html",
        }]
        with patch.object(cdp, "_http_get_json",
                          return_value=reader_targets), \
             patch.object(cdp, "grab_once_from_grabber",
                          return_value={"refresh": ["tok-current"],
                                        "hardware": [],
                                        "idb": ["tolino-user"]}):
            state = cdp.describe_grab_state()
        self.assertIn("Zugänge: 1", state)
        self.assertIn("tolino-user", state)

    def test_grab_live_refresh_keeps_login_page_window_open(self):
        """Fenster offen, Tab auf der Anmeldeseite: NICHT schliessen,
        zur Neu-Anmeldung vor Ort auffordern und LÄNGER warten."""
        from .tolino import grab_live_refresh
        fresh = self._refresh_jwt(time.time() - 5)
        grabs = []

        class FailingClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = hw

            def _login(self):
                raise TolinoAuthError("Tolino HTTP 400: invalid_grant")

        login_state = {"window": True, "reader_tab": False,
                       "login_page": True}
        with patch("calibre_plugin.tolino.TolinoClient", FailingClient), \
             patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [fresh], "hardware": []}), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   side_effect=lambda *a, **k: grabs.append(1) or None), \
             patch("calibre_plugin.cdp.grabber_window_state",
                   return_value=login_state), \
             patch("calibre_plugin.cdp.close_grabber_window") as closer, \
             patch("calibre_plugin.tolino._NEW_TOKEN_ATTEMPTS", 2), \
             patch("calibre_plugin.tolino._NEW_TOKEN_INTERVAL", 0):
            with self.assertRaises(TolinoAuthError) as ctx:
                grab_live_refresh(4, "cfg-hw", timeout=1)
        closer.assert_not_called()
        message = str(ctx.exception)
        self.assertIn("ist offen", message)
        self.assertIn("dort anmelden", message)
        self.assertNotIn("wurde geschlossen", message)
        # Auf der Anmeldeseite wird länger gewartet (zusätzliche
        # Versuche), damit die Neu-Anmeldung vor Ort noch ankommen kann.
        self.assertGreater(len(grabs), 2)

    def test_grab_live_refresh_stops_early_when_window_vanished(self):
        """Endpunkt stumm: nach drei Fehlschlägen abbrechen statt 30
        Wartegängen – und wie bisher schliessen + Neustart-Anweisung."""
        from .tolino import grab_live_refresh
        stale = self._refresh_jwt(time.time() - 7200)
        grabs = []

        def no_tab(*args, **kwargs):
            grabs.append(1)
            return None

        dead = {"window": False, "reader_tab": False, "login_page": False}
        with patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [stale], "hardware": []}), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   side_effect=no_tab), \
             patch("calibre_plugin.cdp.grabber_window_state",
                   return_value=dead), \
             patch("calibre_plugin.tolino._NEW_TOKEN_ATTEMPTS", 30), \
             patch("calibre_plugin.tolino._NEW_TOKEN_INTERVAL", 0):
            with self.assertRaises(TolinoAuthError) as ctx:
                grab_live_refresh(4, "cfg-hw", timeout=1)
        self.assertLessEqual(len(grabs), 4)
        self.assertIn("wurde geschlossen", str(ctx.exception))

    def test_candidate_age_note_describes_undatable_candidate(self):
        """Undatierte Kandidaten werden strukturell beschrieben (Teile,
        Zeichen, Payload) – der Wert selbst nie."""
        from .tolino import _candidate_age_note
        opaque = "opaque-refresh-token-value-without-any-dots"
        note = _candidate_age_note([opaque])
        self.assertIn("Alter des gefundenen Zugangs unbekannt", note)
        self.assertIn("1 Teil", note)
        self.assertIn("Zeichen", note)
        broken = "aaa.bbb.ccc"
        note = _candidate_age_note([broken])
        self.assertIn("3 Teile", note)
        self.assertIn("unlesbar", note)
        self.assertNotIn(opaque, note)
        self.assertNotIn(broken, note)
        # Datierbare Kandidaten bleiben unverändert.
        jwt = self._refresh_jwt(time.time() - 7)
        self.assertIn("Sekunden alt", _candidate_age_note([jwt]))


class WrappedCandidateTests(unittest.TestCase):
    """0.9.33: Der Seiten-Speicher des Readers kann Token-Teile gekodiert
    ablegen – Feldbefund der Extrahier-Schaltfläche: genau EIN
    Refresh-Kandidat, "1 Teil, 920 Zeichen", am Endpunkt abgelehnt
    (invalid_grant) und ohne datierbares JWT-alter. Ein JWT traegt immer
    zwei Punkte; die Hülle muss vor dem Tausch abgezogen werden.
    """

    def _refresh_jwt(self, iat):
        head = base64.urlsafe_b64encode(b'{"alg":"HS512","typ":"JWT"}')
        body = base64.urlsafe_b64encode(
            json.dumps({"iat": int(iat), "typ": "Refresh",
                        "sid": "sess-wrap"}).encode("utf-8"))
        return "%s.%s.sig" % (head.decode().rstrip("="),
                              body.decode().rstrip("="))

    def test_unwrap_recovers_base64_wrapped_jwt(self):
        """base64- und doppelt gehüllte JWTs werden bis zum Kern
        entpackt (Feldform: 920 Zeichen ohne Punkt)."""
        from .tolino import _unwrap_refresh_candidate
        fresh = self._refresh_jwt(time.time() - 5)
        wrapped = base64.urlsafe_b64encode(
            fresh.encode("utf-8")).decode().rstrip("=")
        self.assertEqual(fresh, _unwrap_refresh_candidate(wrapped))
        double = base64.b64encode(wrapped.encode("utf-8")).decode()
        self.assertEqual(fresh, _unwrap_refresh_candidate(double))

    def test_unwrap_recovers_quoted_and_percent_encoded_jwt(self):
        """JSON-Zeichenkette und Prozent-Code (inkl. kodierter Punkte)
        sind ebenfalls bekannte Hüllen."""
        from .tolino import _unwrap_refresh_candidate
        fresh = self._refresh_jwt(time.time() - 5)
        self.assertEqual(fresh, _unwrap_refresh_candidate(json.dumps(fresh)))
        percent = fresh.replace(".", "%2E")
        self.assertEqual(fresh, _unwrap_refresh_candidate(percent))

    def test_unwrap_leaves_opaque_and_broken_values_untouched(self):
        """Opake (echt punktlose) Tokens und unlesbare Zeichenketten
        bleiben unverändert – nur ein JWT-Treffer gewinnt."""
        from .tolino import _unwrap_refresh_candidate
        opaque = "a3f9" * 230  # 920 Zeichen wie im Feldbefund
        self.assertEqual(opaque, _unwrap_refresh_candidate(opaque))
        broken = "aaa.bbb.ccc"
        self.assertEqual(broken, _unwrap_refresh_candidate(broken))
        short = "plainopaquevalue-not-a-jwt"
        self.assertEqual(short, _unwrap_refresh_candidate(short))

    def test_normalize_makes_wrapped_stale_copy_fail_age_filter(self):
        """Gekodete alte Kopien passieren den 30-Minuten-Filter NICHT
        mehr: erst nach dem Entpacken sind sie datierbar und fallen
        unter den Cutoff statt am Endpunkt verbrannt zu werden."""
        from .tolino import _filter_live_candidates, _normalize_live_grab
        stale = self._refresh_jwt(time.time() - 7200)
        wrapped = base64.urlsafe_b64encode(stale.encode("utf-8")).decode()
        normalized = _normalize_live_grab({"refresh": [wrapped],
                                           "hardware": []})
        self.assertEqual([stale], normalized["refresh"])
        self.assertEqual([], _filter_live_candidates(normalized["refresh"]))
        # Unveränderte Grabs werden nicht umsonst kopiert.
        plain = {"refresh": [stale], "hardware": []}
        self.assertIs(plain, _normalize_live_grab(plain))

    def test_grab_live_refresh_exchanges_the_unwrapped_jwt(self):
        """Der Endpunkt sieht das entpackte JWT – nie die Hülle."""
        from .tolino import grab_live_refresh
        fresh = self._refresh_jwt(time.time() - 5)
        wrapped = base64.urlsafe_b64encode(
            fresh.encode("utf-8")).decode().rstrip("=")
        logins = []

        class FakeClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = "fake-hw"

            def _login(self):
                logins.append(self.refresh)
                self.refresh = "rotated-token"

        with patch("calibre_plugin.tolino.TolinoClient", FakeClient), \
             patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [wrapped], "hardware": []}), \
             patch("calibre_plugin.cdp.reader_ws_url", return_value=None):
            refresh, hardware = grab_live_refresh(4, "cfg-hw", timeout=1)
        self.assertEqual(("rotated-token", "fake-hw"),
                         (refresh, hardware))
        self.assertEqual([fresh], logins)

    def test_try_live_grab_first_dates_the_unwrapped_candidate(self):
        """Feldmeldung nach dem Fix: das ALTER des Kandidaten statt der
        Struktur-Notiz – und nie der Wert selbst."""
        from .tolino import try_live_grab_first
        fresh = self._refresh_jwt(time.time() - 5)
        wrapped = base64.urlsafe_b64encode(
            fresh.encode("utf-8")).decode().rstrip("=")

        class FailingClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = hw

            def _login(self):
                raise TolinoAuthError("Tolino HTTP 400: invalid_grant")

        with patch("calibre_plugin.cdp.devtools_port_alive",
                   return_value=True), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   return_value={"refresh": [wrapped], "hardware": [],
                                 "idb": []}), \
             patch("calibre_plugin.cdp.describe_grab_state",
                   return_value="1 Refresh-Kandidat(en)"), \
             patch("calibre_plugin.cdp.reader_ws_url", return_value=None), \
             patch("calibre_plugin.tolino.TolinoClient", FailingClient):
            with self.assertRaises(TolinoAuthError) as ctx:
                try_live_grab_first(4, "hw")
        message = _full(ctx.exception)
        self.assertIn("Letzter Fehler", message)
        self.assertIn("Sekunden alt", message)
        self.assertNotIn("Alter des gefundenen Zugangs unbekannt", message)
        self.assertNotIn(wrapped, message)
        self.assertNotIn(fresh, message)


class ReaderBlobLiveGrabTests(unittest.TestCase):
    """0.9.34: Der Live-Grab entpackt die CryptoJS-verschluesselten
    userToken/userInfos-Bloebe des Readers.
    """

    def _refresh_jwt(self, iat):
        head = base64.urlsafe_b64encode(b'{"alg":"HS512","typ":"JWT"}')
        body = base64.urlsafe_b64encode(
            json.dumps({"iat": int(iat), "typ": "Refresh",
                        "sid": "sess-blob"}).encode("utf-8"))
        return "%s.%s.sig" % (head.decode().rstrip("="),
                              body.decode().rstrip("="))

    def _encrypt(self, plain, phrase=""):
        """Unabhaengige Referenz-Implementation des CryptoJS-Blobs."""
        import hashlib
        try:
            from Crypto.Cipher import AES
        except ImportError:
            self.skipTest("pycryptodome missing: no reference AES available")
        salt = os.urandom(8)
        derived = b""
        prev = b""
        while len(derived) < 48:
            prev = hashlib.md5(prev + phrase.encode() + salt).digest()
            derived += prev
        key, iv = derived[:32], derived[32:]
        data = plain.encode()
        pad = 16 - len(data) % 16
        data += bytes([pad]) * pad
        ct = AES.new(key, AES.MODE_CBC, iv).encrypt(data)
        return base64.b64encode(b"Salted__" + salt + ct).decode()

    def test_unwrap_decrypts_reader_user_token_blob(self):
        """Der verschluesselte Kandidat wird zum echten JWT – und ist
        damit datierbar (Altersnotiz statt Struktur-Hinweis)."""
        from .tolino import (_candidate_age_note, _filter_live_candidates,
                             _normalize_live_grab,
                             _unwrap_refresh_candidate)
        fresh = self._refresh_jwt(time.time() - 5)
        blob = self._encrypt(fresh)
        self.assertTrue(blob.startswith("U2FsdGVk"))
        self.assertEqual(fresh, _unwrap_refresh_candidate(blob))
        normalized = _normalize_live_grab({"refresh": [blob],
                                           "hardware": []})
        self.assertEqual([fresh], normalized["refresh"])
        self.assertEqual([fresh],
                         _filter_live_candidates(normalized["refresh"]))
        self.assertIn("Sekunden alt", _candidate_age_note([fresh]))

    def test_unwrap_decrypts_nested_user_token_bundle(self):
        """Doppelte Schicht: Blob -> {"refresh": Blob} -> JWT."""
        from .tolino import _unwrap_refresh_candidate
        fresh = self._refresh_jwt(time.time() - 5)
        outer = self._encrypt(json.dumps(
            {"refresh": self._encrypt(fresh), "expireTime": 1}))
        self.assertEqual(fresh, _unwrap_refresh_candidate(outer))

    def test_normalize_extracts_hardware_from_user_infos_blob(self):
        """Die im curl-Export gezeigte Hardware-Id steckt im
        verschluesselten userInfos – extrahiert, und niemals als
        Ciphertext weitergereicht."""
        from .tolino import _normalize_live_grab
        blob = self._encrypt(json.dumps({
            "userId": "54389718", "devKey": "dk",
            "hardwareId": "eb22e4cf-bb01-4550-bff7-334446bb20b1"}))
        normalized = _normalize_live_grab({"refresh": [],
                                           "hardware": [blob]})
        self.assertEqual(["eb22e4cf-bb01-4550-bff7-334446bb20b1"],
                         normalized["hardware"])
        # Undurchdringbarer Ciphertext verschwindet stillschweigend,
        # statt als Hardware-Id gespeichert oder getauscht zu werden.
        opaque = self._encrypt('{"keine": "hardware"}', phrase="fremd")
        normalized = _normalize_live_grab({"refresh": [],
                                           "hardware": [opaque]})
        self.assertEqual([], normalized["hardware"])
        # Klartext-Hardware-Ids bleiben wie sie sind.
        plain_hw = "a0b2c3d4-e5f6-7788-99aa-bbccddeeff00"
        holder = {"refresh": [], "hardware": [plain_hw]}
        self.assertIs(holder, _normalize_live_grab(holder))

    def test_grab_live_refresh_exchanges_the_decrypted_blob(self):
        """End-to-end: verschluesselter Kandidat -> Entschluesselung ->
        Tausch des echten JWT (FakeClient sieht den Klartext)."""
        from .tolino import grab_live_refresh
        fresh = self._refresh_jwt(time.time() - 5)
        blob = self._encrypt(fresh)
        logins = []

        class FakeClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = "fake-hw"

            def _login(self):
                logins.append(self.refresh)
                self.refresh = "rotated-token"

        with patch("calibre_plugin.tolino.TolinoClient", FakeClient), \
             patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [blob], "hardware": []}), \
             patch("calibre_plugin.cdp.reader_ws_url", return_value=None):
            refresh, hardware = grab_live_refresh(4, "cfg-hw", timeout=1)
        self.assertEqual(("rotated-token", "fake-hw"),
                         (refresh, hardware))
        self.assertEqual([fresh], logins)

    def test_try_live_grab_first_dates_the_decrypted_stale_blob(self):
        """Verschluesselt + zu alt: der Filter greift, die Meldung
        nennt das Alter – kein "ohne datierbares JWT-alter" mehr."""
        from .tolino import try_live_grab_first
        stale = self._refresh_jwt(time.time() - 7200)
        blob = self._encrypt(stale)
        with patch("calibre_plugin.cdp.devtools_port_alive",
                   return_value=True), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   return_value={"refresh": [blob], "hardware": [],
                                 "idb": []}), \
             patch("calibre_plugin.cdp.describe_grab_state",
                   return_value="1 Refresh-Kandidat(en)"):
            with self.assertRaises(TolinoAuthError) as ctx:
                try_live_grab_first(4, "hw")
        message = _full(ctx.exception)
        self.assertIn("Sekunden alt", message)
        self.assertNotIn("Alter des gefundenen Zugangs unbekannt", message)
        self.assertNotIn(blob, message)


class InventoryDialogSortTests(unittest.TestCase):
    """Spaltenkopf-Klick sortiert den Bestandsvergleich und zieht die
    Auswahl mit (Feldbefund 0.9.38: „das sortieren funktioniert nicht").
    Qt wird ueber den ToolbarIconTests-Stub geladen; die Zell-Widgets
    werden durch einfache Fakes ersetzt."""

    def test_column_click_sorts_and_keeps_checks(self):
        """Ein Klick auf einen Spaltenkopf sortiert die Tabelle neu; die
        Auswahl (Hochladen/Metadaten/Gelesen) bleibt denselben Büchern
        zugeordnet (Feldbefund 0.9.38: „das sortieren funktioniert nicht")."""

        class FakeCheck:
            def __init__(self):
                self._checked = False

            def setChecked(self, value):
                self._checked = bool(value)

            def isChecked(self):
                return self._checked

            def setEnabled(self, value):
                pass

            def setToolTip(self, text):
                pass

            def stateChanged(self):
                return None

        class FakeHeader:
            def setSortIndicatorShown(self, value):
                pass

            def setSortIndicator(self, column, order):
                pass

        class FakeTable:
            def __init__(self):
                self.row_count = -1

            def setRowCount(self, count):
                self.row_count = count

            def setCellWidget(self, row, column, widget):
                pass

            def setItem(self, row, column, item):
                pass

            def resizeColumnsToContents(self):
                pass

            def horizontalHeader(self):
                return FakeHeader()

        class FakeItem:
            def __init__(self, text):
                self.text = text

        ui_module = ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        ui_module.QCheckBox = FakeCheck
        ui_module.QTableWidgetItem = FakeItem
        try:
            dialog = ui_module.InventoryDialog.__new__(
                ui_module.InventoryDialog)
            rows = [
                {"upload": False, "metadata": False, "read": False,
                 "title": "Alpha", "authors": "A", "status": "identical",
                 "book_id": 1, "tolino_id": "c1",
                 "explanation": "Titel stimmt mit der Cloud überein."},
                {"upload": True, "metadata": False, "read": False,
                 "title": "Zulu", "authors": "Z", "status": "new_in_calibre",
                 "book_id": 2, "tolino_id": "",
                 "explanation": "Noch nicht in der Cloud – kann hochgeladen werden."},
                {"upload": False, "metadata": True, "read": True,
                 "title": "beta", "authors": "B", "status": "changed",
                 "book_id": 3, "tolino_id": "c3",
                 "explanation": "Nur Metadaten unterscheiden sich."},
                {"upload": True, "metadata": False, "read": False,
                 "title": "alpha", "authors": "M", "status": "new_in_calibre",
                 "book_id": 4, "tolino_id": "",
                 "explanation": "Noch nicht in der Cloud – kann hochgeladen werden."},
            ]
            dialog.rows = rows
            dialog.checks = [FakeCheck() for _ in rows]
            dialog.meta_checks = [FakeCheck() for _ in rows]
            dialog.read_checks = [FakeCheck() for _ in rows]
            for uploads, metadata, read, row in zip(dialog.checks,
                                                    dialog.meta_checks,
                                                    dialog.read_checks, rows):
                uploads.setChecked(row["upload"])
                metadata.setChecked(row["metadata"])
                read.setChecked(row["read"])
            dialog._sort_column = 0
            dialog._sort_descending = False
            dialog.table = FakeTable()

            # Klick auf den Titel (Spalte 5): aufsteigend, die Auswahl
            # bleibt denselben Büchern zugeordnet.
            dialog._sort_by_column(5)
            self.assertEqual(
                ["alpha", "alpha", "beta", "zulu"],
                [row["title"].casefold() for row in dialog.rows])
            upload_titles = {row["title"].casefold()
                             for row, check in zip(dialog.rows, dialog.checks)
                             if check.isChecked()}
            meta_titles = {row["title"].casefold()
                           for row, check in zip(dialog.rows, dialog.meta_checks)
                           if check.isChecked()}
            read_titles = {row["title"].casefold()
                           for row, check in zip(dialog.rows, dialog.read_checks)
                           if check.isChecked()}
            self.assertEqual({"alpha", "zulu"}, upload_titles)
            self.assertEqual({"beta"}, meta_titles)
            self.assertEqual({"beta"}, read_titles)
            # Zweiter Klick auf dieselbe Spalte: absteigend.
            dialog._sort_by_column(5)
            self.assertEqual(
                ["zulu", "beta", "alpha", "alpha"],
                [row["title"].casefold() for row in dialog.rows])
            self.assertEqual(
                {"alpha", "zulu"},
                {row["title"].casefold()
                 for row, check in zip(dialog.rows, dialog.checks)
                 if check.isChecked()})
            # Hochladen-Spalte (0): ausgewählte zuerst, danach Titel.
            dialog._sort_by_column(0)
            first_two = dialog.rows[:2]
            self.assertTrue(all(row["upload"] for row in first_two))
            self.assertEqual(["alpha", "zulu"],
                             [row["title"].casefold() for row in first_two])
            # Metadaten-Spalte (1): nur das Metadaten-Auswahlfeld steht oben.
            dialog._sort_by_column(1)
            self.assertEqual("beta", dialog.rows[0]["title"].casefold())
            # Gelesen-Spalte (2): nur die Lese-Marke steht oben.
            dialog._sort_by_column(2)
            self.assertEqual("beta", dialog.rows[0]["title"].casefold())
            self.assertTrue(dialog.rows[0]["read"])
            self.assertFalse(any(row["read"] for row in dialog.rows[1:]))
            # In Calibre (3): Bücher mit Calibre-Kopie zuerst -- hier haben
            # alle eine, also sortiert der Titel als Zweitschlüssel.
            dialog._sort_by_column(3)
            self.assertTrue(all(row["book_id"] is not None
                                for row in dialog.rows))
            self.assertEqual(
                ["alpha", "alpha", "beta", "zulu"],
                [row["title"].casefold() for row in dialog.rows])
            # In der Cloud (4): Cloud-Kopien zuerst, ohne Cloud-Kopie hinten.
            dialog._sort_by_column(4)
            self.assertEqual(
                ["c1", "c3", "", ""],
                [row["tolino_id"] for row in dialog.rows])
        finally:
            ui_module._restore_stubs_for_tests()

    def test_columns_sort_keys_and_technical_columns_stay_in_sync(self):
        """Spaltenzahl, Sortierschlüssel und technische Spalten müssen
        zusammenpassen – sonst sortiert ein Klick falsch. Die doppelte
        Statusspalte entfällt, die beiden Orts-Spalten stehen wieder
        einzeln da (0.15.3, Feldbefund)."""
        ui_module = ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        try:
            dialog_cls = ui_module.InventoryDialog
            # 8 sichtbare Spalten + 2 technische.
            self.assertEqual(10, len(dialog_cls.HEADERS))
            visible = [name for index, name in enumerate(dialog_cls.HEADERS)
                       if index not in dialog_cls.TECHNICAL_COLUMNS]
            self.assertEqual(8, len(visible))
            self.assertEqual(["Hochladen", "Metadaten", "Gelesen",
                              "In Calibre", "In der Cloud",
                              "Titel", "Autor(en)", "Hinweis"], visible)
            self.assertEqual(len(dialog_cls.HEADERS),
                             len(dialog_cls._SORT_KEYS))
            # Jede Spalte hat einen eigenen Sortierschlüssel: "Wo" gibt es
            # nicht mehr, die beiden Orts-Spalten sind getrennt sortierbar.
            self.assertNotIn("Wo", dialog_cls.HEADERS)
            for column in dialog_cls.TECHNICAL_COLUMNS:
                self.assertLess(column, len(dialog_cls.HEADERS))
            self.assertEqual((8, 9), dialog_cls.TECHNICAL_COLUMNS)
        finally:
            ui_module._restore_stubs_for_tests()

    def test_note_names_only_the_automatic_actions(self):
        """Metadaten-Updates sind keine Automatik mehr – der Hinweis nennt
        nur, was zusätzlich zur Auswahl passiert."""
        ui_module = ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        try:
            dialog = ui_module.InventoryDialog.__new__(
                ui_module.InventoryDialog)
            dialog.options = {"collections": True, "deletions": True}
            note = dialog._automatic_note()
            self.assertIn("Serien werden in der Cloud", note)
            self.assertIn("gelöscht", note)
            dialog.options = {"collections": True}
            note = dialog._automatic_note()
            self.assertIn("Serien werden in der Cloud", note)
            self.assertNotIn("gelöscht", note)
            # Ohne automatische Aktionen sagt der Hinweis das ausdrücklich.
            dialog.options = {}
            self.assertIn("nur die Auswahl",
                          dialog._automatic_note())
        finally:
            ui_module._restore_stubs_for_tests()

    def test_selection_helpers_mark_differences_and_clear(self):
        ui_module = ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        try:
            class FakeCheck:
                def __init__(self):
                    self._checked = False

                def setChecked(self, value):
                    self._checked = bool(value)

                def isChecked(self):
                    return self._checked

                def setEnabled(self, value):
                    pass

                def setToolTip(self, text):
                    self.tooltip = text

            ui_module.QCheckBox = FakeCheck
            dialog = ui_module.InventoryDialog.__new__(
                ui_module.InventoryDialog)
            rows = [
                {"status": "identical", "book_id": 1, "tolino_id": "c1"},
                {"status": "new_in_calibre", "book_id": 2, "tolino_id": ""},
                {"status": "changed", "book_id": 3, "tolino_id": "c3",
                 "file_changed": True},
                {"status": "changed", "book_id": 4, "tolino_id": "c4"},
                {"status": "only_tolino", "book_id": None, "tolino_id": "c9"},
            ]
            dialog.rows = rows
            dialog.checks = [FakeCheck() for _ in rows]
            dialog.meta_checks = [FakeCheck() for _ in rows]
            dialog.read_checks = [FakeCheck() for _ in rows]
            dialog.apply_recommended()
            # Neu -> hochladen, geänderte Datei -> hochladen, sonst
            # Metadaten; nichts für unveränderte oder reine Cloud-Bücher.
            self.assertEqual({2, 3}, dialog.selected_upload_ids())
            self.assertEqual({4}, dialog.selected_metadata_ids())
            self.assertEqual([False, True, True, False, False],
                             [check.isChecked() for check in dialog.checks])
            self.assertEqual([False, False, False, True, False],
                             [check.isChecked() for check in dialog.meta_checks])
            # Die Lese-Marke ist nie Teil der Empfehlung.
            self.assertEqual([False] * 5,
                             [check.isChecked() for check in dialog.read_checks])
            self.assertEqual([], dialog.selected_read_targets())
            # Ausdrücklich angehakt zählt sie aber -- nur mit Tolino-ID.
            dialog.read_checks[0].setChecked(True)
            dialog.read_checks[4].setChecked(True)
            self.assertEqual(["c1", "c9"], dialog.selected_read_targets())
            dialog.clear_all_selection()
            self.assertEqual(set(), dialog.selected_upload_ids())
            self.assertEqual(set(), dialog.selected_metadata_ids())
            self.assertEqual([], dialog.selected_read_targets())
        finally:
            ui_module._restore_stubs_for_tests()

    def test_default_actions_follow_the_kind_of_change(self):
        ui_module = ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        try:
            choose = ui_module.InventoryDialog.default_actions
            self.assertEqual((True, False), choose(
                {"book_id": 1, "status": "new_in_calibre"}))
            self.assertEqual((True, False), choose(
                {"book_id": 1, "tolino_id": "t1", "status": "changed",
                 "file_changed": True}))
            self.assertEqual((False, True), choose(
                {"book_id": 1, "tolino_id": "t1", "status": "changed"}))
            self.assertEqual((False, False), choose(
                {"book_id": 1, "tolino_id": "t1", "status": "identical"}))
            self.assertEqual((False, False), choose(
                {"book_id": None, "tolino_id": "t1", "status": "only_tolino"}))
        finally:
            ui_module._restore_stubs_for_tests()


class InventoryDialogBuildTests(unittest.TestCase):
    """Der Aufbau des Bestandsvergleichs wird vollständig durchlaufen.

    Qt wird dabei minimal, aber ehrlich nachgebildet (fehlende Methoden
    fallen als AttributeError auf). Damit sind Tippfehler und falsche
    Spaltenindizes sichtbar, auch ohne Calibre auf dem Rechner.
    """

    @staticmethod
    def _qt_stub():
        class Signal:
            def __init__(self):
                self.slots = []

            def connect(self, slot):
                self.slots.append(slot)

            def emit(self, *args):
                for slot in self.slots:
                    slot(*args)

        class Widget:
            def __init__(self, *args, **_kwargs):
                self.args = args
                self.tooltip = None

            def setToolTip(self, text):
                self.tooltip = text

            def setEnabled(self, value):
                self.enabled = value

            def setVisible(self, value):
                self.visible = value

            def isVisible(self):
                return getattr(self, "visible", True)

            def setWindowTitle(self, title):
                self.window_title = title

            def setMinimumSize(self, *args):
                self.minimum_size = args

            def setMinimumWidth(self, width):
                self.minimum_width = width

            def setWordWrap(self, value):
                self.word_wrap = value

            def setText(self, text):
                self._text = text

            def text(self):
                """Text des Widgets – auch wenn er beim Bau kam."""
                if hasattr(self, "_text"):
                    return self._text
                return self.args[0] if self.args else ""

            def setLayout(self, layout):
                self.layout = layout

            def setChecked(self, value):
                self.checked = bool(value)

            def isChecked(self):
                return getattr(self, "checked", False)

            def setSelectionBehavior(self, value):
                self.selection_behavior = value

            def setRowCount(self, count):
                self.row_count = count

            def setCellWidget(self, row, column, widget):
                self.cell_widgets = getattr(self, "cell_widgets", [])
                self.cell_widgets.append((row, column, widget))

            def setItem(self, row, column, item):
                self.items = getattr(self, "items", {})
                self.items[(row, column)] = item.text

            def resizeColumnsToContents(self):
                self.resized = True

            def setColumnHidden(self, column, hidden):
                self.hidden = getattr(self, "hidden", {})
                self.hidden[column] = hidden

        class Layout:
            def __init__(self, *args, **_kwargs):
                self.children = []

            def addWidget(self, *args):
                self.children.append(args)

            def addLayout(self, layout):
                self.children.append(layout)

            def addRow(self, *args):
                self.children.append(args)

            def addStretch(self, *args):
                pass

        class Header:
            def __init__(self):
                self.sectionClicked = Signal()

            def setSortIndicatorShown(self, shown):
                self.shown = shown

            def setSortIndicator(self, column, order):
                self.indicator = (column, order)

        class Table(Widget):
            SelectRows = "SelectRows"

            def __init__(self, rows, columns):
                Widget.__init__(self)
                self.columns = columns
                self.header_items = {}

            def setHorizontalHeaderLabels(self, labels):
                self.headers = list(labels)

            def horizontalHeader(self):
                self.header = Header()
                return self.header

            def horizontalHeaderItem(self, index):
                return self.header_items.setdefault(index, Widget())

        class Item:
            def __init__(self, text):
                self.text = text

        class Dialog(Widget):
            def __init__(self, parent=None):
                Widget.__init__(self)
                self.parent = parent

            def exec(self):
                return 0

            def accept(self):
                pass

            def reject(self):
                pass

        class CheckBox(Widget):
            def __init__(self, text=""):
                Widget.__init__(self, text)
                self.stateChanged = Signal()
                self.toggled = Signal()
                # Wie in Qt: nach dem Umschalten kommt clicked.
                self.clicked = Signal()

        class Button(Widget):
            def __init__(self, text=""):
                Widget.__init__(self, text)
                self.clicked = Signal()

        class SelectRows:
            pass

        qt = type("Qt", (), {"AscendingOrder": 0, "DescendingOrder": 1,
                             "SelectRows": SelectRows})
        return {
            "Qt": qt,
            "QDialog": Dialog,
            "QVBoxLayout": Layout,
            "QHBoxLayout": Layout,
            "QLabel": Widget,
            "QPushButton": Button,
            "QCheckBox": CheckBox,
            "QGroupBox": Widget,
            "QTableWidget": Table,
            "QTableWidgetItem": Item,
        }

    def _build(self, options):
        ui_module = ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None, qt_stub=self._qt_stub())
        rows = [
            {"title": "Zulu", "authors": "Z", "status": "identical",
             "book_id": 1, "tolino_id": "c1", "isbn": "111",
             "explanation": "Titel stimmt mit der Cloud überein."},
            {"title": "Alpha", "authors": "A", "status": "new_in_calibre",
             "book_id": 2, "tolino_id": "", "isbn": "222",
             "explanation": "Noch nicht in der Cloud – kann hochgeladen "
             "werden."},
        ]
        return ui_module, ui_module.InventoryDialog(rows, options=options)

    def test_constructor_writes_every_column_in_the_right_order(self):
        ui_module, dialog = self._build({"collections": True})
        try:
            self.assertEqual(list(ui_module.InventoryDialog.HEADERS),
                             dialog.table.headers)
            cells = {(row, column): widget
                     for row, column, widget in dialog.table.cell_widgets}
            for index, row in enumerate(dialog.rows):
                in_cloud = bool(row["tolino_id"])
                # Drei Auswahlfelder je Buch ...
                self.assertIs(cells[(index, 0)], dialog.checks[index])
                self.assertIs(cells[(index, 1)], dialog.meta_checks[index])
                self.assertIs(cells[(index, 2)], dialog.read_checks[index])
                # ... die zwei Orts-Spalten mit je einem Häkchen ...
                self.assertEqual("✓" if row["book_id"] is not None else "",
                                 dialog.table.items[(index, 3)])
                self.assertEqual("✓" if in_cloud else "",
                                 dialog.table.items[(index, 4)])
                # ... und danach die Textspalten.
                self.assertEqual(row["title"], dialog.table.items[(index, 5)])
                self.assertEqual(row["authors"], dialog.table.items[(index, 6)])
                self.assertEqual(row["explanation"],
                                 dialog.table.items[(index, 7)])
                self.assertEqual(row["isbn"], dialog.table.items[(index, 8)])
                self.assertEqual(row["tolino_id"],
                                 dialog.table.items[(index, 9)])
                # Metadaten und Lese-Marke gehen nur für Bücher, die schon in
                # der Cloud liegen; Hochladen nur mit Calibre-Buch.
                upload = dialog.checks[index]
                metadata = dialog.meta_checks[index]
                mark_read = dialog.read_checks[index]
                self.assertTrue(upload.enabled)
                self.assertEqual(in_cloud, metadata.enabled)
                self.assertEqual(in_cloud, mark_read.enabled)
                # Vorbelegt ist, was der Vergleich vorschlägt; die Lese-Marke
                # bleibt unangetastet.
                self.assertEqual(bool(row["upload"]), upload.isChecked())
                self.assertEqual(bool(row["metadata"]), metadata.isChecked())
                self.assertFalse(mark_read.isChecked())
            # Nur die technischen Spalten sind zunächst verborgen.
            self.assertEqual({8: True, 9: True}, dialog.table.hidden)
            self.assertIn("Serien werden in der Cloud",
                          dialog.note.text())
            self.assertIn("Gelesen-Status ändert nur",
                          dialog.note.text())
            self.assertIn("1 zum Hochladen", dialog.summary.text())
            self.assertIn("0 für Metadaten", dialog.summary.text())
            self.assertIn("0 als gelesen", dialog.summary.text())
            self.assertEqual("Auswahl übernehmen (1)", dialog.confirm.text())
        finally:
            ui_module._restore_stubs_for_tests()

    def test_toggle_switches_the_technical_columns(self):
        ui_module, dialog = self._build({"collections": True})
        try:
            dialog._toggle_technical_columns(True)
            self.assertEqual({8: False, 9: False}, dialog.table.hidden)
            dialog._toggle_technical_columns(False)
            self.assertEqual({8: True, 9: True}, dialog.table.hidden)
            self.assertIn("Serien werden in der Cloud",
                          dialog.note.text())
        finally:
            ui_module._restore_stubs_for_tests()


class ComparisonLayoutTests(unittest.TestCase):
    """Der Bestandsvergleich bleibt kurz: 7 sichtbare Spalten und EIN Knopf
    für alle zeilenbezogenen Aktionen (0.15.2)."""

    @staticmethod
    def _source():
        return (Path(__file__).with_name("ui.py")).read_text(encoding="utf-8")

    @staticmethod
    def _body():
        source = ComparisonLayoutTests._source()
        return source.split("class InventoryDialog(QDialog):", 1)[1].split(
            "\nclass ", 1)[0]

    def test_two_location_columns_plus_technical_ones(self):
        """Die zwei Orts-Spalten stehen wieder einzeln da (0.15.3).

        Feldbefund: «Bring mir die zwei Spalten zurück, wo die Bücher
        sind.» Die doppelte Statusspalte bleibt entfallen.
        """
        ui_module = ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        try:
            headers = ui_module.InventoryDialog.HEADERS
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual(
            ("Hochladen", "Metadaten", "Gelesen", "In Calibre", "In der Cloud",
             "Titel", "Autor(en)", "Hinweis", "ISBN", "Tolino-ID"), headers)
        technical = ui_module.InventoryDialog.TECHNICAL_COLUMNS
        self.assertEqual((8, 9), technical)
        self.assertEqual(8, len(headers) - len(technical))
        # Die eine zusammengefasste Spalte und der doppelte Status sind weg.
        for gone in ("Wo", "Status"):
            self.assertNotIn(gone, headers)

    def test_one_button_holds_all_marked_row_actions(self):
        body = self._body()
        for gone in ("self.download_btn", "self.collection_add_btn",
                     "self.collection_rm_btn", "self.read_selected",
                     "self.more_toggle", "_toggle_more_actions"):
            self.assertNotIn(gone, body, "%s sollte entfallen sein" % gone)
        self.assertIn("self.row_actions = QPushButton(", body)
        self.assertIn("self.row_actions.clicked.connect(self.choose_row_action)",
                      body)

    def test_the_menu_offers_every_row_action(self):
        """Die Spalten-Aktionen gelten auch für markierte Zeilen (0.15.11)."""
        ui_module = ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        try:
            actions = ui_module.InventoryDialog.ROW_ACTIONS
        finally:
            ui_module._restore_stubs_for_tests()
        keys = [key for _label, key in actions]
        self.assertEqual(["upload_on", "upload_off", "metadata_on",
                          "metadata_off", "read", "read_off", "download",
                          "collection_add", "collection_remove"], keys)
        labels = [label for label, _key in actions]
        for wanted in ("Zum Hochladen auswählen", "Hochladen abwählen",
                       "Für Metadaten auswählen", "Metadaten abwählen",
                       "Als gelesen markieren", "Als ungelesen markieren",
                       "Aus der Cloud herunterladen",
                       "Zu Sammlung hinzufügen …",
                       "Aus Sammlung entfernen …"):
            self.assertIn(wanted, labels)
        # Jeder Menüpunkt steht auch im Verteiler, sonst wäre er tot.
        dispatch = self._body().split("    def choose_row_action(self):", 1)[1]
        dispatch = dispatch.split("\n    def ", 1)[0]
        for key in keys:
            self.assertIn('"%s"' % key, dispatch, key)

    def test_every_row_action_still_has_a_handler(self):
        """Ein Menüpunkt ohne Handler wäre ein toter Eintrag."""
        source = self._body().split("    def choose_row_action(self):", 1)[1]
        source = source.split("\n    def ", 1)[0]
        handlers = ("mark_read_for_marked_rows", "clear_read_for_marked_rows",
                    "set_column_for_marked_rows", "download_selected",
                    "add_selected_to_collection",
                    "remove_selected_from_collection")
        for name in handlers:
            self.assertIn(name, source, "Handler %s fehlt im Menü" % name)


class InventoryDialogReadStateTests(unittest.TestCase):
    """Spalte «Gelesen» kommt aus der Cloud und lässt sich mehrfach setzen.

    Feldbefund 0.15.0: «Der Knopf Gelesen soll bereits ausgewählt sein, wenn
    das Buch in der Cloud als gelesen markiert ist» und «Jetzt muss ich alle
    meine 500 Bücher einzeln anklicken».
    """

    def _ui(self):
        return ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None,
            qt_stub=InventoryDialogBuildTests._qt_stub())

    def _rows(self):
        return [
            {"title": "Gelesen", "authors": "A", "status": "identical",
             "book_id": 1, "tolino_id": "t-1", "read_in_cloud": True},
            {"title": "Ungelesen", "authors": "B", "status": "identical",
             "book_id": 2, "tolino_id": "t-2"},
            {"title": "Auch ungelesen", "authors": "C", "status": "changed",
             "book_id": 3, "tolino_id": "t-3"},
            {"title": "Nur lokal", "authors": "D",
             "status": "new_in_calibre", "book_id": 4, "tolino_id": ""},
        ]

    @staticmethod
    def _marked_table(marked):
        class Index:
            def __init__(self, row):
                self._row = row

            def row(self):
                return self._row

        class Model:
            def selectedRows(self):
                return [Index(row) for row in marked]

        class Table:
            def selectionModel(self):
                return Model()

        return Table()

    @staticmethod
    def _cells_by_title(dialog):
        """Zeilen-Widgets, nach Titel – das Fenster sortiert beim Bau."""
        cells = {(row, column): widget
                 for row, column, widget in dialog.table.cell_widgets}
        return {row["title"]: cells[(index, 2)]
                for index, row in enumerate(dialog.rows)}

    def _with_message_box(self, ui_module):
        calls = []

        class MsgBox:
            @staticmethod
            def information(*args, **kwargs):
                calls.append(" ".join(str(a) for a in args))

            @staticmethod
            def warning(*args, **kwargs):
                calls.append(" ".join(str(a) for a in args))

        ui_module.QMessageBox = MsgBox
        return calls

    def test_cloud_read_book_is_prechecked_and_unlockable(self):
        ui_module = self._ui()
        try:
            self._with_message_box(ui_module)
            dialog = ui_module.InventoryDialog(self._rows())
            read_cell = self._cells_by_title(dialog)
            # Schon gelesen: angehakt -- aber anklickbar, damit man es auch
            # wieder auf ungelesen setzen kann (0.15.2).
            self.assertTrue(read_cell["Gelesen"].isChecked())
            self.assertTrue(read_cell["Gelesen"].enabled)
            self.assertIn("Ein Klick entsperrt",
                          read_cell["Gelesen"].tooltip)
            # Ungelesen: leer, aber anklickbar.
            self.assertFalse(read_cell["Ungelesen"].isChecked())
            self.assertTrue(read_cell["Ungelesen"].enabled)
            # Ohne Cloud-Kopie gibt es keine Lese-Marke.
            self.assertFalse(read_cell["Nur lokal"].enabled)
            self.assertIn("in der Cloud liegt", read_cell["Nur lokal"].tooltip)
            # Nur echte Änderungen gehen an die Cloud -- und ohne Klick
            # wird nichts ungelesen.
            self.assertEqual([], dialog.selected_read_targets())
            self.assertEqual([], dialog.selected_read_unmarks())
            read_cell["Ungelesen"].setChecked(True)
            read_cell["Auch ungelesen"].setChecked(True)
            self.assertEqual({"t-2", "t-3"},
                             set(dialog.selected_read_targets()))
            dialog._update_summary()
            self.assertIn("2 als gelesen", dialog.summary.text())
        finally:
            ui_module._restore_stubs_for_tests()

    def test_recommendation_keeps_the_cloud_read_marks(self):
        """«Empfehlung auswählen» hakt nie etwas Neues auf gelesen und
        nimmt die Cloud-Marken nicht weg."""
        ui_module = self._ui()
        try:
            self._with_message_box(ui_module)
            dialog = ui_module.InventoryDialog(self._rows())
            read_cell = self._cells_by_title(dialog)
            read_cell["Ungelesen"].setChecked(True)
            dialog._sync_checks_to_rows()
            dialog.apply_recommended()
            self.assertTrue(read_cell["Gelesen"].isChecked())
            self.assertFalse(read_cell["Ungelesen"].isChecked())
            self.assertEqual([], dialog.selected_read_targets())
            self.assertEqual([], dialog.selected_read_unmarks())
        finally:
            ui_module._restore_stubs_for_tests()

    def test_a_read_book_is_unmarked_with_two_clicks(self):
        """Feldbefund 0.15.2: «Ein Buch, das in der Cloud als gelesen
        markiert ist, kann ich nicht mehr auf ungelesen setzen.»

        Der erste Klick löst nur die Sperre (Häkchen bleibt), der zweite
        nimmt das Häkchen weg – so löscht kein versehentlicher Klick eine
        Lese-Marke in der Cloud.
        """
        ui_module = self._ui()
        try:
            self._with_message_box(ui_module)
            dialog = ui_module.InventoryDialog(self._rows())
            cell = self._cells_by_title(dialog)["Gelesen"]
            self.assertTrue(cell.isChecked())
            # Erster Klick: Qt nimmt das Häkchen weg, der Handler gibt es
            # zurück -- nur die Sperre fällt.
            cell.setChecked(False)
            cell.clicked.emit()
            self.assertTrue(cell.isChecked())
            self.assertIn("Erneut anklicken", cell.tooltip)
            self.assertEqual([], dialog.selected_read_unmarks())
            self.assertEqual([], dialog.selected_read_targets())
            # Zweiter Klick: das Häkchen bleibt weg -> in der Cloud ungelesen.
            cell.setChecked(False)
            cell.clicked.emit()
            self.assertFalse(cell.isChecked())
            self.assertEqual(["t-1"], dialog.selected_read_unmarks())
            self.assertEqual([], dialog.selected_read_targets())
            dialog._update_summary()
            self.assertIn("1 auf ungelesen", dialog.summary.text())
            # Die Zahl im Knopf zählt alle Aktionen: 1 Upload („Nur lokal“),
            # 1 Metadaten-Abgleich („Auch ungelesen“) und der neue
            # Ungelesen-Schritt.
            self.assertEqual("Auswahl übernehmen (3)", dialog.confirm.text())
        finally:
            ui_module._restore_stubs_for_tests()

    def test_an_unmarked_book_stays_unmarked_after_sorting(self):
        """Umsortieren baut die Zeilen neu – der Wunsch bleibt."""
        ui_module = self._ui()
        try:
            self._with_message_box(ui_module)
            dialog = ui_module.InventoryDialog(self._rows())
            cell = self._cells_by_title(dialog)["Gelesen"]
            cell.setChecked(False)
            cell.clicked.emit()   # entsperren
            cell.setChecked(False)
            cell.clicked.emit()   # Häkchen weg
            dialog._sort_by_column(0)
            dialog._sort_by_column(0)
            self.assertEqual(["t-1"], dialog.selected_read_unmarks())
            rebuilt = self._cells_by_title(dialog)["Gelesen"]
            self.assertFalse(rebuilt.isChecked())
            # Die Sperre ist an derselben Zeile weiter aufgehoben: ein Klick
            # hakt sie wieder an.
            self.assertIn("Erneut anklicken", rebuilt.tooltip)
            rebuilt.setChecked(True)
            rebuilt.clicked.emit()
            self.assertEqual([], dialog.selected_read_unmarks())
        finally:
            ui_module._restore_stubs_for_tests()

    def test_marked_rows_get_the_read_mark_in_one_click(self):
        """Die Zeilen-Aktion hakt ungelesene Cloud-Bücher in einem Zug
        auf – ohne Rückfrage und ohne Meldung."""
        ui_module = self._ui()
        try:
            calls = self._with_message_box(ui_module)
            dialog = ui_module.InventoryDialog(self._rows())
            dialog.table = self._marked_table(range(len(dialog.rows)))
            dialog.mark_read_for_marked_rows()
            # Das gelesene Buch hat schon seine Marke, „Nur lokal“ keine
            # Cloud-Kopie -- nur die zwei ungelesenen Cloud-Bücher bleiben.
            self.assertEqual({"t-2", "t-3"},
                             set(dialog.selected_read_targets()))
            self.assertEqual([], calls)
        finally:
            ui_module._restore_stubs_for_tests()

    def test_marked_rows_without_cloud_copy_change_nothing(self):
        ui_module = self._ui()
        try:
            calls = self._with_message_box(ui_module)
            dialog = ui_module.InventoryDialog(self._rows())
            local_index = [index for index, row in enumerate(dialog.rows)
                           if not row["tolino_id"]]
            dialog.table = self._marked_table(local_index)
            dialog.mark_read_for_marked_rows()
            self.assertEqual([], dialog.selected_read_targets())
            self.assertTrue(any("Nichts zu tun" in text for text in calls))
        finally:
            ui_module._restore_stubs_for_tests()

    def test_click_without_marked_rows_explains_the_multi_selection(self):
        ui_module = self._ui()
        try:
            calls = self._with_message_box(ui_module)
            dialog = ui_module.InventoryDialog(self._rows())
            dialog.table = self._marked_table([])
            dialog.mark_read_for_marked_rows()
            self.assertTrue(any("Strg" in text for text in calls), calls)
            self.assertEqual([], dialog.selected_read_targets())
        finally:
            ui_module._restore_stubs_for_tests()

    def test_marked_rows_can_be_chosen_for_upload_and_metadata(self):
        """Hochladen/Metadaten für markierte Zeilen – wie die Kästchen."""
        ui_module = self._ui()
        try:
            self._with_message_box(ui_module)
            dialog = ui_module.InventoryDialog(self._rows())
            dialog.table = self._marked_table(range(len(dialog.rows)))
            dialog.set_column_for_marked_rows("upload", True, "keine Datei")
            self.assertEqual({1, 2, 3, 4}, dialog.selected_upload_ids())
            dialog.set_column_for_marked_rows("metadata", True, "keine Cloud")
            # «Nur lokal» hat keine Cloud-Kopie – dort geht kein Metadaten-Abgleich.
            self.assertEqual({1, 2, 3}, dialog.selected_metadata_ids())
            dialog.set_column_for_marked_rows("upload", False, "keine Datei")
            self.assertEqual(set(), dialog.selected_upload_ids())
            dialog.set_column_for_marked_rows("metadata", False, "keine Cloud")
            self.assertEqual(set(), dialog.selected_metadata_ids())
        finally:
            ui_module._restore_stubs_for_tests()

    def test_marked_rows_can_be_set_back_to_unread(self):
        """«Als ungelesen markieren» löst die Sperre und zählt als Abwahl."""
        ui_module = self._ui()
        try:
            self._with_message_box(ui_module)
            dialog = ui_module.InventoryDialog(self._rows())
            dialog.table = self._marked_table(range(len(dialog.rows)))
            dialog.clear_read_for_marked_rows()
            # Nur das in der Cloud gelesene Buch lässt sich zurücksetzen.
            self.assertEqual({"t-1"}, set(dialog.selected_read_unmarks()))
            self.assertEqual([], dialog.selected_read_targets())
            row = next(item for item in dialog.rows
                       if item["tolino_id"] == "t-1")
            self.assertTrue(row["read_unlocked"])
            self.assertFalse(row["read"])
        finally:
            ui_module._restore_stubs_for_tests()


    def test_one_button_handles_all_actions_for_marked_rows(self):
        """Alle vier Zeilen-Aktionen liegen hinter EINEM Knopf (0.15.2)."""
        ui_module = self._ui()
        try:
            self._with_message_box(ui_module)
            dialog = ui_module.InventoryDialog(self._rows())
            self.assertEqual("Für markierte Zeilen …",
                             dialog.row_actions.text())
            self.assertIn("Strg", dialog.row_actions.tooltip)
            keys = [key for _label, key in dialog.ROW_ACTIONS]
            self.assertEqual(["upload_on", "upload_off", "metadata_on",
                              "metadata_off", "read", "read_off", "download",
                              "collection_add", "collection_remove"], keys)
        finally:
            ui_module._restore_stubs_for_tests()


class CollectionReconcileWorkerTests(unittest.TestCase):
    """Serien → Sammlungen im SyncWorker (0.12.0).

    Feldbefund des Nutzers: «ich habe das Gefühl, es synchronisiert die
    Ordner automatisch». Genau das passiert hier – aber nur, wenn die
    Sammlungen eingeschaltet sind, und vorhandene Zuordnungen werden
    nicht erneut geschrieben.
    """

    @staticmethod
    def _signal_descriptor():
        class Signal:
            def __init__(self):
                self.slots = []

            def connect(self, slot):
                self.slots.append(slot)

            def emit(self, *args):
                for slot in self.slots:
                    slot(*args)

        class Descriptor:
            def __init__(self, *_types):
                self.name = ""

            def __set_name__(self, owner, name):
                self.name = name

            def __get__(self, obj, objtype=None):
                if obj is None:
                    return self
                return obj.__dict__.setdefault(self.name, Signal())

        return Descriptor, Signal

    def _run_worker(self, series_map, state, known_pairs, read_marks=None,
                    read_unmarks=None):
        ui_module = ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None,
            qt_stub={"pyqtSignal": self._signal_descriptor()[0]})
        calls = {"pairs_reads": 0, "added": [], "progress": [],
                 "completed": None, "failed": None, "read_marks": []}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rotated-refresh"

            def login(self):
                return "rotated-refresh"

            def inventory_ids(self):
                return set()

            def collection_pairs(self):
                calls["pairs_reads"] += 1
                return set(known_pairs)

            def add_to_collection(self, tolino_id, series, revision=None):
                calls["added"].append((tolino_id, series))
                return 1

            def mark_read_many(self, book_ids, finished=True):
                calls["read_marks"].append((list(book_ids), finished))
                return 2

        settings = {"partner_id": 4, "hardware_id": "hw",
                    "refresh_token": "tok", "username": "", "password": "",
                    "enable_deletions": False, "state": state}
        try:
            with patch("calibre_plugin.ui.TolinoClient", FakeClient):
                worker = ui_module.SyncWorker(settings, [], [], [], series_map,
                                             read_marks, read_unmarks)
                worker.progress.connect(
                    lambda *args: calls["progress"].append(args))
                worker.completed.connect(
                    lambda *args: calls.update(completed=args))
                worker.failed.connect(
                    lambda *args: calls.update(failed=args))
                worker.run()
        finally:
            ui_module._restore_stubs_for_tests()
        return calls

    def test_switched_on_creates_only_missing_collections(self):
        state = {"u1": {"tolino_id": "t1"}, "u2": {"tolino_id": "t2"}}
        calls = self._run_worker(
            {"u1": "Adamsberg", "u2": "Neu"}, state,
            {("t1", "Adamsberg")})
        # t1/Adamsberg ist schon da, nur t2/Neu fehlt.
        self.assertEqual([("t2", "Neu")], calls["added"])
        self.assertEqual(1, calls["pairs_reads"])
        self.assertIsNotNone(calls["completed"])
        self.assertIsNone(calls["failed"])
        self.assertTrue(any("Serie in der Cloud: Neu" in text
                            for _done, _total, text in calls["progress"]))
        # Beide Zuordnungen sind jetzt gemerkt -- auch die schon vorhandene.
        # Diesen Zustand bekommt das Dashboard und speichert ihn.
        self.assertEqual(["Adamsberg"], state["u1"]["collections"])
        self.assertEqual(["Neu"], state["u2"]["collections"])
        emitted = calls["completed"][0]
        self.assertEqual(["Adamsberg"], emitted["u1"]["collections"])
        self.assertEqual(["Neu"], emitted["u2"]["collections"])

    def test_remembered_collections_skip_the_cloud_entirely(self):
        """Zweiter Sync mit denselben Serien: nichts zu tun, also auch
        keine Cloud-Anfrage und keine Zuordnung mehr."""
        state = {"u1": {"tolino_id": "t1", "collections": ["Adamsberg"]}}
        calls = self._run_worker({"u1": "Adamsberg"}, state, set())
        self.assertEqual([], calls["added"])
        self.assertEqual(0, calls["pairs_reads"])
        self.assertIsNotNone(calls["completed"])

    def test_switched_off_leaves_the_cloud_untouched(self):
        calls = self._run_worker(
            {}, {"u1": {"tolino_id": "t1"}},
            {("t1", "Adamsberg")})
        self.assertEqual([], calls["added"])
        self.assertEqual(0, calls["pairs_reads"])
        self.assertIsNotNone(calls["completed"])

    def test_books_without_cloud_copy_get_no_collection(self):
        calls = self._run_worker(
            {"u2": "Neu"}, {"u1": {"tolino_id": "t1"}}, set())
        self.assertEqual([], calls["added"])
        self.assertEqual(0, calls["pairs_reads"])

    def test_read_marks_are_one_bulk_call(self):
        calls = self._run_worker(
            {}, {"u1": {"tolino_id": "t1"}}, set(),
            read_marks=["t1", "t2"])
        self.assertEqual([(["t1", "t2"], True)], calls["read_marks"])
        self.assertIsNotNone(calls["completed"])
        self.assertTrue(any("Als gelesen markieren" in text
                            for _done, _total, text in calls["progress"]))

    def test_read_unmarks_are_one_bulk_call_with_finished_false(self):
        """Häkchen weggeklickt: EIN Aufruf, der die Marke entfernt (0.15.2)."""
        calls = self._run_worker(
            {}, {"u1": {"tolino_id": "t1"}}, set(),
            read_unmarks=["t1", "t2"])
        self.assertEqual([(["t1", "t2"], False)], calls["read_marks"])
        self.assertTrue(any("Als ungelesen markieren" in text
                            for _done, _total, text in calls["progress"]))
        self.assertIsNotNone(calls["completed"])
        self.assertIsNone(calls["failed"])

    def test_marking_and_unmarking_are_two_separate_calls(self):
        calls = self._run_worker(
            {}, {"u1": {"tolino_id": "t1"}}, set(),
            read_marks=["t1"], read_unmarks=["t2"])
        self.assertEqual([(["t1"], True), (["t2"], False)], calls["read_marks"])
        # Beide Schritte zählen im Fortschritt mit.
        self.assertEqual(2, max(total for _done, total, _text
                                in calls["progress"]))

    def test_without_read_marks_the_cloud_status_is_untouched(self):
        calls = self._run_worker({}, {"u1": {"tolino_id": "t1"}}, set())
        self.assertEqual([], calls["read_marks"])


class DashboardAccountSwitchTests(unittest.TestCase):
    """Mehrere Konten: Umschalten direkt auf der ersten Seite (0.15.4).

    Feldbefund: «Kann man die Kontoauswahl, sofern mehrere Konten
    vorhanden, ebenfalls auf die erste Seite bringen?» Die Zeile erscheint
    nur bei mehr als einem Konto – sonst wäre sie nur Ballast.
    """

    class Combo:
        """Minimaler Qt-Nachbau für Kombobox, Eingabe und Label."""

        def __init__(self, *_args, **_kwargs):
            self.items = []
            self.current_index = 0
            self.visible = True
            self.text_value = ""

        def blockSignals(self, value):
            self.blocked = value

        def clear(self):
            self.items = []

        def addItem(self, text, data=None):
            self.items.append((text, data))

        def setCurrentIndex(self, index):
            self.current_index = index

        def currentData(self):
            return self.itemData(self.current_index)

        def findData(self, data):
            return next((index for index, (_text, item) in enumerate(self.items)
                         if item == data), -1)

        def itemData(self, index):
            if 0 <= index < len(self.items):
                return self.items[index][1]
            return None

        def setVisible(self, value):
            self.visible = value

        def setText(self, text):
            self.text_value = text

        def text(self):
            return self.text_value

        def setToolTip(self, text):
            self.tooltip = text

        def setEnabled(self, value):
            self.enabled = value

        def setChecked(self, value):
            self.checked = bool(value)

        def isChecked(self):
            return getattr(self, "checked", False)

        def setRange(self, *_args):
            pass

        def setValue(self, _value):
            pass

    def _dashboard(self):
        dashboard = type("Dashboard", (), {})()
        for name in ("account_select", "account_quick", "partner", "hardware",
                     "refresh", "formats", "account_quick_label", "status",
                     "progress", "progress_label", "primary", "covers",
                     "deletions", "sync_collections", "tolino_column"):
            setattr(dashboard, name, self.Combo())
        dashboard.thread = None
        return dashboard

    @staticmethod
    def _accounts(count):
        return [{"name": "erstes", "partner_id": 1, "hardware_id": "hw-1",
                 "refresh_token": "token-1", "username": "", "password": "",
                 "state": {}},
                {"name": "zweites", "partner_id": 2, "hardware_id": "hw-2",
                 "refresh_token": "", "username": "", "password": "",
                 "state": {}}][:count]

    def _run(self, count, active="erstes"):
        """load_values/switch_account mit gefälschten Einstellungen."""
        ui_module = ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        original = (config.PREFERENCES, ui_module.hardware_id,
                    ui_module.set_active_account)

        def restore():
            (config.PREFERENCES, ui_module.hardware_id,
             ui_module.set_active_account) = original
            ui_module._restore_stubs_for_tests()

        # Erst NACH dem Test zurücknehmen: sonst ist der Umbau weg, bevor
        # der Test umschaltet.
        self.addCleanup(restore)
        calls = []
        config.PREFERENCES = config.JSONConfig("test")
        config.PREFERENCES.update({"accounts": self._accounts(count),
                                   "active_account": active})
        ui_module.hardware_id = lambda: "auto-hw"
        real_set_active = original[2]

        def record(name):
            calls.append(("active", name))
            return real_set_active(name)

        ui_module.set_active_account = record
        dashboard = self._dashboard()
        dashboard.partner.items = [("%s" % pid, pid)
                                   for pid in sorted(ui_module.PARTNERS)]
        # Die echten Methoden an das gefälschte Fenster binden: geprüft
        # wird ihr Verhalten, nicht ein Nachbau davon.
        for name in ("load_values", "switch_account", "update_status"):
            setattr(dashboard, name,
                    getattr(ui_module.SyncDashboard, name).__get__(dashboard))
        dashboard._save_visible_account = lambda: calls.append(("save", None))
        dashboard.load_values()
        return ui_module, dashboard, calls

    def test_one_account_hides_the_row(self):
        ui_module, dashboard, _calls = self._run(1)
        self.assertFalse(dashboard.account_quick.visible)
        self.assertFalse(dashboard.account_quick_label.visible)
        self.assertEqual([("erstes", "erstes")], dashboard.account_quick.items)

    def test_two_accounts_show_the_row_on_the_first_page(self):
        ui_module, dashboard, _calls = self._run(2)
        self.assertTrue(dashboard.account_quick.visible)
        self.assertTrue(dashboard.account_quick_label.visible)
        self.assertEqual([("erstes", "erstes"), ("zweites", "zweites")],
                         dashboard.account_quick.items)
        self.assertEqual(0, dashboard.account_quick.current_index)
        self.assertEqual(0, dashboard.account_select.current_index)

    def test_switching_on_the_first_page_moves_the_active_account(self):
        ui_module, dashboard, calls = self._run(2)
        self.assertTrue(dashboard.switch_account("zweites"))
        # Erst sichern, dann umschalten.
        self.assertEqual([("save", None), ("active", "zweites")], calls)
        self.assertEqual("zweites", dashboard.account_name)
        # Beide Auswahlen zeigen dasselbe Konto -- sonst arbeitet der
        # Plugin mit dem vorherigen weiter.
        self.assertEqual(1, dashboard.account_quick.current_index)
        self.assertEqual(1, dashboard.account_select.current_index)
        # Buchhändler, Hardware-ID und Zugang kommen vom neuen Konto.
        self.assertEqual(2, dashboard.partner.currentData())
        self.assertEqual("hw-2", dashboard.hardware.text())
        self.assertEqual("", dashboard.refresh.text())
        self.assertIn("Noch nicht angemeldet", dashboard.status.text_value)
        self.assertFalse(dashboard.primary.enabled)

    def test_switching_to_the_same_or_unknown_account_does_nothing(self):
        ui_module, dashboard, calls = self._run(2)
        self.assertFalse(dashboard.switch_account("erstes"))
        self.assertFalse(dashboard.switch_account("gibt-es-nicht"))
        self.assertFalse(dashboard.switch_account(""))
        self.assertEqual([], calls)

    def test_both_places_use_the_same_switch(self):
        source = (Path(__file__).with_name("ui.py")).read_text(encoding="utf-8")
        body = source.split("class SyncDashboard(QDialog):", 1)[1].split(
            "\nclass ", 1)[0]
        self.assertIn("self.account_quick.currentIndexChanged.connect(", body)
        self.assertIn("self.quick_account_changed)", body)
        for method in ("account_changed", "quick_account_changed"):
            handler = body.split("    def %s(self, index):" % method, 1)[1]
            handler = handler.split("\n    def ", 1)[0]
            self.assertIn("self.switch_account(", handler)
            self.assertIn("itemData(index)", handler)


class DashboardLayoutTests(unittest.TestCase):
    """Feldbefund: Der Knopf «Diagnose» wurde im Dashboard gebaut, aber nie
    in ein Layout gehängt – er war dadurch ab 0.10.0 unsichtbar, obwohl
    Code, Tooltip und Funktion vorhanden waren.

    Der Test liest den Quelltext (ohne Qt starten zu müssen) und verlangt,
    dass JEDER im Dashboard erzeugte Knopf/Haken einem Layout übergeben
    wird – sonst verschwindet er wieder stillschweigend.
    """

    def _source(self):
        return (Path(__file__).with_name("ui.py")).read_text(encoding="utf-8")

    def _dashboard_body(self):
        return self._source().split("class SyncDashboard(QDialog):", 1)[1].split(
            "\nclass ", 1)[0]

    def test_every_created_button_is_added_to_a_layout(self):
        """Auch der Knopf im Einstellungsfenster und die Kontoauswahl auf
        der ersten Seite (0.15.2 / 0.15.4)."""
        source = self._source()
        names = re.findall(
            r"self\.(\w+) = (?:QPushButton|QCheckBox|QComboBox|QLineEdit)\(",
            source)
        self.assertIn("debug", names)
        # Beschriftungen wie "Hardware-ID (automatisch erkannt)" enthalten
        # selbst Klammern -- deshalb zeilenweise prüfen: dieselbe Zeile muss
        # an ein addRow/addWidget/addLayout gehen.
        for name in names:
            self.assertTrue(
                re.search(r"add(?:Row|Widget|Layout)\([^\n]*self\.%s\b" % name,
                          source),
                "Widget %r hängt in keinem Layout" % name)

    def test_login_and_compare_have_their_own_buttons(self):
        """Der alte Doppelweg (zwei Anmeldeknöpfe) bleibt weg, aber der
        Anmeldeknopf steht wieder sichtbar auf der ersten Seite (0.15.3)."""
        body = self._dashboard_body()
        self.assertIn("self.primary.clicked.connect(self.primary_action)", body)
        self.assertIn(
            "self.login_button.clicked.connect(self.browser_login)", body)
        self.assertNotIn("self.browser = QPushButton(", body)
        self.assertNotIn("self.start = QPushButton(", body)
        self.assertNotIn("self.cancel = QPushButton(", body)

    def test_the_settings_dialog_holds_the_rare_options(self):
        """Alles Seltene liegt in EINEM Fenster, nicht hinter zwei
        «… anzeigen»-Häkchen (0.15.2)."""
        source = self._source()
        self.assertIn("class SettingsDialog(QDialog):", source)
        settings = source.split("class SettingsDialog(QDialog):", 1)[1].split(
            "\nclass ", 1)[0]
        for group in ("Übertragung", "Konten", "Diagnose"):
            self.assertIn('QGroupBox("%s")' % group, settings)
        body = self._dashboard_body()
        self.assertNotIn("advanced_toggle", body)
        self.assertNotIn("_toggle_advanced", body)
        self.assertIn("self.settings_button.clicked.connect(self.open_settings)",
                      body)

    def test_diagnostics_button_reports_without_network(self):
        """Der Knopf öffnet den Bericht; die Tolino-Anfrage passiert erst
        im Bericht selbst (Feldbefund: «Diagnose» führt keine Anmeldung
        und keine Tolino-Anfrage aus)."""
        body = self._dashboard_body()
        self.assertIn("self.debug.clicked.connect(self.debug_diagnose)", body)
        source = (Path(__file__).with_name("ui.py")).read_text(encoding="utf-8")
        diagnose = source.split("def debug_diagnose", 1)[1].split(
            "\n    def ", 1)[0]
        self.assertIn("diagnose_preparation", diagnose)
        self.assertNotIn("TolinoClient", diagnose)
        self.assertNotIn("login", diagnose)


class DashboardPrimaryActionTests(unittest.TestCase):
    """Hauptknopf und Anmeldeknopf getrennt (0.15.3).

    Feldbefund: «Bring mir den Anmeldeknopf auf die erste Seite zurück.»
    Vorher war das Anmelden nur ein Zustand des Hauptknopfes – bei
    gespeichertem, aber abgelaufenem Zugang war es damit nicht erreichbar.
    """

    @staticmethod
    def _dashboard(token="", running=False):
        class LineEdit:
            def __init__(self, value):
                self.value = value

            def text(self):
                return self.value

        class Widget:
            def setText(self, text):
                self.text_value = text

            def setVisible(self, value):
                self.visible = value

            def setEnabled(self, value):
                self.enabled = value

            def setToolTip(self, text):
                self.tooltip = text

        dashboard = type("Dashboard", (), {})()
        dashboard.thread = object() if running else None
        dashboard.refresh = LineEdit(token)
        dashboard.status = Widget()
        dashboard.progress = Widget()
        dashboard.progress_label = Widget()
        dashboard.primary = Widget()
        return dashboard

    @staticmethod
    def _source():
        return (Path(__file__).with_name("ui.py")).read_text(encoding="utf-8")

    def _update_status(self):
        ui_module = ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        try:
            return ui_module.SyncDashboard.update_status
        finally:
            ui_module._restore_stubs_for_tests()

    def test_primary_action_compares_or_cancels_never_logs_in(self):
        body = self._source().split("    def primary_action(self):", 1)[1]
        body = body.split("\n    def ", 1)[0]
        self.assertIn("if self.thread is not None:", body)
        self.assertIn("self.cancel_sync()", body)
        self.assertIn("self.start_sync()", body)
        self.assertNotIn("browser_login", body)

    def test_the_login_button_sits_on_the_first_page(self):
        body = self._source().split("class SyncDashboard(QDialog):", 1)[1]
        body = body.split("\nclass ", 1)[0]
        self.assertIn(
            'self.login_button = QPushButton("Jetzt bei Tolino anmelden")',
            body)
        self.assertIn(
            "self.login_button.clicked.connect(self.browser_login)", body)
        self.assertRegex(body, r"addRow\([^)]*self\.login_button\b")

    def test_compare_button_follows_the_login_state(self):
        """Ohne Zugang wartet der Hauptknopf, mit Zugang ist er bereit."""
        update_status = self._update_status()
        waiting = self._dashboard("")
        update_status(waiting)
        self.assertEqual("Vergleichen und übernehmen …",
                         waiting.primary.text_value)
        self.assertFalse(waiting.primary.enabled)
        self.assertIn("anmelden", waiting.primary.tooltip)
        self.assertIn("Noch nicht angemeldet", waiting.status.text_value)
        ready = self._dashboard("token-1")
        update_status(ready)
        self.assertEqual("Vergleichen und übernehmen …",
                         ready.primary.text_value)
        self.assertTrue(ready.primary.enabled)
        self.assertIn("Angemeldet", ready.status.text_value)

    def test_a_running_sync_turns_the_button_into_cancel(self):
        update_status = self._update_status()
        dashboard = self._dashboard("token-1", running=True)
        update_status(dashboard)
        self.assertEqual("Abbrechen", dashboard.primary.text_value)
        self.assertTrue(dashboard.primary.enabled)
        self.assertTrue(dashboard.progress.visible)
        self.assertIn("Synchronisierung läuft", dashboard.status.text_value)


class ReadStateComparisonTests(unittest.TestCase):
    """Der Bestandsvergleich bekommt die Gelesen-Marken der Cloud und
    belegt damit die Spalte «Gelesen» vor (0.15.0)."""

    BOOK = {"uuid": "u1", "title": "Der Fall Collini",
            "authors": "Ferdinand von Schirach", "isbn": "1",
            "formats": ["EPUB"], "last_modified": "1",
            "format_signatures": {"EPUB": "1:1"}}

    def _rows(self, read_ids):
        from .sync import compare_inventory
        inventory = [{"deliverableId": "t-1",
                      "epubMetaData": {"title": "Der Fall Collini",
                                       "author": [{"name": "F. v. Schirach"}]}},
                     {"deliverableId": "t-9",
                      "epubMetaData": {"title": "Die Bucht",
                                       "author": [{"name": "Unbekannt"}]}}]
        state = {"u1": {"tolino_id": "t-1"}}
        return compare_inventory({1: self.BOOK}, state, inventory, ["EPUB"],
                                 read_ids=read_ids)

    def test_matching_book_carries_the_cloud_read_state(self):
        rows = self._rows({"t-1"})
        matched = next(row for row in rows if row["book_id"] == 1)
        self.assertTrue(matched["read_in_cloud"])
        self.assertTrue(matched["read"])
        self.assertEqual("t-1", matched["tolino_id"])

    def test_cloud_only_book_carries_the_read_state_too(self):
        rows = self._rows({"t-9"})
        only_cloud = next(row for row in rows if row["tolino_id"] == "t-9")
        self.assertEqual("only_tolino", only_cloud["status"])
        self.assertTrue(only_cloud["read_in_cloud"])
        self.assertTrue(only_cloud["read"])

    def test_without_read_ids_nothing_is_prechecked(self):
        for row in self._rows(()):
            self.assertFalse(row["read_in_cloud"])
            self.assertFalse(row["read"])


class SyncDataResponseShapeTests(unittest.TestCase):
    """Die sync-data-Antwort des Servers trägt die Marken-ID im Pfad.

    Gelesen-Marken und Sammlungen kommen als
    ``/publications/<Buch-ID>/tags/<Marken-ID>`` zurück – die Marken-ID
    vergibt der Server. Der Plugin schrieb nur die Form ohne Marken-ID und
    verlangte beim Lesen genau vier Pfadteile: damit war jede Server-Marke
    unsichtbar. Folge: die Spalte «Gelesen» blieb leer, obwohl die Cloud die
    Bücher als gelesen führte, und jede Serie schien bei jedem Lauf zu
    fehlen (0.15.1).
    """

    PUB = "bosh_8_543897185873248682836029182"
    READ = {"op": "replace",
            "path": "/publications/%s/tags/6438286" % PUB,
            "value": {"revision": "1-abc", "modified": 17,
                      "name": "collection_finished_readings_name",
                      "category": "system"}}
    SERIES = {"op": "replace",
              "path": "/publications/%s/tags/6438287" % PUB,
              "value": {"revision": "1-def", "modified": 18,
                        "name": "Brunetti", "category": "collection"}}
    OWN_ADD = {"op": "add", "path": "/publications/%s/tags" % PUB,
               "value": {"modified": 19, "name": "SciFi",
                         "category": "collection"}}

    def _client(self, patches, revision="42"):
        """Client mit echter Antwortform als Lesegrundlage."""
        client = TolinoClient(4, "hardware", "refresh-token")
        calls = []

        def fake_request(url, method="GET", data=None, form=False,
                         authenticated=True, content_type=None,
                         _auth_retry=True, _hw_retry=True,
                         _waf_rounds=0):
            calls.append({"url": url, "method": method, "data": data})
            return {"revision": revision, "patches": list(patches)}

        client._request = fake_request
        return client, calls

    def test_read_ids_understands_the_server_path_with_tag_id(self):
        client, calls = self._client([self.READ, self.OWN_ADD])
        self.assertEqual({self.PUB}, client.read_ids())
        self.assertEqual(1, len(calls))

    def test_collection_pairs_understands_the_server_path_with_tag_id(self):
        client, _ = self._client([self.SERIES, self.OWN_ADD])
        self.assertEqual({(self.PUB, "Brunetti"), (self.PUB, "SciFi")},
                         client.collection_pairs())

    def test_read_state_and_collections_come_from_one_read(self):
        client, calls = self._client([self.READ, self.SERIES])
        self.assertEqual({self.PUB}, client.read_ids())
        self.assertEqual({(self.PUB, "Brunetti")}, client.collection_pairs())
        self.assertEqual(1, len(calls))

    def test_remove_with_tag_id_takes_the_book_out_again(self):
        removed = {"op": "remove",
                   "path": "/publications/%s/tags/6438286" % self.PUB,
                   "value": {"revision": "1-abc",
                             "name": "collection_finished_readings_name",
                             "category": "system"}}
        client, _ = self._client([self.READ, removed])
        self.assertEqual(set(), client.read_ids())

    def test_foreign_paths_and_malformed_patches_are_ignored(self):
        patches = [
            {"op": "replace", "path": "/publications/%s/bookmark/12" % self.PUB,
             "value": {"progress": 0.4}},
            {"op": "replace", "path": "/publications//tags/1",
             "value": {"name": "collection_finished_readings_name",
                       "category": "system"}},
            {"op": "replace", "path": "/publications/%s/tags/1" % self.PUB},
            "kein dict",
        ]
        client, _ = self._client(patches)
        self.assertEqual(set(), client.read_ids())
        self.assertEqual(set(), client.collection_pairs())

    def test_mark_read_many_skips_books_the_server_already_calls_read(self):
        """Die Server-Form wird erkannt: eine vorhandene Marke wird nicht
        noch einmal geschrieben."""
        client, calls = self._client([self.READ], revision="43")
        self.assertEqual("43", client.mark_read_many([self.PUB]))
        self.assertEqual(1, len(calls))

    def test_mark_read_uses_the_revision_of_a_real_response(self):
        client, calls = self._client([self.SERIES], revision="77")
        client.mark_read_many(["neues-buch"])
        self.assertEqual(2, len(calls))
        self.assertEqual("77", calls[1]["data"]["revision"])
        self.assertEqual("/publications/neues-buch/tags",
                         calls[1]["data"]["patches"][0]["path"])

    def test_cloud_read_state_reaches_the_comparison_rows(self):
        """Von der Server-Antwort bis zur angehakten Spalte – der Fall aus
        dem Feldbericht (0.15.1)."""
        client, _ = self._client([self.READ])
        book = {"uuid": "u1", "title": "Feuerprobe", "authors": "Donna Leon",
                "isbn": "", "formats": ["EPUB"], "last_modified": "1",
                "format_signatures": {"EPUB": "1:1"}}
        inventory = [{"deliverableId": self.PUB,
                      "epubMetaData": {"title": "Feuerprobe",
                                       "author": [{"name": "Donna Leon"}]}}]
        rows = compare_inventory({1: book}, {"u1": {"tolino_id": self.PUB}},
                                 inventory, ["EPUB"],
                                 read_ids=client.read_ids())
        row = rows[0]
        self.assertTrue(row["read_in_cloud"])
        self.assertTrue(row["read"])
        self.assertEqual(self.PUB, row["tolino_id"])


class SyncDataDiagnosticTests(unittest.TestCase):
    """Der Diagnose-Bericht zeigt, was die sync-data wirklich liefert.

    Ohne diesen Block war dem Feldbericht nicht anzusehen, dass der Server
    Marken mit Marken-ID im Pfad schickt – und damit auch nicht, warum die
    Spalte «Gelesen» leer blieb (0.15.1).
    """

    PUB = "bosh_8_543897185873248682836029182"

    @staticmethod
    def _ui():
        class Icon:
            def isNull(self):
                return False

        return ToolbarIconTests._load_ui_module(Icon())

    @staticmethod
    def _client(patches):
        client = TolinoClient(4, "hardware", "refresh-token")

        def fake_request(url, method="GET", data=None, form=False,
                         authenticated=True, content_type=None,
                         _auth_retry=True, _hw_retry=True,
                         _waf_rounds=0):
            return {"revision": "42", "patches": list(patches)}

        client._request = fake_request
        return client

    def test_summary_counts_collections_and_read_marks(self):
        patches = [
            {"op": "replace",
             "path": "/publications/%s/tags/6438286" % self.PUB,
             "value": {"revision": "1-abc", "modified": 17,
                       "name": "collection_finished_readings_name",
                       "category": "system"}},
            {"op": "replace",
             "path": "/publications/%s/tags/6438287" % self.PUB,
             "value": {"revision": "1-def", "modified": 18,
                       "name": "Brunetti", "category": "collection"}},
        ]
        ui_module = self._ui()
        try:
            summary = ui_module.DiagnosticDialog._sync_data_summary(
                self._client(patches), ("refresh-token",))
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertTrue(summary["revision_present"])
        self.assertEqual(2, summary["patch_count"])
        self.assertEqual(1, summary["read_marks"])
        self.assertEqual(1, summary["collections"])
        self.assertIn("system", summary["tag_categories"])
        self.assertTrue(summary["sample_paths"][0].startswith("/publications/"))

    def test_summary_reports_a_failed_read_without_raising(self):
        class Broken:
            access = "access-secret"

            def sync_snapshot(self):
                raise RuntimeError("Netz weg")

        ui_module = self._ui()
        try:
            summary = ui_module.DiagnosticDialog._sync_data_summary(
                Broken(), ("refresh-token",))
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual("RuntimeError", summary["error_type"])
        self.assertNotIn("access-secret", summary["error_message"])


class ComparisonSortTests(unittest.TestCase):
    """Bestandsvergleich (0.9.38): Upload-Auswahl gruppiert oben, dann
    alphabetisch nach Titel und Autoren."""

    def test_upload_group_first_then_title_then_authors(self):
        from .sync import sort_comparison_rows
        rows = [
            {"selected": False, "title": "Alpha", "authors": "A", "book_id": 1},
            {"selected": True, "title": "Zulu", "authors": "Z", "book_id": 2},
            {"selected": False, "title": "beta", "authors": "B", "book_id": 3},
            {"selected": True, "title": "alpha", "authors": "M", "book_id": 4},
        ]
        ordered = sort_comparison_rows(rows)
        self.assertEqual([4, 2, 1, 3],
                         [row["book_id"] for row in ordered])
        # Eingabeliste bleibt unverändert.
        self.assertEqual([1, 2, 3, 4], [row["book_id"] for row in rows])

    def test_missing_fields_do_not_crash(self):
        from .sync import sort_comparison_rows
        rows = [{"book_id": None, "tolino_id": "cloud-only"},
                {"selected": True, "title": "Titel", "book_id": 7}]
        ordered = sort_comparison_rows(rows)
        self.assertEqual([7, None], [row["book_id"] for row in ordered])
        self.assertEqual("cloud-only", ordered[1]["tolino_id"])


class BoshDeviceRecoveryTests(unittest.TestCase):
    """0.9.35: Ein authentifizierter BOSH-400 (leeres "{}") loest eine
    Geraete-Recovery aus: das registrierte Geraet des Kontos wird
    uebernommen, sonst wird unsere Hardware-ID per registerhw
    registriert – dann genau EIN Retry.
    """

    INVENTORY_BODY = b'{"PublicationInventory": {"edata": []}}'

    def _client(self, hardware="configured-hw"):
        from .tolino import TolinoClient
        client = TolinoClient(4, hardware, "refresh-token-value")
        client.access = "access-value"
        client.expires_at = time.time() + 3600
        return client

    def _response(self, body, status=200):
        outer_body, outer_status = body, status

        class Response(object):
            status = outer_status

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return outer_body
        return Response()

    def _error400(self, body):
        from urllib.error import HTTPError
        error = HTTPError("https://bosh.pageplace.de/bosh/rest/x", 400,
                          "Bad Request", {}, None)
        error.read = lambda: body
        return error

    def test_error_text_surfaces_response_info_message(self):
        """BOSH meldet Fehler als ResponseInfo.message – ohne diesen
        Schluessel zeigte die Meldung nur ein nutzloses "{}"."""
        from .tolino import TolinoApiError
        client = self._client()

        def fake_urlopen(request, timeout=None):
            raise self._error400(
                b'{"ResponseInfo": {"message": "hardware not registered"}}')

        with patch("calibre_plugin.tolino.urlopen", fake_urlopen), \
             patch("calibre_plugin.tolino.TolinoClient.fetch_hardware_id",
                   side_effect=Exception("no devices")), \
             patch("calibre_plugin.tolino.TolinoClient._register_hardware",
                   return_value=False):
            with self.assertRaises(TolinoApiError) as ctx:
                client.inventory()
        self.assertIn("hardware not registered", str(ctx.exception))
        self.assertIn("hardware not registered", client.last_error_text)

    def test_inventory_adopts_registered_device_and_retries(self):
        """Ein fremdes, konfiguriertes Geraet wird durch das
        registrierte des Kontos ersetzt, danach folgt GENAU ein Retry."""
        seen = []
        adopted = []

        def fake_urlopen(request, timeout=None):
            seen.append(request.full_url)
            if len(seen) == 1:
                raise self._error400(b"{}")
            return self._response(self.INVENTORY_BODY)

        client = self._client("stale-hw-000")
        client.hardware_callback = adopted.append
        with patch("calibre_plugin.tolino.urlopen", fake_urlopen), \
             patch("calibre_plugin.tolino.TolinoClient.fetch_hardware_id",
                   return_value="eb22e4cf-bb01-4550-bff7-334446bb20b1"), \
             patch("calibre_plugin.tolino.TolinoClient._register_hardware",
                   side_effect=AssertionError("register must not run")):
            data = client.inventory()
        self.assertEqual([], data)
        self.assertEqual(2, len(seen))
        self.assertEqual(seen[0], seen[1])
        self.assertEqual("eb22e4cf-bb01-4550-bff7-334446bb20b1",
                         client.hardware)
        self.assertEqual(["eb22e4cf-bb01-4550-bff7-334446bb20b1"], adopted)

    def test_inventory_registers_hardware_when_device_list_unavailable(self):
        """Ohne Geraeteliste wird die eigene Hardware-ID per registerhw
        registriert (wie die Referenz-Clients), dann der Retry."""
        seen = []

        def fake_urlopen(request, timeout=None):
            seen.append((request.full_url,
                         {key.casefold(): value
                          for key, value in request.headers.items()}))
            if "registerhw" in request.full_url:
                return self._response(b"{}")
            inventory_calls = len([1 for url, _ in seen
                                   if "inventory" in url])
            if inventory_calls == 1:
                raise self._error400(b"{}")
            return self._response(self.INVENTORY_BODY)

        client = self._client("configured-hw")
        with patch("calibre_plugin.tolino.urlopen", fake_urlopen), \
             patch("calibre_plugin.tolino.TolinoClient.fetch_hardware_id",
                   side_effect=Exception("no devices")), \
             patch("calibre_plugin.tolino.time.sleep"):
            data = client.inventory()
        self.assertEqual([], data)
        self.assertEqual(3, len(seen))
        self.assertIn("/inventory/delta", seen[0][0])
        self.assertIn("v2/registerhw", seen[1][0])
        self.assertIn("/inventory/delta", seen[2][0])
        register_headers = seen[1][1]
        self.assertEqual("HTML5", register_headers.get("hardware_type"))
        self.assertEqual("TOLINO_WEBREADER",
                         register_headers.get("client_type"))
        self.assertEqual("configured-hw", client.hardware)

    def test_registered_current_device_raises_without_extra_calls(self):
        """Ist das konfigurierte Geraet bereits das registrierte, kommt
        kein Retry und kein registerhw – der echte Fehler 400 (andere
        Ursache) bleibt sichtbar."""
        from .tolino import TolinoApiError
        client = self._client("configured-hw")
        seen = []

        def fake_urlopen(request, timeout=None):
            seen.append(request.full_url)
            raise self._error400(b"{}")

        with patch("calibre_plugin.tolino.urlopen", fake_urlopen), \
             patch("calibre_plugin.tolino.TolinoClient.fetch_hardware_id",
                   return_value="configured-hw"), \
             patch("calibre_plugin.tolino.TolinoClient._register_hardware",
                   side_effect=AssertionError("register must not run")):
            with self.assertRaises(TolinoApiError):
                client.inventory()
        self.assertEqual(1, len(seen))

    def test_registration_settles_with_waiting_retries(self):
        """0.9.36-Feldbefund: Der Diagnose-Test direkt nach registerhw
        warf noch 400 (die ID war noch nicht angekommen), der naechste
        Klick Sekunden spaeter lief. Der erste Retry wartet jetzt kurz,
        und es gibt GENAU EINEN zweiten Versuch danach."""
        seen = []
        sleeps = []

        def fake_urlopen(request, timeout=None):
            seen.append(request.full_url)
            if "registerhw" in request.full_url:
                return self._response(b"{}")
            inventory_calls = len([1 for url in seen if "inventory" in url])
            if inventory_calls <= 2:
                raise self._error400(b"{}")
            return self._response(self.INVENTORY_BODY)

        client = self._client("configured-hw")
        with patch("calibre_plugin.tolino.urlopen", fake_urlopen), \
             patch("calibre_plugin.tolino.TolinoClient.fetch_hardware_id",
                   side_effect=Exception("no devices")), \
             patch("calibre_plugin.tolino.time.sleep",
                   side_effect=sleeps.append):
            data = client.inventory()
        self.assertEqual([], data)
        self.assertEqual(4, len(seen))
        self.assertIn("/inventory/delta", seen[0])
        self.assertIn("v2/registerhw", seen[1])
        self.assertIn("/inventory/delta", seen[2])
        self.assertIn("/inventory/delta", seen[3])
        self.assertEqual([1.5, 1.5], sleeps)
        # Danach ist die ID bekannt: der naechste Lauf braucht keine
        # Recovery (Flag ist verbraucht).
        self.assertFalse(client._hw_registered_now)

    def test_adopted_device_needs_no_settle_wait(self):
        """Uebernommenes, registriertes Geraet: kein Warten, weiterhin
        genau ein direkter Retry."""
        seen = []
        sleeps = []

        def fake_urlopen(request, timeout=None):
            seen.append(request.full_url)
            if len(seen) == 1:
                raise self._error400(b"{}")
            return self._response(self.INVENTORY_BODY)

        client = self._client("stale-hw-000")
        with patch("calibre_plugin.tolino.urlopen", fake_urlopen), \
             patch("calibre_plugin.tolino.TolinoClient.fetch_hardware_id",
                   return_value="eb22e4cf-bb01-4550-bff7-334446bb20b1"), \
             patch("calibre_plugin.tolino.TolinoClient._register_hardware",
                   side_effect=AssertionError("register must not run")), \
             patch("calibre_plugin.tolino.time.sleep",
                   side_effect=sleeps.append):
            data = client.inventory()
        self.assertEqual([], data)
        self.assertEqual(2, len(seen))
        self.assertEqual([], sleeps)
        self.assertFalse(client._hw_registered_now)


class SharedWebreaderPartnerTests(unittest.TestCase):
    """Alle Shops mit dem gemeinsamen Web Reader verhalten sich wie
    Orell Fuessli – ein Profil statt pro Partner nachgebauter
    Einzelheiten.
    """

    SHARED = (1, 2, 4, 5, 6, 7, 8)

    def test_shared_reader_covers_every_shop(self):
        from .tolino import uses_shared_webreader
        for pid in self.SHARED:
            partner = PARTNERS[pid]
            self.assertIn("webreader.mytolino.com", partner["reader_url"], pid)
            self.assertTrue(uses_shared_webreader(partner), pid)
        # Buch.de (reseller 6) ist eingestellt, hat keine Endpunkte und
        # faellt nicht in das Profil -- der klare Fehler bleibt.
        self.assertFalse(uses_shared_webreader(PARTNERS[3]))

    def test_hugendubel_reader_is_the_shared_reader(self):
        # webreader.hugendubel.de leitet 301 auf webreader.mytolino.com
        # (reseller=13&autologin=true) -- ab jetzt direkt dorthin.
        self.assertEqual(PARTNERS[4]["reader_url"],
                         PARTNERS[5]["reader_url"])

    def test_ebook_de_partner_matches_reference_config(self):
        partner = PARTNERS[8]
        self.assertEqual("eBook.de", partner["name"])
        self.assertEqual("81", partner["reseller_id"])
        self.assertEqual("ebookde0501html5readerV0001", partner["client_id"])
        self.assertEqual("e-publishing", partner["scope"])
        self.assertEqual("https://www.ebook.de/oauth/token",
                         partner["token_url"])
        self.assertEqual("https://www.ebook.de/oauth/authorize",
                         partner["auth_url"])
        self.assertEqual(8, force_legacy_partner_id(81))
        self.assertEqual(8, resolve_partner_id(81))

    def test_corrected_endpoints_match_reader_config(self):
        self.assertEqual("https://www.hugendubel.de/oauth/token",
                         PARTNERS[5]["token_url"])
        self.assertEqual("webreader", PARTNERS[7]["client_id"])
        self.assertEqual("SCOPE_BOSH", PARTNERS[7]["scope"])
        self.assertEqual("https://www.buecher.de/auth/oauth2/token",
                         PARTNERS[7]["token_url"])
        self.assertEqual("webreader", PARTNERS[2]["client_id"])
        self.assertEqual("https://www.thalia.at/auth/oauth2/token",
                         PARTNERS[2]["token_url"])

    def test_token_post_carries_reader_origin(self):
        """Der Token-POST sieht bei allen Shops aus wie der des Readers."""
        for pid in (1, 5, 8):
            captured = {}

            class Response:
                status = 200

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self):
                    return b'{"access_token":"a","expires_in":3600}'

            def request(request, timeout):
                captured.update(
                    {key.casefold(): value
                     for key, value in request.headers.items()})
                return Response()

            with patch("calibre_plugin.tolino._curl_binary",
                       return_value=None), \
                    patch("calibre_plugin.tolino._impersonate_session",
                          return_value=None), \
                    patch("calibre_plugin.tolino.urlopen", request):
                TolinoClient(pid, "3xxA-00BCD-EFGHI-JKLMN-OPQRh",
                             "refresh-token").login()
            self.assertEqual("https://webreader.mytolino.com",
                             captured.get("origin"), pid)
            self.assertEqual("https://webreader.mytolino.com/",
                             captured.get("referer"), pid)

    def test_authenticated_requests_send_webreader_client(self):
        for pid in (1, 5):
            captured = {}

            class Response:
                status = 200

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self):
                    return b"{}"

            def request(request, timeout):
                captured.update(
                    {key.casefold(): value
                     for key, value in request.headers.items()})
                return Response()

            with patch("calibre_plugin.tolino._curl_binary",
                       return_value=None), \
                    patch("calibre_plugin.tolino._impersonate_session",
                          return_value=None), \
                    patch("calibre_plugin.tolino.urlopen", request):
                client = TolinoClient(pid, "3xxA-00BCD-EFGHI-JKLMN-OPQRh",
                                      "refresh-token")
                client.access = "access-token"
                client.expires_at = time.time() + 60
                client._request(
                    "https://bosh.pageplace.de/bosh/rest/inventory/delta")
            self.assertEqual("TOLINO_WEBREADER",
                             captured.get("client_type"), pid)
            self.assertEqual("5.2.0", captured.get("client_version"), pid)

    def test_browser_login_never_falls_back_to_system_browser(self):
        """Shops auf dem gemeinsamen Reader: fehlt Chromium, wird der
        Fehler sichtbar – der System-Browser wird NICHT geoeffnet."""
        for pid in (1, 5, 8):
            opened = []
            with patch("calibre_plugin.tolino.webbrowser.open",
                       side_effect=lambda url: opened.append(url) or True):
                with self.assertRaises(TolinoAuthError) as ctx:
                    browser_login(pid, "test_hardware")
            self.assertIn("Kein Chromium-Browser gefunden",
                          str(ctx.exception))
            self.assertEqual([], opened, pid)


class AccountStateBindingTests(unittest.TestCase):
    """Das im Dialog angezeigte Konto IST das Konto, dessen State
    gelesen und geschrieben wird – nur eine Quelle je Konto, und die
    Tolino-ID-Spalte folgt EINER Regel (nur bei genau einem Konto)."""

    def test_account_by_name_ignores_the_active_account(self):
        accounts = [
            {"name": "alpha", "state": {"u1": {"tolino_id": "id-a"}}},
            {"name": "beta", "state": {"u2": {"tolino_id": "id-b"}}},
        ]
        self.assertEqual({"u1": {"tolino_id": "id-a"}},
                         config.account_by_name("alpha", accounts)["state"])
        self.assertEqual({"u2": {"tolino_id": "id-b"}},
                         config.account_by_name("beta", accounts)["state"])
        self.assertEqual({}, config.account_by_name("gone", accounts))

    def test_set_active_account_switches_without_touching_state(self):
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            config.settings()
            config.save_account(
                "alpha", {"state": {"u1": {"tolino_id": "id-a"}}},
                active="alpha")
            config.save_account(
                "beta", {"state": {"u2": {"tolino_id": "id-b"}}},
                active="beta")
            config.set_active_account("alpha")
            values = config.settings()
            self.assertEqual("alpha", values["active_account"])
            self.assertEqual({"u1": {"tolino_id": "id-a"}}, values["state"])
            # Das andere Konto bleibt unberuehrt.
            self.assertEqual({"u2": {"tolino_id": "id-b"}},
                             config.account_by_name("beta")["state"])
        finally:
            config.PREFERENCES = original

    def test_tolino_column_active_requires_single_account(self):
        from .sync import tolino_column_active

        class Db:
            field_metadata = {TOLINO_COLUMN: {}}

        class NoColumn:
            field_metadata = {}

        values = {"use_tolino_column": True,
                  "accounts": [{"name": "a"}, {"name": "b"}]}
        # Zwei Konten: die globale Spalte kann nur das zuletzt
        # synchronisierte Konto spiegeln -- weder lesen noch schreiben.
        self.assertFalse(tolino_column_active(values, Db()))
        values["accounts"] = [{"name": "a"}]
        self.assertTrue(tolino_column_active(values, Db()))
        values["use_tolino_column"] = False
        self.assertFalse(tolino_column_active(values, Db()))
        values["use_tolino_column"] = True
        self.assertFalse(tolino_column_active(values, NoColumn()))
        self.assertFalse(tolino_column_active({}, Db()))


class FlatpakSandboxBrowserOpenTests(unittest.TestCase):
    """Feldbefund 0.9.40: Pop!_OS, Calibre über Flathub – "öffnet sich
    kein browser". In der Flatpak-Sandbox sind die Browser des Rechners
    unsichtbar (kein Browser im PATH, kein flatpak, kein flatpak run);
    die Öffnerkette und die Fehlermeldungen müssen das offenlegen."""

    URL = "https://webreader.mytolino.com/library/"

    def test_open_system_browser_prefers_portal_tools_in_sandbox(self):
        """In der Sandbox startet die synchrone Kette zuerst xdg-open
        (Portal-Wrapper); das webbrowser-Modul läuft hier gar nicht an."""
        from .tolino import open_system_browser

        calls = []

        def fake_run(argv, timeout=20):
            calls.append(list(argv))
            return argv[0] == "xdg-open"

        with patch("calibre_plugin.tolino.in_flatpak_sandbox",
                   return_value=True), \
             patch("calibre_plugin.tolino._run_open_tool",
                   side_effect=fake_run), \
             patch("calibre_plugin.tolino.webbrowser.open",
                   side_effect=AssertionError("webbrowser.open called")):
            opened = open_system_browser(self.URL)
        self.assertTrue(opened)
        self.assertEqual(["xdg-open", self.URL], calls[0])

    def test_open_system_browser_falls_back_to_flatpak_spawn_host(self):
        """Ohne portal-tauglichen Öffner endet die Kette bei
        `flatpak-spawn --host xdg-open` (Start ausserhalb der Sandbox,
        braucht die talk-name-Freigabe, deren Befehl der Fehler nennt)."""
        from .tolino import open_system_browser

        calls = []

        def fake_run(argv, timeout=20):
            calls.append(list(argv))
            return argv[:2] == ["flatpak-spawn", "--host"]

        with patch("calibre_plugin.tolino.in_flatpak_sandbox",
                   return_value=True), \
             patch("calibre_plugin.tolino._run_open_tool",
                   side_effect=fake_run), \
             patch("calibre_plugin.tolino.shutil.which",
                   side_effect=lambda name: "/usr/bin/flatpak-spawn"
                   if name == "flatpak-spawn" else None), \
             patch("calibre_plugin.tolino.webbrowser.open",
                   return_value=False):
            opened = open_system_browser(self.URL)
        self.assertTrue(opened)
        self.assertEqual(["flatpak-spawn", "--host", "xdg-open", self.URL],
                         calls[-1])

    def test_outside_sandbox_the_webbrowser_module_decides(self):
        """Ohne Sandbox bleibt das webbrowser-Modul der Weg – die
        Öffnerkette wird nicht angerissen. Öffnet es nichts, bleibt es
        False; die Meldung kommt wie bisher vom Aufrufer."""
        from .tolino import open_system_browser

        with patch("calibre_plugin.tolino.in_flatpak_sandbox",
                   return_value=False), \
             patch("calibre_plugin.tolino._run_open_tool",
                   side_effect=AssertionError("open chain called")):
            with patch("calibre_plugin.tolino.webbrowser.open",
                       return_value=True):
                self.assertTrue(open_system_browser(self.URL))
            with patch("calibre_plugin.tolino.webbrowser.open",
                       return_value=False):
                self.assertFalse(open_system_browser(self.URL))


    def test_launch_reader_window_without_sandbox_has_no_permission_hint(self):
        """Ausserhalb einer Sandbox nennt dieselbe Meldung KEINEN
        Flatpak-Befehl (sonst führt sie in die Irre)."""
        from unittest.mock import patch as _patch
        from . import cdp as cdp_module

        with _patch.object(cdp_module, "pick_chromium", return_value=None), \
             _patch.object(cdp_module, "_host_probe_status", ""), \
             _patch.object(cdp_module, "in_flatpak_sandbox",
                           return_value=False):
            with self.assertRaises(TolinoAuthError) as ctx:
                cdp_module.launch_reader_window(4)
        message = str(ctx.exception)
        self.assertIn("Kein Chromium-Browser gefunden", message)
        self.assertNotIn("flatpak override", message)

    def test_chromium_candidates_probe_host_browser_via_flatpak_spawn(self):
        """Die Sandbox-Sonde liest native Host-Pfade und Host-Flatpak-
        Apps und baut Launcher mit `flatpak-spawn --host` als Präfix."""
        from unittest.mock import patch as _patch
        from . import cdp as cdp_module

        class FakeProc(object):
            returncode = 0
            stdout = ("NATIVE /usr/bin/chromium\n"
                      "FLATPAK org.chromium.Chromium\n")

        with _patch.object(cdp_module, "in_flatpak_sandbox",
                           return_value=True), \
             _patch("shutil.which",
                    side_effect=lambda name: "/usr/bin/flatpak-spawn"
                    if name == "flatpak-spawn" else None), \
             _patch.object(cdp_module.subprocess, "run",
                           return_value=FakeProc()):
            found = cdp_module.chromium_candidates(home="/nonexistent-home")
        argvs = [argv for argv, _label in found]
        self.assertIn(["/usr/bin/flatpak-spawn", "--host",
                       "/usr/bin/chromium"], argvs)
        self.assertIn(["/usr/bin/flatpak-spawn", "--host", "flatpak", "run",
                       "--command=chrome", "org.chromium.Chromium"], argvs)
        labels = [label for _argv, label in found]
        self.assertIn("chromium (Host)", labels)
        self.assertIn("Chromium (Flatpak, Host)", labels)


    def test_chromium_candidates_never_probe_outside_sandbox(self):
        """Ausserhalb einer Sandbox wird für die Host-Sonde nie ein
        Prozess gestartet."""
        from unittest.mock import patch as _patch
        from . import cdp as cdp_module

        with _patch.object(cdp_module, "in_flatpak_sandbox",
                           return_value=False), \
             _patch.object(cdp_module, "_EXTRA_BROWSER_PATHS", ()), \
             _patch("shutil.which", return_value=None), \
             _patch.object(cdp_module.subprocess, "run",
                           side_effect=AssertionError("probe started")):
            found = cdp_module.chromium_candidates(home="/nonexistent-home")
        self.assertEqual([], found)


class ExtractOpensOwnChromiumWindowTests(unittest.TestCase):
    """Feldbefund 0.9.41: trotz Standardbrowser Firefox soll der
    Extract-Button („Token aus laufendem Web Reader übernehmen") ein
    eigenes Chromium-Fenster öffnen, live lesen wie der Grabber und
    NUR dieses Fenster schliessen – nie den gesamten Browser."""

    def test_chromium_available_tracks_pick_chromium(self):
        from .tolino import chromium_available

        with patch("calibre_plugin.cdp.pick_chromium",
                   return_value=("/usr/bin/chromium", "Chromium")):
            self.assertTrue(chromium_available())
        with patch("calibre_plugin.cdp.pick_chromium", return_value=None):
            self.assertFalse(chromium_available())
        with patch("calibre_plugin.cdp.pick_chromium",
                   side_effect=OSError("boom")):
            self.assertFalse(chromium_available())


class ChromiumOpensRegardlessOfDefaultTests(unittest.TestCase):
    """Feldbefund 0.9.42: "es öffnet weiterhin den Firefox … in 0.9.39
    funktionierte es mit diesem Chromium browser. Wenn der User eine
    Chromium-Variante installiert hat, soll sich diese öffnen, egal
    welchen Standardbrowser der User hat."""

    def test_chromium_candidates_finds_extra_binary_names(self):
        """Auch `chrome`, Beta/Dev-Varianten etc. werden als Befehlsname
        erkannt – nicht nur die klassischen zehn."""
        from . import cdp as cdp_module

        with tempfile.TemporaryDirectory() as home:
            with patch("shutil.which",
                       side_effect=lambda name: "/usr/bin/%s" % name
                       if name == "chrome" else None):
                found = cdp_module.chromium_candidates(home=home)
        argvs = [argv for argv, _label in found]
        self.assertIn(["/usr/bin/chrome"], argvs)

    def test_chromium_candidates_falls_back_to_known_paths(self):
        """Fest verdrahtete Installationsorte finden auch dann eine
        installierte Chromium-Variante, wenn Calibre mit reduziertem
        PATH startet (Desktop-Start)."""
        from . import cdp as cdp_module

        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "chromium-bin")
            with open(path, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(path, 0o755)
            with patch.object(cdp_module, "_EXTRA_BROWSER_PATHS", (path,)), \
                 patch.object(cdp_module, "_host_probe_status", ""), \
                 patch("shutil.which", return_value=None):
                found = cdp_module.chromium_candidates(home=home)
        self.assertIn([path], [argv for argv, _label in found])
        self.assertIn("Chromium", [label for _argv, label in found])


    def test_host_probe_status_empty_when_host_has_no_browser(self):
        """Sonde erlaubt (rc 0), aber der Host hat keinen Chromium --
        Status "empty" fordert dann die Installation."""
        from . import cdp as cdp_module

        class EmptyProc(object):
            returncode = 0
            stdout = ""

        with patch.object(cdp_module, "in_flatpak_sandbox",
                          return_value=True), \
             patch.object(cdp_module, "_host_probe_status", ""), \
             patch("shutil.which",
                   side_effect=lambda name: "/usr/bin/flatpak-spawn"
                   if name == "flatpak-spawn" else None), \
             patch.object(cdp_module.subprocess, "run",
                          return_value=EmptyProc()):
            cdp_module.chromium_candidates(home="/nonexistent-home")
            self.assertEqual("empty", cdp_module._host_probe_status)

    def test_launch_names_installation_when_host_probe_empty(self):
        """Ist die Sonde durchgelaufen und hat nichts gefunden, nennt die
        Meldung die Installation statt des override-Befehls (der wäre
        überflüssig)."""
        from unittest.mock import patch as _patch
        from . import cdp as cdp_module

        with _patch.object(cdp_module, "pick_chromium", return_value=None), \
             _patch.object(cdp_module, "_host_probe_status", "empty"), \
             _patch.object(cdp_module, "in_flatpak_sandbox",
                           return_value=True):
            with self.assertRaises(TolinoAuthError) as ctx:
                cdp_module.launch_reader_window(4)
        message = str(ctx.exception)
        self.assertIn("installieren", message)
        self.assertNotIn("flatpak override", message)

    def test_launch_falls_through_to_next_candidate(self):
        """Ein defekter erster Kandidat (OSError beim Start) darf die
        Anmeldung nicht abwürgen: die nächste installierte
        Chromium-Variante wird gestartet."""
        import tempfile
        from unittest.mock import patch as _patch
        from . import cdp as cdp_module

        broken = (["/usr/bin/broken"], "Broken")
        working = (["/usr/bin/working"], "Working")
        spawned = {}

        class FakePopen(object):
            def __init__(self, args, **kwargs):
                if args[0] == "/usr/bin/broken":
                    raise OSError("exec format error")
                spawned["args"] = list(args)

        with tempfile.TemporaryDirectory() as home:
            with _patch.object(cdp_module, "pick_chromium",
                               return_value=broken), \
                 _patch.object(cdp_module, "chromium_options",
                               return_value=[broken, working]), \
                 _patch.object(cdp_module, "_host_probe_status", ""), \
                 _patch.object(cdp_module, "_clean_profile_dir"), \
                 _patch.object(cdp_module, "_profile_dir",
                               return_value=os.path.join(home, "prof")), \
                 _patch.object(cdp_module.subprocess, "Popen", FakePopen), \
                 _patch.object(cdp_module, "_http_get_json",
                               side_effect=[None, {"webSocketDebuggerUrl":
                                                   "ok"}]), \
                 _patch.object(cdp_module.time, "sleep"):
                _binary, port = cdp_module.launch_reader_window(4)
        self.assertEqual("/usr/bin/working", spawned["args"][0])
        self.assertIn("--remote-debugging-port=%d" % port, spawned["args"])
        self.assertIn("mytolino.com", spawned["args"][-1])

    def test_open_login_browser_prefers_installed_chromium(self):
        """Der Anmeldelink geht an die installierte Chromium-Variante --
        der Standardbrowser (Firefox) spielt keine Rolle."""
        from .tolino import open_login_browser

        spawned = {}

        class FakePopen(object):
            def __init__(self, args, **kwargs):
                spawned["args"] = list(args)

        with patch("calibre_plugin.cdp.pick_chromium",
                   return_value=(["/usr/bin/chromium"], "Chromium")), \
             patch.object(tolino_module.subprocess, "Popen", FakePopen), \
             patch("calibre_plugin.tolino.open_system_browser",
                   side_effect=AssertionError("system browser called")):
            opened = open_login_browser("https://example.invalid/auth")
        self.assertTrue(opened)
        self.assertEqual(["/usr/bin/chromium",
                          "https://example.invalid/auth"], spawned["args"])

    def test_open_login_browser_falls_back_without_chromium(self):
        """Ohne Chromium bleibt der System-Browser der Notfall."""
        from .tolino import open_login_browser

        with patch("calibre_plugin.cdp.pick_chromium", return_value=None), \
             patch("calibre_plugin.tolino.open_system_browser",
                   return_value=True) as system, \
             patch.object(tolino_module.subprocess, "Popen",
                          side_effect=AssertionError("Popen called")):
            opened = open_login_browser("https://example.invalid/auth")
        self.assertTrue(opened)
        system.assert_called_once_with("https://example.invalid/auth")


    def test_local_callback_opens_chromium_variant(self):
        """Der localhost-Callback-Weg öffnet den Auth-Link in der
        installierten Chromium-Variante, nie im Standardbrowser."""
        from .tolino import browser_login

        synthetic = {
            9: {"name": "Testladen ohne Reader",
                "key": 99,
                "reseller_id": "99",
                "client_id": "webshop01",
                "scope": "SCOPE_BOSH",
                "token_url": "https://example.invalid/oauth2/token",
                "auth_url": "https://example.invalid/oauth2/authorize"},
        }
        spawned = {}

        class FakePopen(object):
            def __init__(self, args, **kwargs):
                spawned["args"] = list(args)

        with patch.dict("calibre_plugin.tolino.PARTNERS", synthetic), \
             patch("calibre_plugin.cdp.pick_chromium",
                   return_value=(["/usr/bin/chromium"], "Chromium")), \
             patch.object(tolino_module.subprocess, "Popen", FakePopen), \
             patch("calibre_plugin.tolino.webbrowser.open",
                   side_effect=AssertionError("webbrowser.open called")):
            with self.assertRaises(TolinoAuthError):
                browser_login(9, "test_hardware", timeout=0.2)
        self.assertEqual("/usr/bin/chromium", spawned["args"][0])
        self.assertIn("https://example.invalid/oauth2/authorize",
                      spawned["args"][-1])
        self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1",
                      spawned["args"][-1])


class FlatpakHostProbeReportTests(unittest.TestCase):
    """Feldbefund 0.9.43: der override-Befehl warerteilt, die Meldung
    «kein Chromium-Browser … Freigabe erteilen» blieb trotzdem. Die
    Host-Sonde wertete den Exit-Code des sh-Skripts aus, dessen letzte
    flatpak-info-Prüfung auf fast jedem Rechner scheitert – sie
    meldete also IMMER «Freigabe fehlt» und warf die gefundenen
    Host-Browser weg.
    """

    def test_host_probe_keeps_results_when_script_exits_nonzero(self):
        """Ein Fehlercode MIT Ausgabe ist kein Freigabe-Verlust: die
        NATIVE-Zeilen werden gelesen, der Status wird «ok»."""
        from . import cdp as cdp_module

        class Proc(object):
            returncode = 1
            stdout = "NATIVE /usr/bin/chromium\n"

        with patch.object(cdp_module, "in_flatpak_sandbox",
                          return_value=True), \
             patch.object(cdp_module, "_host_probe_status", ""), \
             patch.object(cdp_module, "_EXTRA_BROWSER_PATHS", ()), \
             patch("shutil.which",
                   side_effect=lambda name: "/usr/bin/flatpak-spawn"
                   if name == "flatpak-spawn" else None), \
             patch.object(cdp_module.subprocess, "run",
                          return_value=Proc()):
            found = cdp_module.chromium_candidates(home="/nonexistent-home")
            # Status hier pruefen: der Patch stellt den alten Wert wieder
            # her, sobald der Block endet (Verhalten wie die
            # Bestandstests in ChromiumOpensRegardlessOfDefaultTests).
            self.assertEqual("ok", cdp_module._host_probe_status)
        argvs = [argv for argv, _label in found]
        self.assertIn(["/usr/bin/flatpak-spawn", "--host",
                       "/usr/bin/chromium"], argvs)

    def test_host_probe_script_forces_exit_zero(self):
        """Das Sonden-Skript endet mit `exit 0` – sonst meldet die
        Host-Schleife selbst dann einen Fehler, wenn die Freigabe
        erteilt ist (Regressionsschutz für den Feldbefund)."""
        from . import cdp as cdp_module

        captured = {}

        class Proc(object):
            returncode = 0
            stdout = ""

        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)
            return Proc()

        with patch.object(cdp_module, "in_flatpak_sandbox",
                          return_value=True), \
             patch.object(cdp_module, "_host_probe_status", ""), \
             patch("shutil.which",
                   side_effect=lambda name: "/usr/bin/flatpak-spawn"
                   if name == "flatpak-spawn" else None), \
             patch.object(cdp_module.subprocess, "run",
                          side_effect=fake_run):
            cdp_module.chromium_candidates(home="/nonexistent-home")
        script = captured["argv"][-1]
        self.assertTrue(script.rstrip().endswith("exit 0"), script)

    def test_denied_still_reported_without_any_output(self):
        """Ohne Ausgabe und mit Fehlercode bleibt «denied» die einzige
        plausible Deutung: flatpak-spawn wurde abgewiesen. Ob die Sonde
        gar nichts oder nur Leerraum liefert, ist dabei egal."""
        from . import cdp as cdp_module

        class Proc(object):
            returncode = 1
            stdout = "   "

        with patch.object(cdp_module, "in_flatpak_sandbox",
                          return_value=True), \
             patch.object(cdp_module, "_host_probe_status", ""), \
             patch.object(cdp_module, "_EXTRA_BROWSER_PATHS", ()), \
             patch("shutil.which",
                   side_effect=lambda name: "/usr/bin/flatpak-spawn"
                   if name == "flatpak-spawn" else None), \
             patch.object(cdp_module.subprocess, "run",
                          return_value=Proc()):
            found = cdp_module.chromium_candidates(home="/nonexistent-home")
            self.assertEqual("denied", cdp_module._host_probe_status)
        self.assertEqual([], found)

    def test_launch_permission_names_both_installations(self):
        """Die Meldung nennt beide Installationen (User-Befehl wie
        ausgeführt, System-Befehl mit sudo), den Nachweis über
        `flatpak info --show-permissions` und den vollständigen
        Neustart."""
        from unittest.mock import patch as _patch
        from . import cdp as cdp_module

        with _patch.object(cdp_module, "pick_chromium", return_value=None), \
             _patch.object(cdp_module, "_host_probe_status", "denied"), \
             _patch.object(cdp_module, "in_flatpak_sandbox",
                           return_value=True):
            with self.assertRaises(TolinoAuthError) as ctx:
                cdp_module.launch_reader_window(4)
        message = str(ctx.exception)
        self.assertIn("Kein Chromium-Browser gefunden", message)
        self.assertIn("flatpak override --user", message)
        self.assertIn("sudo flatpak override", message)
        self.assertIn("--show-permissions", message)
        self.assertIn("neu starten", message)

    def test_launch_names_runtime_update_when_spawn_missing(self):
        """Ohne flatpak-spawn hilft keine Freigabe – die Meldung
        nennt dann `flatpak update` statt des override-Befehls."""
        from unittest.mock import patch as _patch
        from . import cdp as cdp_module

        with _patch.object(cdp_module, "pick_chromium", return_value=None), \
             _patch.object(cdp_module, "_host_probe_status",
                           "unavailable"), \
             _patch.object(cdp_module, "in_flatpak_sandbox",
                           return_value=True):
            with self.assertRaises(TolinoAuthError) as ctx:
                cdp_module.launch_reader_window(4)
        message = str(ctx.exception)
        self.assertIn("flatpak update", message)
        self.assertNotIn("flatpak override", message)

    def test_open_error_names_both_installations_and_the_manual_way(self):
        """Der Öffner-Fehler nennt beide Installationen (User-Befehl wie
        ausgeführt, System-Befehl mit sudo), den Nachweis-Befehl und den
        manuellen Weg (0.9.40/0.9.43)."""
        from .tolino import _system_browser_open_error

        with patch("calibre_plugin.tolino.in_flatpak_sandbox",
                   return_value=True):
            message = _system_browser_open_error()
        self.assertIn("Der System-Browser konnte nicht geöffnet werden.",
                      message)
        self.assertIn("flatpak override --user", message)
        self.assertIn("sudo flatpak override", message)
        self.assertIn("org.freedesktop.Flatpak", message)
        self.assertIn("--show-permissions", message)
        self.assertIn("vollständig neu starten", message)
        self.assertIn("von Hand eintragen", message)


class SettingsLockTests(unittest.TestCase):
    """0.15.5: Der Einstellungs-Speicher wird aus zwei Threads benutzt.

    Der Dialog speichert im GUI-Thread, der Keep-alive rotiert den Token im
    Hintergrund. `save_account` liest UND schreibt den ganzen Konto-Block --
    ohne Lock kann ein Lesen-Aendern-Schreiben die Rotation des anderen
    Threads verlieren.
    """

    def setUp(self):
        self.original = config.PREFERENCES
        config.PREFERENCES = config.JSONConfig("test")
        config.PREFERENCES.update({
            "accounts": [{"name": "konto", "partner_id": 4,
                          "hardware_id": "hw", "refresh_token": "tok",
                          "username": "", "password": "", "state": {}}],
            "active_account": "konto",
        })
        self.addCleanup(setattr, config, "PREFERENCES", self.original)

    def test_save_account_waits_for_the_settings_lock(self):
        import threading
        started = threading.Event()
        done = threading.Event()

        def worker():
            started.set()
            config.save_account("zweites", {"hardware_id": "hw-2"})
            done.set()

        with config._SETTINGS_LOCK:
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(started.wait(5))
            # Solange der Lock haelt, kommt der Hintergrund-Thread nicht durch.
            self.assertFalse(done.wait(0.3))
        thread.join(5)
        # Nach dem Freigeben laeuft er durch.
        self.assertTrue(done.is_set())
        self.assertEqual("hw-2",
                         config.account_by_name("zweites")["hardware_id"])

    def test_settings_lock_is_reentrant(self):
        """save_account ruft intern settings() und save_account -- ohne
        Reentranz wuerde das Plugin in einen Deadlock laufen."""
        lock = config._SETTINGS_LOCK
        self.assertTrue(lock.acquire(blocking=False))
        try:
            self.assertTrue(lock.acquire(blocking=False))
            lock.release()
        finally:
            lock.release()

    def test_parallel_writes_never_lose_an_account(self):
        import threading
        errors = []

        def worker(index):
            try:
                name = "konto-%d" % index
                for _ in range(20):
                    config.save_account(name, {"hardware_id": "hw-%d" % index})
            except Exception as exc:  # pragma: no cover - nur Diagnose
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,))
                   for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual([], errors)
        names = {item["name"] for item in config.settings()["accounts"]}
        self.assertEqual({"konto"} | {"konto-%d" % index for index in range(8)},
                         names)


class KeepaliveSafetyTests(unittest.TestCase):
    """0.15.5: Keep-alive und Sync duerfen den Refresh-Token nie gleichzeitig
    einloesen -- der Reuse-Schutz von Keycloak beendet die Sitzung sonst
    sofort. Und ein Fehler darf nicht still verschwinden."""

    def test_keepalive_stands_still_while_a_sync_runs(self):
        from . import tolino as tolino_module
        self.addCleanup(tolino_module.mark_sync_active, False)
        tolino_module.mark_sync_active(True)
        with patch("calibre_plugin.tolino.TolinoClient") as client_mock:
            result = tolino_module.keepalive_refresh(
                lambda: {"refresh_token": "tok"}, None)
        self.assertIsNone(result)
        client_mock.assert_not_called()
        # Nach dem Sync rotiert er wieder.
        tolino_module.mark_sync_active(False)
        with patch("calibre_plugin.tolino.TolinoClient") as client_mock:
            client_mock.return_value.refresh = "rotiert"
            self.assertEqual("rotiert", tolino_module.keepalive_refresh(
                lambda: {"refresh_token": "tok"}, None))
        client_mock.assert_called_once()

    def test_keepalive_failure_is_logged_without_the_token(self):
        from . import tolino as tolino_module
        logged = []

        class BoomClient(object):
            def __init__(self, *args, **kwargs):
                pass

            def login(self):
                raise RuntimeError("kaputt mit sk-geheim-token")

        with patch.object(tolino_module, "log_warning", logged.append), \
                patch("calibre_plugin.tolino.TolinoClient", BoomClient):
            result = tolino_module.keepalive_refresh(
                lambda: {"partner_id": 4, "hardware_id": "hw",
                         "refresh_token": "sk-geheim-token"}, None)
        self.assertIsNone(result)
        self.assertEqual(1, len(logged))
        self.assertIn("Keep-alive", logged[0])
        self.assertNotIn("sk-geheim-token", logged[0])

    def test_log_warning_never_raises_without_calibre(self):
        from .tolino import log_warning
        log_warning("nur ein Test -- ohne Calibre muss das still bleiben")


class SyncKeepsKeepaliveQuietTests(unittest.TestCase):
    """0.15.5: Der Sync markiert sich als aktiv -- waehrend er laeuft,
    loest der Keep-alive keinen Token ein (und danach wieder)."""

    def test_worker_marks_the_sync_active_for_its_whole_run(self):
        from . import tolino as tolino_module
        ui_module = ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None,
            qt_stub={"pyqtSignal":
                     CollectionReconcileWorkerTests._signal_descriptor()[0]})
        self.addCleanup(tolino_module.mark_sync_active, False)
        seen = []

        class FakeClient(object):
            def __init__(self, *args, **kwargs):
                self.refresh = "rotated"

            def login(self):
                # Im laufenden Sync ist der Keep-alive schon gesperrt.
                seen.append(tolino_module._SYNC_ACTIVE)
                return "rotated"

            def inventory_ids(self):
                return set()

        settings = {"partner_id": 4, "hardware_id": "hw",
                    "refresh_token": "tok", "username": "", "password": "",
                    "enable_deletions": False, "state": {}}
        try:
            with patch("calibre_plugin.ui.TolinoClient", FakeClient):
                worker = ui_module.SyncWorker(settings, [], [])
                worker.run()
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual([True], seen)
        self.assertFalse(tolino_module._SYNC_ACTIVE)


class TokenRotationWriteBackTests(unittest.TestCase):
    """0.15.5: Der Keep-alive rotiert den gespeicherten Zugang im Hintergrund,
    das Eingabefeld weiss davon nichts.

    Der Dialog schrieb den alten Token beim Schliessen oder Kontowechsel
    zurueck -- danach lehnt Tolino ihn als verbraucht ab («invalid_grant» /
    «reuse exceeded»). Ein UNVERAENDERTES Feld darf die Rotation deshalb nie
    ueberschreiben; nur eine eigene Eingabe zaehlt.
    """

    Combo = DashboardAccountSwitchTests.Combo

    def _ui(self, token="alt-token"):
        ui_module = ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        original = (config.PREFERENCES, ui_module.hardware_id)
        config.PREFERENCES = config.JSONConfig("test")
        config.PREFERENCES.update({
            "accounts": [{"name": "konto", "partner_id": 4,
                          "hardware_id": "hw-1", "refresh_token": token,
                          "username": "", "password": "", "state": {}}],
            "active_account": "konto",
        })
        ui_module.hardware_id = lambda: "auto-hw"

        def restore():
            (config.PREFERENCES, ui_module.hardware_id) = original
            ui_module._restore_stubs_for_tests()

        self.addCleanup(restore)
        dashboard = type("Dashboard", (), {})()
        for name in ("account_select", "account_quick", "partner", "hardware",
                     "refresh", "formats", "account_quick_label", "status",
                     "progress", "progress_label", "primary", "covers",
                     "deletions", "sync_collections", "tolino_column"):
            setattr(dashboard, name, self.Combo())
        dashboard.thread = None
        dashboard.partner.items = [(str(pid), pid)
                                   for pid in sorted(ui_module.PARTNERS)]
        # Die ECHTEN Methoden an das gefaelschte Fenster binden.
        for name in ("load_values", "values", "_selected_account",
                     "update_status", "set_refresh_token",
                     "_save_visible_account"):
            setattr(dashboard, name,
                    getattr(ui_module.SyncDashboard, name).__get__(dashboard))
        dashboard.load_values()
        return ui_module, dashboard

    def _rotate(self, token):
        config.save_account("konto", {"refresh_token": token}, active="konto")

    def _stored(self, name="konto"):
        return config.account_by_name(name)["refresh_token"]

    def test_untouched_field_keeps_the_rotated_token(self):
        _ui_module, dashboard = self._ui("alt-token")
        self.assertEqual("alt-token", dashboard.refresh.text())
        self._rotate("rotiert")
        # Das Feld ist unveraendert -- gerechnet wird mit dem gespeicherten
        # (frisch rotierten) Zugang, nicht mit der alten Kopie.
        self.assertEqual("alt-token", dashboard.refresh.text())
        self.assertEqual("rotiert", dashboard.values()["refresh_token"])
        # Und das Speichern beim Schliessen verbrennt die Rotation nicht.
        dashboard._save_visible_account()
        self.assertEqual("rotiert", self._stored())

    def test_own_input_wins_over_the_stored_one(self):
        """Nur eine eigene Eingabe zählt – auch wenn sie leer ist."""
        _ui_module, dashboard = self._ui("alt-token")
        self._rotate("rotiert")
        dashboard.refresh.setText("selbst-eingetragen")
        self.assertEqual("selbst-eingetragen",
                         dashboard.values()["refresh_token"])
        dashboard._save_visible_account()
        self.assertEqual("selbst-eingetragen", self._stored())
        # Feld geleert: das Löschen muss stehen bleiben.
        dashboard.refresh.setText("")
        self.assertEqual("", dashboard.values()["refresh_token"])
        dashboard._save_visible_account()
        self.assertEqual("", self._stored())

    def test_adopted_token_survives_a_later_rotation(self):
        """Frisch gelesen heisst gewollt: der Wert steht danach im Feld UND
        im Speicher (so macht es `persist_refresh_token`). Eine spaetere
        Rotation im Hintergrund darf auch er nicht ueberschreiben."""
        _ui_module, dashboard = self._ui("alt-token")
        dashboard.set_refresh_token("frisch-gelesen")
        config.save_account("konto",
                            {"refresh_token": dashboard.refresh.text()},
                            active="konto")
        self.assertEqual("frisch-gelesen", dashboard.values()["refresh_token"])
        self._rotate("rotiert-2")
        self.assertEqual("rotiert-2", dashboard.values()["refresh_token"])
        dashboard._save_visible_account()
        self.assertEqual("rotiert-2", self._stored())

    def test_untouched_hardware_field_keeps_the_adopted_id(self):
        _ui_module, dashboard = self._ui()
        config.save_account("konto", {"hardware_id": "adoptiert"},
                            active="konto")
        self.assertEqual("adoptiert", dashboard.values()["hardware_id"])


class RequestRetryBudgetTests(unittest.TestCase):
    """0.15.5: Statt des Tri-States True/1/False in `_retry` hat jede der drei
    Rettungen einen eigenen, sichtbaren Zaehler (Neuanmeldung, Geraete-
    Recovery, WAF-Warteversuch)."""

    @staticmethod
    def _bot_error():
        from urllib.error import HTTPError
        error = HTTPError("https://example.invalid/token", 403, "Forbidden",
                          {}, None)
        error.read = lambda: (b"<!doctype html><title>Zugriff geblockt"
                              b"</title>")
        return error

    def test_waf_budget_counts_down_and_then_raises(self):
        from .tolino import _WAF_RETRY_SECONDS, TolinoAuthError, TolinoClient
        sleeps = []

        def fake_urlopen(request, timeout=None):
            raise self._bot_error()

        with patch("calibre_plugin.tolino._curl_binary", return_value=None), \
                patch("calibre_plugin.tolino._impersonate_session",
                      return_value=None), \
                patch("calibre_plugin.tolino.urlopen",
                      side_effect=fake_urlopen), \
                patch("calibre_plugin.tolino.time.sleep",
                      side_effect=sleeps.append):
            client = TolinoClient(4, "hardware", "old-refresh")
            with self.assertRaises(TolinoAuthError):
                client.login()
        # Genau die zwei Warteversuche der Kaskade, dann der sichtbare Fehler.
        self.assertEqual(list(_WAF_RETRY_SECONDS), sleeps)

    def test_spent_auth_retry_does_not_block_the_device_recovery(self):
        """Die Rettungen teilen sich keinen Zaehler mehr: eine bereits
        verbrauchte Neuanmeldung (_auth_retry=False) darf die Geraete-
        Recovery nach einem BOSH-400 nicht sperren."""
        from urllib.error import HTTPError

        from .tolino import TolinoApiError, TolinoClient
        error = HTTPError("https://bosh.pageplace.de/bosh/rest/x", 400,
                          "Bad Request", {}, None)
        error.read = lambda: b"{}"
        client = TolinoClient(4, "stale-hw", "refresh-token-value")
        client.access = "access-value"
        client.expires_at = time.time() + 3600

        def fake_urlopen(request, timeout=None):
            raise error

        with patch("calibre_plugin.tolino.urlopen", fake_urlopen), \
                patch("calibre_plugin.tolino.TolinoClient.fetch_hardware_id",
                      return_value="registered-hw"), \
                patch("calibre_plugin.tolino.TolinoClient._register_hardware",
                      return_value=False):
            with self.assertRaises(TolinoApiError):
                client._request("https://bosh.pageplace.de/bosh/rest/x",
                                _auth_retry=False)
        # Die Recovery lief trotz verbrauchtem Auth-Retry ...
        self.assertEqual("registered-hw", client.hardware)


class PersistCredentialsDedupTests(unittest.TestCase):
    """0.15.5: Token + Hardware-ID werden an EINER Stelle gespeichert.

    Dialog und Sync-Worker hatten dafuer je ein fast identisches Methodenpaar
    (`persist_refresh_token`/`persist_hardware`). Beide gehen jetzt durch
    `config.save_credentials`; in den vier Methoden steht kein eigener
    `save_account`-Aufruf mehr.
    """

    @staticmethod
    def _ui_module():
        return ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None,
            qt_stub={"pyqtSignal":
                     CollectionReconcileWorkerTests._signal_descriptor()[0]})

    def test_save_credentials_writes_token_and_hardware(self):
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            config.save_credentials("konto", "tok-1", "hw-1")
            account = config.account_by_name("konto")
            self.assertEqual("tok-1", account["refresh_token"])
            self.assertEqual("hw-1", account["hardware_id"])
            self.assertEqual("konto", config.settings()["active_account"])
        finally:
            config.PREFERENCES = original

    def test_save_credentials_without_a_name_does_nothing(self):
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            config.save_credentials("", "tok", "hw")
            active = config.settings()["active_account"]
            self.assertEqual("", config.account_by_name(active)["refresh_token"])
        finally:
            config.PREFERENCES = original

    def test_sync_worker_persists_through_the_shared_helper(self):
        ui_module = self._ui_module()
        saved = []
        settings = {"account_name": "konto", "hardware_id": "hw-alt",
                    "refresh_token": "tok-alt"}
        try:
            with patch("calibre_plugin.ui.save_credentials",
                       lambda *args: saved.append(args)):
                worker = ui_module.SyncWorker(settings, [], [])
                worker.persist_refresh_token("tok-neu")
                worker.persist_hardware("hw-neu")
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual([("konto", "tok-neu", "hw-alt"),
                          ("konto", "tok-neu", "hw-neu")], saved)

    def test_dashboard_persists_through_the_shared_helper(self):
        ui_module = self._ui_module()

        class Field:
            def __init__(self, value=""):
                self.value = value

            def text(self):
                return self.value

            def setText(self, value):
                self.value = value

        saved = []
        dashboard = type("Dashboard", (), {})()
        dashboard.account_name = "konto"
        dashboard.refresh = Field("alt")
        dashboard.hardware = Field("hw-alt")
        dashboard.set_refresh_token = dashboard.refresh.setText
        try:
            with patch("calibre_plugin.ui.save_credentials",
                       lambda *args: saved.append(args)):
                ui_module.SyncDashboard.persist_refresh_token(
                    dashboard, "tok-neu")
                ui_module.SyncDashboard.persist_hardware(dashboard, "hw-neu")
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual([("konto", "tok-neu", "hw-alt"),
                          ("konto", "tok-neu", "hw-neu")], saved)
        self.assertEqual("hw-neu", dashboard._loaded_hardware)

    def test_all_four_methods_use_the_shared_helper(self):
        source = (Path(__file__).with_name("ui.py")).read_text(encoding="utf-8")
        lines = source.splitlines()
        seen = 0
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.ClassDef) or node.name not in (
                    "SyncWorker", "SyncDashboard"):
                continue
            for func in node.body:
                if not isinstance(func, ast.FunctionDef) or func.name not in (
                        "persist_refresh_token", "persist_hardware"):
                    continue
                body = "\n".join(lines[func.lineno - 1:func.end_lineno])
                self.assertIn("save_credentials(", body, func.name)
                self.assertNotIn("save_account(", body, func.name)
                seen += 1
        self.assertEqual(4, seen)


class StartSyncOrchestrationTests(unittest.TestCase):
    """`start_sync` bleibt Ablaufsteuerung, die Arbeit steckt in Schritten.

    Vorher las, anmeldete, verglich, plante und startete eine einzige
    160-Zeilen-Methode. Diese Tests halten Reihenfolge, Fehlergrenze und
    die Weitergabe der Werte an die neuen Schritte fest (0.15.6).
    """

    @staticmethod
    def _ui_module():
        return ToolbarIconTests._load_ui_module(lambda *args, **kwargs: None)

    @staticmethod
    def _source():
        return (Path(__file__).with_name("ui.py")).read_text(encoding="utf-8")

    @classmethod
    def _method_body(cls, name):
        body = cls._source().split(f"    def {name}(", 1)[1]
        return body.split("\n    def ", 1)[0]

    @staticmethod
    def _settings():
        return {
            "refresh_token": "token-1", "partner_id": "thalia",
            "hardware_id": "hw-1", "username": "", "password": "",
            "state": {}, "preferred_formats": ("EPUB",),
            "sync_collections": False, "enable_deletions": False,
            "upload_covers": False,
        }

    def _dashboard(self, ui_module, settings, thread=None):
        dashboard = ui_module.SyncDashboard.__new__(ui_module.SyncDashboard)
        dashboard.thread = thread
        dashboard.values = lambda: settings
        dashboard.gui = type("Gui", (), {"current_db": object()})()
        dashboard.sync_column_enabled = None
        return dashboard

    @staticmethod
    def _stub_warning(ui_module, warnings):
        """QMessageBox.warning durch einen Recorder ersetzen."""

        def warning(*args, **kwargs):
            warnings.append(args)

        ui_module.QMessageBox = type("QMessageBox", (),
                                     {"warning": staticmethod(warning)})

    @staticmethod
    def _install_fake_qt(ui_module):
        """QThread/SyncWorker durch Recorder ersetzen (Thread-Start messbar)."""

        class FakeSignal:
            def __init__(self):
                self.slots = []

            def connect(self, slot):
                self.slots.append(slot)

        class FakeThread:
            def __init__(self, parent=None):
                self.started = FakeSignal()
                self.finished = FakeSignal()
                self.starts = 0

            def start(self):
                self.starts += 1

            def deleteLater(self):
                self.deleted = True

        class FakeWorker:
            def __init__(self, settings, jobs, removals, meta_updates=None,
                         series_map=None, selected_read=(),
                         selected_unread=()):
                self.arguments = (settings, jobs, removals, meta_updates,
                                  series_map, selected_read, selected_unread)
                self.progress = FakeSignal()
                self.completed = FakeSignal()
                self.failed = FakeSignal()

            def moveToThread(self, thread):
                self.moved_to = thread

            def run(self):
                pass

        ui_module.QThread = FakeThread
        ui_module.SyncWorker = FakeWorker
        return FakeThread, FakeWorker

    def test_the_work_goes_to_the_named_steps(self):
        ui_module = self._ui_module()
        try:
            settings = self._settings()
            ui_module.tolino_column_active = lambda *args: True
            dashboard = self._dashboard(ui_module, settings)
            seen = []
            preparation = {"jobs": [], "skipped_uploads": []}
            dashboard._prepare_sync = lambda given: (
                seen.append(("prepare", given)) or preparation)
            dashboard._start_sync_worker = lambda given, prep: seen.append(
                ("worker", given, prep))
            dashboard.start_sync()
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual(["prepare", "worker"], [item[0] for item in seen])
        self.assertIs(settings, seen[0][1])
        self.assertIs(settings, seen[1][1])
        self.assertIs(preparation, seen[1][2])

    def test_without_a_token_it_only_warns(self):
        ui_module = self._ui_module()
        try:
            warnings = []
            self._stub_warning(ui_module, warnings)
            ui_module.tolino_column_active = lambda *args: True
            settings = dict(self._settings(), refresh_token="")
            dashboard = self._dashboard(ui_module, settings)
            prepared = []
            dashboard._prepare_sync = lambda given: prepared.append(given)
            dashboard.start_sync()
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual(1, len(warnings))
        self.assertIn("Noch nicht angemeldet", warnings[0][1])
        self.assertIn("anmelden", warnings[0][2])
        self.assertEqual([], prepared)

    def test_a_failed_preparation_is_reported_and_stops_everything(self):
        ui_module = self._ui_module()
        try:
            errors = []
            ui_module.error_dialog = lambda *args, **kwargs: errors.append(args)
            ui_module.tolino_column_active = lambda *args: True
            settings = self._settings()
            dashboard = self._dashboard(ui_module, settings)

            def boom(given):
                raise RuntimeError("kaputt")

            dashboard._prepare_sync = boom
            started = []
            dashboard._start_sync_worker = lambda given, prep: started.append(prep)
            dashboard.start_sync()
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual(1, len(errors))
        self.assertEqual("Vorbereitung fehlgeschlagen", errors[0][1])
        self.assertIn("kaputt", errors[0][2])
        self.assertEqual([], started)

    def test_a_cancelled_dialog_starts_nothing(self):
        ui_module = self._ui_module()
        try:
            errors = []
            ui_module.error_dialog = lambda *args, **kwargs: errors.append(args)
            ui_module.tolino_column_active = lambda *args: True
            settings = self._settings()
            dashboard = self._dashboard(ui_module, settings)
            dashboard._prepare_sync = lambda given: None
            started = []
            dashboard._start_sync_worker = lambda given, prep: started.append(prep)
            dashboard.start_sync()
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual([], errors)
        self.assertEqual([], started)

    def test_a_running_sync_ignores_the_second_click(self):
        ui_module = self._ui_module()
        try:
            settings = self._settings()
            dashboard = self._dashboard(ui_module, settings, thread=object())
            calls = []
            dashboard.values = lambda: calls.append("values")
            dashboard._prepare_sync = lambda given: calls.append("prepare")
            dashboard._start_sync_worker = lambda given, prep: calls.append("worker")
            dashboard.start_sync()
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual([], calls)

    def test_prepare_sync_runs_the_steps_in_order(self):
        ui_module = self._ui_module()
        try:
            settings = self._settings()
            order = []
            state = {"fingerprints": {}}
            metadata = {1: {"title": "A"}}
            client = object()
            comparison = [{"status": "identical"}]
            selection = {"uploads": {1}, "metadata": set(),
                         "read": set(), "unread": set()}
            preparation = {"jobs": []}
            captured = {}
            ui_module.load_state = lambda value: order.append("state") or state
            dashboard = self._dashboard(ui_module, settings)
            dashboard._local_metadata = lambda: order.append("metadata") or metadata
            dashboard._logged_in_client = lambda given: order.append("login") or client

            def cloud(given, meta, st, cl):
                order.append("compare")
                captured["cloud"] = (given, meta, st, cl)
                return comparison

            def confirm(comp, given):
                order.append("confirm")
                captured["confirm"] = (comp, given)
                return selection

            def plan(given, meta, st, sel):
                order.append("plan")
                captured["plan"] = (given, meta, st, sel)
                return preparation

            dashboard._cloud_comparison = cloud
            dashboard._confirm_comparison = confirm
            dashboard._plan_actions = plan
            result = dashboard._prepare_sync(settings)
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual(
            ["metadata", "state", "login", "compare", "confirm", "plan"], order)
        self.assertIs(preparation, result)
        self.assertEqual((settings, metadata, state, client), captured["cloud"])
        self.assertEqual((comparison, settings), captured["confirm"])
        self.assertEqual((settings, metadata, state, selection), captured["plan"])
        self.assertIs(state, captured["plan"][2])

    def test_prepare_sync_stops_when_the_dialog_is_cancelled(self):
        ui_module = self._ui_module()
        try:
            settings = self._settings()
            ui_module.load_state = lambda value: {}
            dashboard = self._dashboard(ui_module, settings)
            dashboard._local_metadata = lambda: {}
            dashboard._logged_in_client = lambda given: object()
            dashboard._cloud_comparison = lambda *args: []
            dashboard._confirm_comparison = lambda comp, given: None
            planned = []
            dashboard._plan_actions = lambda *args: planned.append(args)
            result = dashboard._prepare_sync(settings)
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertIsNone(result)
        self.assertEqual([], planned)

    def test_the_worker_gets_the_finished_plan(self):
        ui_module = self._ui_module()
        try:
            self._install_fake_qt(ui_module)
            settings = self._settings()
            dashboard = self._dashboard(ui_module, settings)
            ranges, values, labels, status_updates = [], [], [], []
            dashboard.progress = type("Bar", (), {
                "setRange": lambda self, *args: ranges.append(args),
                "setValue": lambda self, value: values.append(value),
            })()
            dashboard.progress_label = type("Label", (), {
                "setText": lambda self, text: labels.append(text),
            })()
            dashboard.refresh = type("Edit", (), {
                "text": lambda self: "token-neu",
            })()
            dashboard.update_status = lambda: status_updates.append(True)
            for name in ("progress_changed", "sync_completed", "sync_failed"):
                setattr(dashboard, name, lambda *args: None)
            jobs = ["job-1", "job-2"]
            preparation = {
                "jobs": jobs, "removals": ["remove-1"],
                "meta_updates": [("uuid-1", 7)], "series_map": {"Reihe": ["Sammlung"]},
                "skipped_uploads": [], "selected_read": {"t-read"},
                "selected_unread": {"t-unread"},
            }
            dashboard._start_sync_worker(settings, preparation)
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual(settings, dashboard.worker.arguments[0])
        self.assertEqual(jobs, dashboard.worker.arguments[1])
        self.assertEqual(["remove-1"], dashboard.worker.arguments[2])
        self.assertEqual([("uuid-1", 7)], dashboard.worker.arguments[3])
        self.assertEqual({"Reihe": ["Sammlung"]}, dashboard.worker.arguments[4])
        self.assertEqual({"t-read"}, dashboard.worker.arguments[5])
        self.assertEqual({"t-unread"}, dashboard.worker.arguments[6])
        self.assertIs(dashboard.thread, dashboard.worker.moved_to)
        self.assertEqual(1, dashboard.thread.starts)
        self.assertEqual(1, len(dashboard.worker.progress.slots))
        self.assertEqual(1, len(dashboard.worker.completed.slots))
        self.assertEqual(1, len(dashboard.worker.failed.slots))
        # sync_summary zählt 2 Uploads + 1 Löschung.
        self.assertEqual([(0, 3)], ranges)
        self.assertEqual([0, 0], values)
        self.assertEqual(["Synchronisierung läuft"], labels)
        self.assertEqual([True], status_updates)
        self.assertEqual("token-neu", settings["refresh_token"])

    def test_skipped_uploads_are_reported_before_the_run(self):
        ui_module = self._ui_module()
        try:
            warnings = []
            self._stub_warning(ui_module, warnings)
            self._install_fake_qt(ui_module)
            settings = self._settings()
            dashboard = self._dashboard(ui_module, settings)
            ranges, values, labels = [], [], []
            dashboard.progress = type("Bar", (), {
                "setRange": lambda self, *args: ranges.append(args),
                "setValue": lambda self, value: values.append(value),
            })()
            dashboard.progress_label = type("Label", (), {
                "setText": lambda self, text: labels.append(text),
            })()
            dashboard.refresh = type("Edit", (), {"text": lambda self: "t"})()
            dashboard.update_status = lambda: None
            dashboard.persist_refresh_token = lambda value: None
            for name in ("progress_changed", "sync_completed", "sync_failed"):
                setattr(dashboard, name, lambda *args: None)
            dashboard._start_sync_worker(settings, {
                "jobs": [], "removals": [], "meta_updates": [], "series_map": {},
                "skipped_uploads": ["Buch 1 (Titel), Format EPUB: fehlt"],
                "selected_read": set(), "selected_unread": set(),
            })
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual(1, len(warnings))
        self.assertIn("übersprungen", warnings[0][2])
        self.assertIn("Buch 1 (Titel), Format EPUB: fehlt", warnings[0][2])
        # Der Lauf startet trotzdem -- nur mit den übrigen Büchern.
        self.assertEqual(1, dashboard.thread.starts)
        self.assertEqual([], dashboard.worker.arguments[1])

    def test_the_new_steps_exist_and_are_small(self):
        """Jeder Schritt liegt als eigene, kleine Methode im Dashboard."""
        methods = ("start_sync", "_prepare_sync", "_local_metadata",
                   "_logged_in_client", "_cloud_comparison",
                   "_confirm_comparison", "_plan_actions", "_upload_jobs",
                   "_start_sync_worker")
        tree = ast.parse(self._source())
        dashboard = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "SyncDashboard")
        defined = {node.name for node in dashboard.body
                   if isinstance(node, ast.FunctionDef)}
        self.assertLessEqual(set(methods), defined)
        for name in methods:
            with self.subTest(method=name):
                body = self._method_body(name)
                code = [line for line in body.splitlines()
                        if line.strip() and not line.strip().startswith("#")]
                self.assertLessEqual(
                    len(code), 40,
                    f"{name} ist wieder auf {len(code)} Zeilen gewachsen")

    def test_start_sync_only_orders_the_steps(self):
        body = self._method_body("start_sync")
        code = [line for line in body.splitlines()
                if line.strip() and not line.strip().startswith("#")]
        self.assertLessEqual(len(code), 30)
        self.assertIn("self._prepare_sync(settings)", body)
        self.assertIn("self._start_sync_worker(settings, preparation)", body)
        self.assertIn("self._show_prepare_error(exc, settings)", body)
        self.assertIn("Vorbereitung fehlgeschlagen",
                      self._method_body("_show_prepare_error"))
        # Die eigentliche Arbeit liegt nicht mehr hier.
        for name in ("compare_inventory(", "plan_sync(", "QThread(",
                     "InventoryDialog(", "safe_format_path("):
            self.assertNotIn(name, body)


class LoginPreflightTests(unittest.TestCase):
    """0.15.7: Der Anmeldeknopf prueft zuerst den GESPEICHERTEN Zugang.

    Feldwunsch: «Zuerst pruefen, ob die aktuellen Logindaten noch
    funktionieren, bevor ein neuer Loginprozess beginnt.» Die Pruefung
    laeuft still im Hintergrund; das Browser-Fenster geht nur auf, wenn
    Tolino den Zugang ablehnt oder er nicht pruefbar war.
    """

    # --- tolino.check_stored_login --------------------------------------

    @staticmethod
    def _patch_client(records, error=None, rotate=True):
        """TolinoClient durch einen Recorder ersetzen (kein Netz)."""

        class FakeClient:
            def __init__(self, partner, hardware, token, username=None,
                         password=None, token_callback=None):
                records.append((partner, hardware, token, username, password))
                self.token = token
                self.refresh = token
                self.token_callback = token_callback

            def login(self):
                if error is not None:
                    raise error
                if rotate:
                    self.refresh = self.token + "-rotated"
                    if self.token_callback:
                        self.token_callback(self.refresh)
                return self.refresh

        return patch("calibre_plugin.tolino.TolinoClient", FakeClient)

    def test_a_working_access_is_renewed_and_saved(self):
        records, persisted = [], []
        with self._patch_client(records):
            status, detail = tolino_module.check_stored_login(
                4, "hw-1", "tok-1", "u", "p", persist=persisted.append)
        self.assertEqual("valid", status)
        self.assertEqual("tok-1-rotated", detail)
        # Geprueft wurde der GESPEICHERTE Zugang, nicht ein leerer.
        self.assertEqual([(4, "hw-1", "tok-1", "u", "p")], records)
        self.assertEqual(["tok-1-rotated"], persisted)

    def test_an_unchanged_token_is_not_written_again(self):
        records, persisted = [], []
        with self._patch_client(records, rotate=False):
            status, detail = tolino_module.check_stored_login(
                4, "hw-1", "tok-1", persist=persisted.append)
        self.assertEqual(("valid", "tok-1"), (status, detail))
        self.assertEqual([], persisted)

    def test_a_rejected_access_reports_the_reason_without_the_token(self):
        records = []
        error = TolinoAuthError(
            "invalid_grant: refresh_token tok-1 wurde abgelehnt")
        with self._patch_client(records, error=error):
            status, detail = tolino_module.check_stored_login(
                4, "hw-1", "tok-1")
        self.assertEqual("invalid", status)
        self.assertNotIn("tok-1", detail)
        self.assertIn("[REDACTED]", detail)
        self.assertIn("invalid_grant", detail)

    def test_without_a_stored_token_nothing_is_asked(self):
        with patch("calibre_plugin.tolino.TolinoClient") as client:
            status, detail = tolino_module.check_stored_login(4, "hw-1", "")
        self.assertEqual("invalid", status)
        self.assertIn("Noch nicht angemeldet", detail)
        client.assert_not_called()

    def test_a_running_sync_blocks_the_check(self):
        records = []
        tolino_module.mark_sync_active(True)
        self.addCleanup(tolino_module.mark_sync_active, False)
        with self._patch_client(records):
            status, detail = tolino_module.check_stored_login(
                4, "hw-1", "tok-1")
        # NICHT „invalid": der Zugang ist ungeprueft, nicht abgelehnt.
        self.assertEqual(("busy", ""), (status, detail))
        self.assertEqual([], records)

    def test_a_parallel_rotation_blocks_the_check(self):
        records = []
        with tolino_module._KEEPALIVE_LOCK, self._patch_client(records):
            status, detail = tolino_module.check_stored_login(
                4, "hw-1", "tok-1", lock_timeout=0)
        self.assertEqual(("busy", ""), (status, detail))
        self.assertEqual([], records)

    def test_the_rotation_lock_is_released_afterwards(self):
        with self._patch_client([]):
            tolino_module.check_stored_login(4, "hw-1", "tok-1")
        self.assertTrue(tolino_module._KEEPALIVE_LOCK.acquire(blocking=False))
        tolino_module._KEEPALIVE_LOCK.release()

    # --- Der Anmeldeknopf -----------------------------------------------

    class Field:
        def __init__(self, value=""):
            self.value = value
            self.enabled = True

        def text(self):
            return self.value

        def setText(self, value):
            self.value = value

        def setEnabled(self, value):
            self.enabled = value

        def setToolTip(self, value):
            self.tooltip = value

    def _ui(self):
        ui_module = ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        self.addCleanup(ui_module._restore_stubs_for_tests)
        ui_module.hardware_id = lambda: "auto-hw"
        return ui_module

    @staticmethod
    def _fake_qt(ui_module):
        """QThread und beide Worker durch Recorder ersetzen."""

        class Signal:
            def __init__(self):
                self.slots = []

            def connect(self, slot):
                self.slots.append(slot)

            def fire(self, *args):
                for slot in list(self.slots):
                    slot(*args)

        class Thread:
            def __init__(self, parent=None):
                self.started = Signal()
                self.finished = Signal()
                self.starts = 0

            def start(self):
                self.starts += 1
                self.started.fire()

            def isRunning(self):
                return self.starts > 0

            def quit(self, *args):
                self.quits = getattr(self, "quits", 0) + 1

            def deleteLater(self, *args):
                self.deleted = True

        class CheckWorker:
            def __init__(self, settings):
                self.settings = settings
                self.valid = Signal()
                self.invalid = Signal()
                self.busy = Signal()

            def moveToThread(self, thread):
                self.moved_to = thread

            def run(self):
                pass

        class LoginWorker:
            def __init__(self, partner_id, hardware):
                self.arguments = (partner_id, hardware)
                self.progress = Signal()
                self.completed = Signal()
                self.failed = Signal()

            def moveToThread(self, thread):
                self.moved_to = thread

            def run(self):
                pass

        ui_module.QThread = Thread
        ui_module.SessionCheckWorker = CheckWorker
        ui_module.BrowserLoginWorker = LoginWorker
        return Thread, CheckWorker, LoginWorker

    @staticmethod
    def _boxes(ui_module, seen):
        """QMessageBox.information/warning als Recorder.

        Der Aufruf im Dialog beginnt mit `self`, danach folgen Titel und
        Text -- die Zusicherungen lesen deshalb Index 1 und 2.
        """

        def record(*args, **kwargs):
            seen.append(args)

        ui_module.QMessageBox = type("QMessageBox", (), {
            "information": staticmethod(record),
            "warning": staticmethod(record),
        })

    def _assert_one_line(self, box, title):
        """Die neuen Infofenster bleiben EINE Zeile (Feldwunsch 0.15.8)."""
        self.assertEqual(title, box[1])
        text = box[2]
        self.assertNotIn("\n", text)
        self.assertLessEqual(len(text), 100, text)

    def _dashboard(self, ui_module, token="tok-1"):
        dashboard = type("Dashboard", (), {})()
        dashboard.account_name = "konto"
        dashboard.login_thread = None
        dashboard.login_worker = None
        dashboard.refresh = self.Field(token)
        dashboard.hardware = self.Field("hw-1")
        dashboard.primary = self.Field()
        dashboard.grab_button = self.Field()
        dashboard.status = self.Field()
        dashboard.update_status = lambda: None
        dashboard.settings_values = {
            "account_name": "konto", "partner_id": 4,
            "hardware_id": "hw-1", "refresh_token": token,
            "username": "", "password": "",
        }
        dashboard.values = lambda: dashboard.settings_values
        # Die beiden Ergebnis-Slots des Anmeldefensters: hier nur Empfaenger.
        dashboard.login_completed = lambda *args: None
        dashboard.login_failed = lambda *args: None
        names = ("browser_login", "_check_stored_login",
                 "_start_login_window", "_login_running",
                 "login_still_valid", "login_rejected", "login_check_busy",
                 "_login_cleanup", "set_refresh_token",
                 "persist_refresh_token", "_login_progress")
        for name in names:
            setattr(dashboard, name,
                    getattr(ui_module.SyncDashboard, name).__get__(dashboard))
        return dashboard

    def test_without_a_stored_token_the_window_opens_right_away(self):
        ui_module = self._ui()
        self._fake_qt(ui_module)
        dashboard = self._dashboard(ui_module, token="")
        opened = []
        dashboard._start_login_window = lambda values: opened.append(values)
        dashboard.browser_login()
        self.assertEqual([""], [values["refresh_token"] for values in opened])
        self.assertIsNone(dashboard.login_thread)

    def test_with_a_token_the_stored_access_is_checked_first(self):
        ui_module = self._ui()
        Thread, CheckWorker, _ = self._fake_qt(ui_module)
        dashboard = self._dashboard(ui_module)
        opened = []
        dashboard._start_login_window = lambda values: opened.append(values)
        dashboard.browser_login()
        self.assertEqual([], opened)
        self.assertIsInstance(dashboard.login_thread, Thread)
        self.assertIsInstance(dashboard.login_worker, CheckWorker)
        self.assertEqual(1, dashboard.login_thread.starts)
        self.assertIs(dashboard.settings_values,
                      dashboard.login_worker.settings)
        self.assertIs(dashboard.login_thread,
                      dashboard.login_worker.moved_to)
        self.assertIn("geprüft", dashboard.status.text())
        self.assertFalse(dashboard.primary.enabled)

    def test_a_working_access_skips_the_login_window(self):
        ui_module = self._ui()
        self._fake_qt(ui_module)
        dashboard = self._dashboard(ui_module)
        opened, saved, boxes = [], [], []
        dashboard._start_login_window = lambda values: opened.append(values)
        self._boxes(ui_module, boxes)
        with patch("calibre_plugin.ui.save_credentials",
                   lambda *args: saved.append(args)):
            dashboard.browser_login()
            dashboard.login_worker.valid.fire("tok-rotated")
        self.assertEqual([], opened)
        self.assertEqual([("konto", "tok-rotated", "hw-1")], saved)
        self.assertEqual("tok-rotated", dashboard.refresh.text())
        # Knopf und Fenster sind wieder frei, die Pruefung ist beendet.
        self.assertIsNone(dashboard.login_thread)
        self.assertTrue(dashboard.primary.enabled)
        self.assertIn("weiterhin", dashboard.status.text())
        self.assertEqual(1, len(boxes))
        self._assert_one_line(boxes[0], "Zugang geprüft")

    def test_a_rejected_access_opens_the_login_window(self):
        ui_module = self._ui()
        Thread, CheckWorker, LoginWorker = self._fake_qt(ui_module)
        dashboard = self._dashboard(ui_module)
        boxes, logged = [], []
        self._boxes(ui_module, boxes)
        with patch("calibre_plugin.ui.log_warning",
                   lambda message: logged.append(message)):
            dashboard.browser_login()
            check_thread = dashboard.login_thread
            self.assertIsInstance(dashboard.login_worker, CheckWorker)
            dashboard.login_worker.invalid.fire("invalid_grant (ohne Token)")
        self.assertEqual(1, len(boxes))
        self._assert_one_line(boxes[0], "Zugang abgelehnt")
        self.assertIn("anmelden", boxes[0][2])
        # Der technische Grund bleibt aussen vor: eine Zeile im Fenster,
        # der Rest ins Log (Feldwunsch 0.15.8).
        self.assertNotIn("invalid_grant", boxes[0][2])
        self.assertEqual(1, len(logged))
        self.assertIn("invalid_grant", logged[0])
        # Der neue Anlauf ist ein eigener Thread, nicht der Pruef-Thread:
        # die Ablehnung oeffnet wirklich das Anmeldefenster.
        self.assertIsInstance(dashboard.login_thread, Thread)
        self.assertIsNot(check_thread, dashboard.login_thread)
        self.assertIsInstance(dashboard.login_worker, LoginWorker)
        self.assertEqual((4, "hw-1"), dashboard.login_worker.arguments)
        self.assertEqual(1, dashboard.login_thread.starts)
        self.assertIn("Anmeldung läuft", dashboard.status.text())

    def test_a_busy_check_opens_nothing_and_keeps_the_token(self):
        ui_module = self._ui()
        self._fake_qt(ui_module)
        dashboard = self._dashboard(ui_module)
        opened, saved, boxes = [], [], []
        dashboard._start_login_window = lambda values: opened.append(values)
        self._boxes(ui_module, boxes)
        with patch("calibre_plugin.ui.save_credentials",
                   lambda *args: saved.append(args)):
            dashboard.browser_login()
            dashboard.login_worker.busy.fire("")
        self.assertEqual([], opened)
        self.assertEqual([], saved)
        self.assertEqual("tok-1", dashboard.refresh.text())
        self.assertIsNone(dashboard.login_thread)
        self.assertTrue(dashboard.primary.enabled)
        self.assertEqual(1, len(boxes))
        self._assert_one_line(boxes[0], "Zugang in Benutzung")

    def test_a_second_click_while_checking_is_ignored(self):
        ui_module = self._ui()
        check_thread = type("Thread", (), {
            "isRunning": lambda self: True})()
        dashboard = self._dashboard(ui_module)
        dashboard.login_thread = check_thread
        calls = []
        dashboard._check_stored_login = lambda values: calls.append(values)
        dashboard._start_login_window = lambda values: calls.append(values)
        dashboard.browser_login()
        self.assertEqual([], calls)
        self.assertIs(check_thread, dashboard.login_thread)

    def test_the_window_step_starts_its_own_sign_in(self):
        ui_module = self._ui()
        Thread, _, LoginWorker = self._fake_qt(ui_module)
        dashboard = self._dashboard(ui_module)
        dashboard._start_login_window(dashboard.values())
        self.assertIsInstance(dashboard.login_thread, Thread)
        self.assertIsInstance(dashboard.login_worker, LoginWorker)
        self.assertEqual((4, "hw-1"), dashboard.login_worker.arguments)
        self.assertEqual(1, dashboard.login_thread.starts)
        self.assertFalse(dashboard.primary.enabled)
        self.assertFalse(dashboard.grab_button.enabled)
        self.assertIn("Anmeldung läuft", dashboard.status.text())

    def _run_check_worker(self, ui_module, result):
        """`SessionCheckWorker.run()` mit gestubbter Pruefung laufen lassen.

        `result` ist das, was `check_stored_login` liefert -- oder eine
        Exception, die es stattdessen wirft. Zurueck kommen der Worker, die
        gemeldeten Signale und die Argumente der Pruefung.
        """

        def check(*args, **kwargs):
            calls.append((args, kwargs))
            if isinstance(result, Exception):
                raise result
            return result

        def signal(name):
            return type("Signal", (), {
                "emit": lambda self, value: emitted.append((name, value))})()

        emitted, calls = [], []
        settings = {"partner_id": 4, "hardware_id": "hw-1",
                    "refresh_token": "tok-1", "username": "u",
                    "password": "p", "account_name": "konto"}
        worker = ui_module.SessionCheckWorker(settings)
        worker.valid, worker.invalid, worker.busy = (signal("valid"),
                                                     signal("invalid"),
                                                     signal("busy"))
        ui_module.check_stored_login = check
        worker.run()
        return worker, emitted, calls

    def test_the_worker_hands_the_stored_access_to_the_check(self):
        ui_module = self._ui()
        worker, emitted, calls = self._run_check_worker(
            ui_module, ("valid", "tok-2"))
        args, kwargs = calls[0]
        self.assertEqual((4, "hw-1", "tok-1", "u", "p"), args)
        # Der rotierte Token wird aus dem Arbeitsthread heraus gesichert.
        self.assertEqual(worker.persist_refresh_token, kwargs["persist"])
        self.assertEqual([("valid", "tok-2")], emitted)

    def test_every_check_result_reaches_the_matching_slot(self):
        ui_module = self._ui()
        results = (("valid", "tok-2"), ("invalid", "Grund"), ("busy", ""))
        reported = [self._run_check_worker(ui_module, result)[1]
                    for result in results]
        self.assertEqual([[result] for result in results], reported)

    def test_a_failing_check_is_reported_as_a_rejection(self):
        ui_module = self._ui()
        _worker, emitted, _calls = self._run_check_worker(
            ui_module, RuntimeError("kaputt mit tok-1"))
        self.assertEqual(1, len(emitted))
        self.assertEqual("invalid", emitted[0][0])
        self.assertIn("kaputt", emitted[0][1])
        # Auch der unerwartete Fehler verraet den Zugang nicht.
        self.assertNotIn("tok-1", emitted[0][1])

    def test_the_worker_saves_rotations_through_the_shared_helper(self):
        ui_module = self._ui()
        saved = []
        settings = {"account_name": "konto", "hardware_id": "hw-1",
                    "refresh_token": "tok-1"}
        worker = ui_module.SessionCheckWorker(settings)
        with patch("calibre_plugin.ui.save_credentials",
                   lambda *args: saved.append(args)):
            worker.persist_refresh_token("tok-2")
        self.assertEqual([("konto", "tok-2", "hw-1")], saved)
        self.assertEqual("tok-2", settings["refresh_token"])

    def test_the_button_checks_before_it_opens_a_window(self):
        """Der Anmeldeweg bleibt EIN Weg: erst pruefen, dann anmelden."""
        source = (Path(__file__).with_name("ui.py")).read_text(
            encoding="utf-8")
        self.assertIn("self.login_button.clicked.connect(self.browser_login)",
                      source)
        body = source.split("    def browser_login(self):", 1)[1].split(
            "\n    def ", 1)[0]
        self.assertIn("self._check_stored_login(settings)", body)
        self.assertIn("self._start_login_window(settings)", body)
        self.assertNotIn("BrowserLoginWorker(", body)

if __name__ == "__main__":
    unittest.main()


class ErrorCodeTests(unittest.TestCase):
    """Fehlercodes: kurzer Nutzertext, Details nur im Log (0.15.9)."""

    def setUp(self):
        tolino_module._RECENT_ERRORS.clear()


    def test_short_text_hides_the_server_body_but_the_log_keeps_it(self):
        exc = tolino_module.TolinoApiError(
            "Tolino-Fehler (HTTP 500): <html>boom</html>", code="N03",
            short="Tolino meldet einen Fehler (HTTP 500).")
        with patch.object(tolino_module, "log_warning") as log:
            shown = tolino_module.report_error(exc)
        self.assertEqual("Tolino meldet einen Fehler (HTTP 500). (Code N03)",
                         shown)
        self.assertIn("boom", log.call_args[0][0])
        self.assertIn("Tolino-Fehler (HTTP 500)", str(exc))

    def test_secrets_never_reach_the_dialog_the_log_or_the_history(self):
        exc = tolino_module.TolinoAuthError(
            "Fehler mit geheimwert-123", code="A06",
            detail="auch hier geheimwert-123")
        with patch.object(tolino_module, "log_warning") as log:
            shown = tolino_module.report_error(
                exc, "Test", ("geheimwert-123",))
        blob = " ".join([shown, log.call_args[0][0]]
                        + tolino_module.recent_errors())
        self.assertNotIn("geheimwert-123", blob)

    def test_unexpected_errors_get_e99_and_a_traceback_in_the_log(self):
        with patch.object(tolino_module, "log_warning") as log:
            try:
                raise ValueError("kaputt")
            except ValueError as exc:
                shown = tolino_module.report_error(exc, "Test")
        self.assertIn("(Code E99)", shown)
        self.assertIn("kaputt", shown)
        logged = " ".join(call[0][0] for call in log.call_args_list)
        self.assertIn("Traceback", logged)

    def test_recent_errors_keep_only_the_last_twenty(self):
        with patch.object(tolino_module, "log_warning"):
            for index in range(25):
                tolino_module.report_error(tolino_module.TolinoError(
                    "Fehler %d" % index, code="S90"))
        recent = tolino_module.recent_errors()
        self.assertEqual(20, len(recent))
        self.assertIn("Fehler 24", recent[-1])
        self.assertIn("Fehler 5", recent[0])

    def test_code_without_message_defaults_to_e00(self):
        self.assertEqual("E00", tolino_module.TolinoError("x").code)

    def test_every_code_in_the_source_is_documented_in_the_error_list(self):
        """Die Code-Liste liegt in docs/fehlercodes.md (aus der README ausgelagert)."""
        base = Path(__file__).resolve().parent
        error_list = (base.parent / "docs" / "fehlercodes.md").read_text(
            encoding="utf-8")
        used = set()
        for name in ("tolino.py", "cdp.py", "ui.py"):
            text = (base / name).read_text(encoding="utf-8")
            used |= set(re.findall(r'code\s*=\s*"([A-Z][0-9]{2})"', text))
        used.discard("E00")  # Standardwert der Basisklasse, kein Meldungscode
        self.assertTrue(used)
        for code in sorted(used):
            self.assertIn("| %s |" % code, error_list, code)
        self.assertIn("| E99 |", error_list)

    def test_the_readme_points_to_the_error_list(self):
        """Die README bleibt kurz und verweist auf die ausgelagerte Liste."""
        base = Path(__file__).resolve().parent
        readme = (base.parent / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/fehlercodes.md", readme)
        self.assertNotIn("| A02 |", readme)

class UserTextTests(unittest.TestCase):
    """Einheitliche, kurze Nutzertexte (0.15.10)."""

    WORDS = ("häkchen", "anhaken", "angehakt")

    def _visible_strings(self, path):
        """Alle String-Konstanten ohne Docstrings (also die sichtbaren Texte)."""
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                body = getattr(node, "body", None)
                if body and isinstance(body[0], ast.Expr) and isinstance(
                        getattr(body[0], "value", None), ast.Constant):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings):
                yield node.value

    def test_visible_ui_texts_avoid_the_haekchen_wording(self):
        """«Auswahl/ausgewählt» statt «Häkchen/anhaken» – überall im Dialog."""
        base = Path(__file__).resolve().parent
        offenders = [text.strip()[:60]
                     for text in self._visible_strings(base / "ui.py")
                     if any(word in text.casefold() for word in self.WORDS)]
        self.assertEqual([], offenders)

    def test_docs_avoid_the_haekchen_wording(self):
        """Auch README und die Fehlerliste sprechen von Auswahl."""
        base = Path(__file__).resolve().parent.parent
        for name in ("README.md", "docs/fehlercodes.md"):
            text = (base / name).read_text(encoding="utf-8").casefold()
            for word in self.WORDS:
                self.assertNotIn(word, text, "%s: %s" % (name, word))
        self.assertIn("auswählen", (base / "README.md").read_text(
            encoding="utf-8"))

    def test_prepare_error_dialog_shows_only_the_short_message(self):
        """Feldbefund: Der Dialog zeigte Meldung + Traceback doppelt.

        Die Details (inklusive Traceback unerwarteter Fehler) schreibt
        report_error() ins Calibre-Log; der Dialog bekommt genau eine Zeile.
        """
        base = Path(__file__).resolve().parent
        source = (base / "ui.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        body = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == "_show_prepare_error"):
                body = ast.get_source_segment(source, node) or ""
        self.assertIsNotNone(body)
        self.assertIn('report_error(exc, "Vorbereitung", secrets)', body)
        self.assertNotIn("format_error_details", body)
        self.assertNotIn("Traceback", body)

    def test_reported_message_is_one_clean_line(self):
        """Genau «Text (Code A01)» – ohne Zeilenumbruch und ohne Traceback."""
        exc = tolino_module.TolinoAuthError(
            "Die Tolino-Sitzung ist beendet. " + tolino_module.RELOGIN,
            code="A01", detail="HTTP 400: Token is not active")
        with patch.object(tolino_module, "log_warning") as log:
            shown = tolino_module.report_error(exc, "Vorbereitung")
        self.assertEqual(
            "Die Tolino-Sitzung ist beendet. Bitte auf «Jetzt bei Tolino "
            "anmelden» klicken. (Code A01)", shown)
        logged = log.call_args[0][0]
        self.assertIn("A01", logged)
        self.assertIn("Token is not active", logged)
        self.assertNotIn("Token is not active", shown)
        self.assertNotIn("Traceback", shown)

    def test_auth_errors_keep_their_traceback_chain_in_the_log(self):
        """A01 und Co.: Dialog eine Zeile, die Kette samt Werten nur im Log."""
        secret = "geheimwert-77"
        try:
            try:
                raise tolino_module.TolinoApiError("HTTP 400: %s" % secret,
                                                  code="N03")
            except tolino_module.TolinoApiError as inner:
                raise tolino_module.TolinoAuthError(
                    "Die Tolino-Sitzung ist beendet. " + tolino_module.RELOGIN,
                    code="A01", detail="HTTP 400") from inner
        except tolino_module.TolinoAuthError as exc:
            with patch.object(tolino_module, "log_warning") as log:
                shown = tolino_module.report_error(
                    exc, "Vorbereitung", (secret,))
        calls = [call[0][0] for call in log.call_args_list]
        self.assertEqual(1, shown.count("\n") + 1)
        self.assertTrue(shown.endswith("(Code A01)"))
        self.assertTrue(any(c.startswith("Tolino-Details") for c in calls), calls)
        blob = " ".join(calls)
        self.assertIn("Traceback", blob)
        self.assertIn("direct cause", blob)
        self.assertNotIn(secret, blob)
        self.assertNotIn(secret, shown)

    def test_report_error_without_a_traceback_logs_a_single_line(self):
        """Ohne Traceback (z. B. selbst gebaute Meldung) bleibt es eine Zeile."""
        exc = tolino_module.TolinoError("Fehler ohne Traceback", code="S90")
        with patch.object(tolino_module, "log_warning") as log:
            shown = tolino_module.report_error(exc)
        self.assertEqual(1, log.call_count)
        self.assertEqual("Fehler ohne Traceback (Code S90)", shown)
