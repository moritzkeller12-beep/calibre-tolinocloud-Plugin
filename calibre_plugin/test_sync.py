import base64
import unittest
import ast
import json
import os
import tempfile
import time
import zipfile
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from . import config
from .sync import (compare_inventory, cover_bytes, fingerprint, iter_book_ids,
                   normalize_formats, plan_sync, safe_format_path,
                   selected_book_ids, selected_table_rows, sync_summary,
                   unpack_plan_result, unpack_upload_record, redact_sensitive,
                   format_diagnostic_report, diagnose_preparation,
                   _diagnostic_formats, metadata_by_id, format_error_details,
                   normalize_title, normalize_inventory_item, custom_column_available,
                   metadata_tolino_id, update_tolino_ids, TOLINO_COLUMN)
from .tolino import (PARTNERS, TolinoAuthError, TolinoApiError,
                     callback_redirect_uri,
                     TolinoClient, hardware_id,
                     force_legacy_partner_id, normalize_hardware_id,
                     normalize_refresh_token, resolve_partner_id,
                     sanitize_error,
                     validate_callback, scrape_browser_tokens, browser_login,
                     validate_refresh_candidates)
import calibre_plugin.tolino as tolino_module


# The login helpers must not open a REAL browser window during tests.
# On machines that have Chrome/Chromium installed (CI runners do),
# pick_chromium() would succeed and launch_reader_window() would spawn a
# browser the tests never asked for -- every login test that asserts the
# no-Chromium fallback would then fail or hang. Tests that need a
# browser patch calibre_plugin.cdp.pick_chromium themselves.
_PICK_CHROMIUM_PATCH = None


def setUpModule():
    global _PICK_CHROMIUM_PATCH
    _PICK_CHROMIUM_PATCH = patch("calibre_plugin.cdp.pick_chromium",
                                 return_value=None)
    _PICK_CHROMIUM_PATCH.start()


def tearDownModule():
    global _PICK_CHROMIUM_PATCH
    if _PICK_CHROMIUM_PATCH is not None:
        _PICK_CHROMIUM_PATCH.stop()
        _PICK_CHROMIUM_PATCH = None


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

    def test_iter_book_ids_falls_back_to_library_database_all_ids(self):
        class Database:
            data = object()

            def all_ids(self):
                return (8, 3, 8)

        self.assertEqual([8, 3], list(iter_book_ids(Database())))

    def test_iter_book_ids_uses_gui_all_book_ids_before_legacy_all_ids(self):
        class Database:
            data = object()

            def all_book_ids(self):
                return (18, 5, 18)

            def all_ids(self):
                raise AssertionError("must prefer all_book_ids")

        self.assertEqual([18, 5], list(iter_book_ids(Database())))

    def test_iter_book_ids_uses_data_id_fallback_before_all_ids(self):
        class Data:
            def iterallids(self):
                return iter((7, 1, 7))

        class Database:
            data = Data()

            def all_ids(self):
                raise AssertionError("must prefer data.iterallids")

        self.assertEqual([7, 1], list(iter_book_ids(Database())))

    def test_iter_book_ids_ignores_rows_without_existing_book_id(self):
        class Data:
            def iterall(self):
                return iter((object(), {"title": "not a book id"}))

        class Database:
            data = Data()

            def all_ids(self):
                raise AssertionError("must not use fallback after iterall")

        self.assertEqual([], list(iter_book_ids(Database())))

    def test_iter_book_ids_accepts_calibre_tuple_rows_and_skips_short_rows(self):
        class Data:
            def iterall(self):
                return iter(((12, "title"), (), (13,), ("not-an-id", "title")))

        class Database:
            data = Data()

        self.assertEqual([12, 13], list(iter_book_ids(Database())))

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

    def test_custom_column_uses_supported_set_field_api(self):
        class Database:
            field_metadata = {TOLINO_COLUMN: {"datatype": "text"}}

            def set_field(self, field, values):
                self.field, self.values = field, values

        database = Database()
        self.assertTrue(custom_column_available(database))
        self.assertEqual(1, update_tolino_ids(database, [(7, "bosh_8_new")]))
        self.assertEqual(TOLINO_COLUMN, database.field)
        self.assertEqual({7: "bosh_8_new"}, database.values)

    def test_custom_column_missing_does_not_use_sql_fallback(self):
        class Database:
            field_metadata = {}

        self.assertFalse(custom_column_available(Database()))
        self.assertEqual(0, update_tolino_ids(Database(), [(7, "bosh_8_new")]))

    def test_custom_column_can_be_disabled_even_when_present(self):
        class Database:
            field_metadata = {TOLINO_COLUMN: {"datatype": "text"}}

            def set_field(self, field, values):
                raise AssertionError("disabled column must not be written")

        self.assertEqual(0, update_tolino_ids(
            Database(), [(7, "bosh_8_new")], enabled=False))

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
        self.assertEqual(normalize_title("Der „Überblick“ – Band 2.epub"),
                         normalize_title("DER Überblick - Band 2"))
        metadata = {1: {"uuid": "u1", "title": "Der „Überblick“ – Band 2",
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

    def test_explicit_selection_gates_changed_uploads(self):
        metadata = {1: dict(self.book)}
        old = {"u1": {"tolino_id": "t1", "fingerprint": "old"}}
        self.assertEqual([], plan_sync(metadata, old, ["EPUB"], upload_book_ids=set())[0])

    def test_unchanged_book_is_not_uploaded(self):
        old = {"u1": {"tolino_id": "t1", "fingerprint": fingerprint(self.book, "EPUB")}}
        self.assertEqual([], plan_sync({1: self.book}, old, ["EPUB"])[0])

    def test_selected_unchanged_book_can_be_uploaded_again(self):
        old = {"u1": {"tolino_id": "t1", "fingerprint": fingerprint(self.book, "EPUB")}}
        self.assertEqual(
            [(1, "u1", "EPUB", "t1")],
            plan_sync({1: self.book}, old, ["EPUB"], upload_book_ids={1})[0],
        )

    def test_changed_book_is_uploaded(self):
        old = {"u1": {"tolino_id": "t1", "fingerprint": "old"}}
        self.assertEqual("t1", plan_sync({1: self.book}, old, ["EPUB"])[0][0][3])

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
            with self.assertRaisesRegex(TolinoAuthError, "Web Reader again"):
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
            with self.assertRaisesRegex(TolinoAuthError, "Web Reader again"):
                TolinoClient(4, "", "old-refresh").login()

    def test_403_bot_protection_reports_response_detail(self):
        from urllib.error import HTTPError

        body = b"Access denied | bot protection"
        error = HTTPError("https://example.invalid/token", 403, "Forbidden", {}, None)
        error.read = lambda: body
        client = TolinoClient(4, "", "old-refresh")
        with patch("calibre_plugin.tolino._curl_binary", return_value=None), \
                patch("calibre_plugin.tolino._impersonate_session", return_value=None), \
                        patch("calibre_plugin.tolino.urlopen", side_effect=error):
            with self.assertRaisesRegex(TolinoAuthError, "Access denied"):
                client.login()
        self.assertEqual(403, client.last_http_status)
        self.assertIn("Access denied", client.last_error_text)
        self.assertEqual("urllib", client.last_transport)

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
            with self.assertRaisesRegex(TolinoAuthError, "Web Reader again"):
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

    def test_preparation_error_keeps_traceback_and_redacts_without_dashboard_settings(self):
        try:
            raise IndexError("tuple index out of range; refresh-secret")
        except Exception as exc:
            rendered = format_error_details(exc, ("refresh-secret",))
        self.assertIn("IndexError", rendered)
        self.assertIn("tuple index out of range", rendered)
        self.assertIn("Traceback", rendered)
        self.assertNotIn("refresh-secret", rendered)
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
                "bootstrapper.py",
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
        """The bundled image must be a real PNG.

        It shipped as a WebP file with a .png extension once; Qt picks the
        decoder by extension, fails to decode, and the toolbar icon stayed
        empty.
        """
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
        """Orell Füssli (reseller 8) must use the guided Web Reader login.

        Its Keycloak rejects localhost callback redirect URIs with
        "Ungültiger Parameter: redirect_uri". The routing keys on the
        stable reseller_id, not the internal partner id (4 after
        renumbering) - the v0.9.9 guided login was never reached because
        the old check compared against the historic id 8.
        """
        opened = []
        clock = {"t": 1000.0}

        def fake_time():
            clock["t"] += 1000.0
            return clock["t"]

        with patch("calibre_plugin.tolino.webbrowser.open",
                   side_effect=lambda url: opened.append(url) or True), \
             patch("calibre_plugin.tolino.scrape_browser_tokens",
                   return_value=(None, [], [])), \
             patch("calibre_plugin.tolino.time.time", side_effect=fake_time), \
             patch("calibre_plugin.tolino.time.sleep", lambda _s: None):
            with self.assertRaises(TolinoAuthError):
                browser_login(4, "test_hardware")
        self.assertEqual(1, len(opened))
        self.assertEqual(PARTNERS[4]["reader_url"], opened[0])
        self.assertNotIn("127.0.0.1", opened[0])
        self.assertNotIn("redirect_uri", opened[0])

    def test_browser_login_keeps_localhost_callback_for_other_partners(self):
        opened = []
        with patch("calibre_plugin.tolino.webbrowser.open",
                   side_effect=lambda url: opened.append(url) or True):
            with self.assertRaises(TolinoAuthError):
                browser_login(1, "test_hardware", timeout=0.2)
        self.assertEqual(1, len(opened))
        self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1", opened[0])

    def test_keycloak_assisted_login_validates_each_new_candidate_once(self):
        """Every newly harvested candidate is validated exactly once.

        Validating a refresh token rotates it; an "invalid_grant" answer
        means the candidate is dead forever and must be remembered, so it
        is never retried on a later poll. A fresh candidate that shows up
        later (e.g. the reader's next background rotation) is still picked
        up and validated.
        """
        from .tolino import _keycloak_assisted_login

        validations = []

        def fake_validate(partner_id, hw, candidate):
            validations.append(candidate)
            if candidate == "fresh-1":
                return ("ok", "rotated-fresh", "hw1")
            return ("spent", None, None)

        polls = {"n": 0}

        def fake_scrape(diagnose=False, all_candidates=False):
            polls["n"] += 1
            if polls["n"] == 1:
                return (["spent-1"], ["hw1"], ["note"])
            if polls["n"] == 2:
                return (["spent-1", "spent-2"], ["hw1"], ["note"])
            # The reader's rotation wrote a fresh token.
            return (["spent-1", "spent-2", "fresh-1"], ["hw1"], ["note"])

        clock = {"t": 1000.0}

        def fake_time():
            clock["t"] += 5.0
            return clock["t"]

        with patch("calibre_plugin.tolino.webbrowser.open",
                   return_value=True), \
             patch("calibre_plugin.tolino.scrape_browser_tokens",
                   side_effect=fake_scrape), \
             patch("calibre_plugin.tolino._validate_one",
                   side_effect=fake_validate), \
             patch("calibre_plugin.tolino.time.time", side_effect=fake_time), \
             patch("calibre_plugin.tolino.time.sleep", lambda _s: None):
            refresh, hardware = _keycloak_assisted_login(4, "test_hardware")
        self.assertEqual(("rotated-fresh", "hw1"), (refresh, hardware))
        # Each candidate was tried exactly once, in harvest order; the
        # fresh one is adopted, no dead token was ever re-tested.
        self.assertEqual(["spent-1", "spent-2", "fresh-1"], validations)

    def test_keycloak_assisted_login_reports_close_reader_hint(self):
        """When candidates were harvested but all were rejected, the error
        must tell the user to CLOSE the Web Reader before retrying: an open
        reader consumes/rotates the token in the background, which is the
        most common reason every candidate is already spent."""
        from .tolino import _keycloak_assisted_login

        clock = {"t": 1000.0}

        def fake_time():
            clock["t"] += 10.0
            return clock["t"]

        with patch("calibre_plugin.tolino.webbrowser.open", return_value=True), \
             patch("calibre_plugin.tolino.scrape_browser_tokens",
                   return_value=(["cand1"], ["hw1"], ["note"])), \
             patch("calibre_plugin.tolino._validate_one",
                   return_value=("spent", None, None)), \
             patch("calibre_plugin.tolino.time.time", side_effect=fake_time), \
             patch("calibre_plugin.tolino.time.sleep", lambda _s: None):
            with self.assertRaises(TolinoAuthError) as ctx:
                _keycloak_assisted_login(4, "test_hardware")
        message = str(ctx.exception)
        self.assertIn("SCHLIESSEN", message)
        self.assertIn("1 gefundene(n)", message)

    def test_keycloak_assisted_login_keeps_polling_after_exhausted(self):
        """A run that exhausts all candidates must not end immediately.

        The freshest token often reaches disk only when the user closes
        the Web Reader tab (Chromium flushes Local Storage on close), so
        the loop must keep polling and adopt the late candidate instead
        of failing with "all candidates spent".
        """
        from .tolino import _keycloak_assisted_login

        polls = {"n": 0}
        validations = []

        def fake_scrape(diagnose=False, all_candidates=False):
            polls["n"] += 1
            if polls["n"] <= 10:
                return (["dead-1", "dead-2"], ["hw1"], ["note"])
            # The user closed the reader tab: the fresh token lands late.
            return (["dead-1", "dead-2", "late-fresh"], ["hw1"], ["note"])

        def fake_validate(partner_id, hw, candidate):
            validations.append(candidate)
            if candidate == "late-fresh":
                return ("ok", "rotated-late", "hw1")
            return ("spent", None, None)

        clock = {"t": 1000.0}

        def fake_time():
            clock["t"] += 5.0
            return clock["t"]

        with patch("calibre_plugin.tolino.webbrowser.open",
                   return_value=True), \
             patch("calibre_plugin.tolino.scrape_browser_tokens",
                   side_effect=fake_scrape), \
             patch("calibre_plugin.tolino._validate_one",
                   side_effect=fake_validate), \
             patch("calibre_plugin.tolino.time.time", side_effect=fake_time), \
             patch("calibre_plugin.tolino.time.sleep", lambda _s: None):
            refresh, hardware = _keycloak_assisted_login(4, "test_hardware")
        self.assertEqual(("rotated-late", "hw1"), (refresh, hardware))
        # The dead pair was validated once and then never again.
        self.assertEqual(["dead-1", "dead-2", "late-fresh"], validations)

    def test_keycloak_assisted_login_retries_unclear_candidates(self):
        """Unclear verdicts are retried after a cooldown, not declared dead.

        A network error or bot-protection page proves nothing about the
        token itself: the candidate must stay alive and be retried once
        the transport recovers. It must not crowd out newer candidates
        every round either (30 s cooldown per candidate).
        """
        from .tolino import _keycloak_assisted_login

        attempts = {"tok": 0}
        attempts_dead = []

        def fake_validate(partner_id, hw, candidate):
            if candidate == "tok":
                attempts["tok"] += 1
                if attempts["tok"] >= 3:
                    return ("ok", "rotated", "hw1")
                return ("unclear", None, None)
            attempts_dead.append(candidate)
            return ("spent", None, None)

        polls = {"n": 0}

        def fake_scrape(diagnose=False, all_candidates=False):
            polls["n"] += 1
            return (["tok", "dead"], ["hw1"], ["note"])

        clock = {"t": 1000.0}

        def fake_time():
            clock["t"] += 30.0
            return clock["t"]

        with patch("calibre_plugin.tolino.webbrowser.open",
                   return_value=True), \
             patch("calibre_plugin.tolino.scrape_browser_tokens",
                   side_effect=fake_scrape), \
             patch("calibre_plugin.tolino._validate_one",
                   side_effect=fake_validate), \
             patch("calibre_plugin.tolino.time.time", side_effect=fake_time), \
             patch("calibre_plugin.tolino.time.sleep", lambda _s: None):
            refresh, hardware = _keycloak_assisted_login(4, "test_hardware")
        self.assertEqual(("rotated", "hw1"), (refresh, hardware))
        # Two unclear rounds plus the successful third attempt; the dead
        # candidate was spent from round one, so it is never retried.
        self.assertEqual(3, attempts["tok"])
        self.assertEqual(["dead"], attempts_dead)

    def test_validate_one_network_failure_is_unclear_not_spent(self):
        """A transport failure wrapped by _login must NOT burn a candidate.

        TolinoClient._login wraps every underlying error (network reset,
        WAF page, 5xx) in TolinoAuthError. Only a definitive Keycloak
        verdict inside the message means the token is dead; anything else
        must stay retryable ("unclear"), otherwise a single hiccup makes
        the flow abandon a still-live token forever.
        """
        from .tolino import _validate_one

        def wrapped_network_error(self):
            raise TolinoAuthError(
                "Tolino authentication failed: <urlopen error [Errno -3] "
                "Temporary failure in name resolution>")

        def bot_protection_page(self):
            raise TolinoAuthError(
                "Tolino authentication failed: Zugriff geblockt")

        def session_not_active(self):
            raise TolinoAuthError(
                'Tolino HTTP 400: {"error": "invalid_grant", '
                '"error_description": "Token is not active"}')

        cases = [
            (wrapped_network_error, "unclear"),
            (bot_protection_page, "unclear"),
            (session_not_active, "spent"),
        ]
        for login_impl, expected in cases:
            with patch.object(tolino_module.TolinoClient, "_login",
                              login_impl):
                verdict, rotated, _hw = _validate_one(4, "hw-x", "tok")
            self.assertEqual(expected, verdict)

    def test_keycloak_assisted_login_one_post_per_candidate(self):
        """Each candidate gets exactly ONE token-endpoint attempt.

        The token exchange does not involve the hardware ID, so the old
        loop over up to three hardware candidates sent redundant replays;
        a replayed token can trip Keycloak's reuse protection and kill
        the session. Every verdict must now come from a single call.
        """
        from .tolino import _keycloak_assisted_login

        validations = []

        def fake_validate(partner_id, hw, candidate):
            validations.append((candidate, hw))
            if candidate == "fresh-1":
                return ("ok", "rotated-fresh", "hw1")
            return ("spent", None, None)

        polls = {"n": 0}

        def fake_scrape(diagnose=False, all_candidates=False):
            polls["n"] += 1
            if polls["n"] == 1:
                return (["spent-1", "spent-2"], ["hw1", "hw2"], ["note"])
            return (["spent-1", "spent-2", "fresh-1"], ["hw1", "hw2"],
                    ["note"])

        clock = {"t": 1000.0}

        def fake_time():
            clock["t"] += 5.0
            return clock["t"]

        with patch("calibre_plugin.tolino.webbrowser.open",
                   return_value=True), \
             patch("calibre_plugin.tolino.scrape_browser_tokens",
                   side_effect=fake_scrape), \
             patch("calibre_plugin.tolino._validate_one",
                   side_effect=fake_validate), \
             patch("calibre_plugin.tolino.time.time", side_effect=fake_time), \
             patch("calibre_plugin.tolino.time.sleep", lambda _s: None):
            refresh, hardware = _keycloak_assisted_login(4, "test_hardware")
        self.assertEqual(("rotated-fresh", "hw1"), (refresh, hardware))
        # Every candidate validated exactly once -- not once per hardware ID.
        self.assertEqual(
            [("spent-1", "test_hardware"), ("spent-2", "test_hardware"),
             ("fresh-1", "test_hardware")],
            validations)

    def test_keycloak_assisted_login_prefers_newest_token_per_session(self):
        """Only the freshest sibling of one Keycloak session is validated.

        All refresh tokens of one web-reader login share the 'sid' claim;
        within a session only the newest 'iat' can be valid, and replaying
        an older sibling may trigger Keycloak's reuse protection and kill
        the live session. The selector must therefore validate at most one
        token per session -- the newest -- and skip sessions that already
        produced a spent token during this run.
        """
        from .tolino import _keycloak_assisted_login, _select_candidate_round

        import base64 as b64

        def jwt(payload):
            head = b64.urlsafe_b64encode(b'{"alg":"HS512","typ":"JWT"}')
            body = b64.urlsafe_b64encode(json.dumps(payload).encode())
            return "%s.%s.sig" % (head.decode().rstrip("="),
                                  body.decode().rstrip("="))

        old_sibling = jwt({"iat": 100, "sid": "sess-A", "typ": "Refresh"})
        new_sibling = jwt({"iat": 900, "sid": "sess-A", "typ": "Refresh"})
        other_session_newest = jwt({"iat": 500, "sid": "sess-B",
                                    "typ": "Refresh"})
        older_b = jwt({"iat": 400, "sid": "sess-B", "typ": "Refresh"})
        no_sid = jwt({"iat": 950, "typ": "Refresh"})

        # Pure selector behaviour: freshest per session, order preserved.
        picked = _select_candidate_round(
            [old_sibling, new_sibling, older_b, other_session_newest, no_sid],
            set())
        self.assertEqual([new_sibling, other_session_newest, no_sid], picked)

        # A session with a spent sibling is skipped entirely.
        picked = _select_candidate_round(
            [new_sibling, other_session_newest], {old_sibling})
        self.assertEqual([other_session_newest], picked)

        validations = []

        def fake_validate(partner_id, hw, candidate):
            validations.append(candidate)
            return ("spent", None, None)

        polls = {"n": 0}

        def fake_scrape(diagnose=False, all_candidates=False):
            polls["n"] += 1
            return ([old_sibling, new_sibling, older_b,
                     other_session_newest, no_sid], ["hw1"], ["note"])

        clock = {"t": 1000.0}

        def fake_time():
            clock["t"] += 5.0
            return clock["t"]

        with patch("calibre_plugin.tolino.webbrowser.open",
                   return_value=True), \
             patch("calibre_plugin.tolino.scrape_browser_tokens",
                   side_effect=fake_scrape), \
             patch("calibre_plugin.tolino._validate_one",
                   side_effect=fake_validate), \
             patch("calibre_plugin.tolino.time.time", side_effect=fake_time), \
             patch("calibre_plugin.tolino.time.sleep", lambda _s: None):
            with self.assertRaises(TolinoAuthError):
                _keycloak_assisted_login(4, "test_hardware")
        # Exactly one endpoint attempt per session (the newest sibling of
        # sess-A and sess-B each) plus the sid-less token; the OLDER
        # siblings old_sibling and older_b were never replayed, and the
        # dead sessions were not retried in later rounds.
        self.assertEqual([new_sibling, other_session_newest, no_sid],
                         validations)

    def test_validate_one_classifies_spent_vs_unclear(self):
        """Only a definitive Keycloak rejection counts as spent."""
        from .tolino import _validate_one

        def ok_login(self):
            self.refresh = self.refresh + "-rotated"

        def invalid_grant(self):
            raise TolinoApiError(
                'Tolino HTTP 400: {"error": "invalid_grant", '
                '"error_description": "Token is not active"}')

        def network_down(self):
            raise TolinoApiError("Tolino request failed: connection reset")

        cases = [
            (invalid_grant, "spent"),
            (network_down, "unclear"),
            (ok_login, "ok"),
        ]
        for login_impl, expected in cases:
            with patch.object(tolino_module.TolinoClient, "_login",
                              login_impl):
                verdict, rotated, _hw = _validate_one(4, "hw-x", "tok")
            self.assertEqual(expected, verdict)
            if expected == "ok":
                self.assertEqual("tok-rotated", rotated)

    def test_validate_refresh_candidates_retries_unclear_calls(self):
        """The list wrapper skips unclear candidates without consuming."""
        from .tolino import _validate_one
        attempts = []

        def flaky_login(self):
            attempts.append(self.refresh)
            if self.refresh == "flaky":
                raise TolinoApiError("Tolino request failed: timeout")
            raise TolinoAuthError("invalid_grant: nope")

        with patch.object(tolino_module.TolinoClient, "_login", flaky_login):
            self.assertIsNone(validate_refresh_candidates(
                4, "hw-x", ["flaky", "dead"]))
        self.assertEqual(["flaky", "dead"], attempts)

    def test_jwt_helpers_parse_iat_and_age(self):
        """JWT payload parsing extracts iat; age text reflects freshness."""
        import base64 as b64
        from .tolino import (_refresh_token_iat,
                             _candidate_age_text, _jwt_shaped)

        def make_jwt(payload):
            head = b64.urlsafe_b64encode(b'{"alg":"HS512","typ":"JWT"}')
            body = b64.urlsafe_b64encode(json.dumps(payload).encode())
            return "%s.%s.sig" % (head.decode().rstrip("="),
                                  body.decode().rstrip("="))

        now = time.time()
        fresh = make_jwt({"iat": now - 30, "typ": "Refresh"})
        old = make_jwt({"iat": now - 3 * 3600, "typ": "Refresh"})
        self.assertEqual(now - 30, _refresh_token_iat(fresh))
        self.assertIsNone(_refresh_token_iat("plain-not-a-jwt"))
        self.assertTrue(_jwt_shaped(fresh))
        self.assertFalse(_jwt_shaped("short.not.jwt"))
        self.assertIn("gerade geschrieben", _candidate_age_text(fresh, now))
        self.assertIn("3 h", _candidate_age_text(old, now))

    def test_sort_candidates_orders_jwt_tokens_by_issue_time(self):
        """Tokens with a JWT iat sort newest-first, ahead of opaque ones."""
        import base64 as b64
        from .tolino import _sort_candidates_by_recency, _RECENCY_BUCKETS

        def make_jwt(iat):
            body = b64.urlsafe_b64encode(json.dumps({"iat": iat}).encode())
            return "h.%s.s" % body.decode().rstrip("=")

        now = time.time()
        old_jwt = make_jwt(now - 7200)
        fresh_jwt = make_jwt(now - 60)
        opaque = "o" * 120
        try:
            _RECENCY_BUCKETS.clear()
            _RECENCY_BUCKETS[opaque] = 0  # freshest storage tier
            ordered = _sort_candidates_by_recency([old_jwt, opaque, fresh_jwt])
            self.assertEqual([fresh_jwt, old_jwt, opaque], ordered)
        finally:
            _RECENCY_BUCKETS.clear()

    def test_extract_all_tokens_sweeps_opaque_oidc_keys(self):
        """The Keycloak reader's oidc.user blob must yield its token.

        The web reader stores its CURRENT token set under a key like
        'oidc.user:<issuer>:webreader' whose name carries no credential
        hint, while only historical copies sit under refresh_token keys.
        The sweep must find the live token through shape detection.
        """
        import base64 as b64
        from .tolino import _extract_all_tokens_from_storage

        body = b64.urlsafe_b64encode(json.dumps({
            "iat": time.time() - 30, "typ": "Refresh"}).encode())
        fresh_jwt = "eyJhbGciOiJIUzUxMiJ9.%s.sigpart9sigpart9" % (
            body.decode().rstrip("="))

        storage = {
            "webreader.mytolino.com/refresh_token": "spent-old",
            "webreader.mytolino.com/oidc.user:https://issuer:webreader":
                json.dumps({"refresh_token": fresh_jwt,
                            "hardware_id": "hw-oidc"}),
        }
        refreshes, hardwares = _extract_all_tokens_from_storage(storage)
        self.assertIn("spent-old", refreshes)
        self.assertIn(fresh_jwt, refreshes)
        self.assertIn("hw-oidc", hardwares)

    def test_extract_all_tokens_does_not_sweep_unrelated_values(self):
        """Swept non-credential values must never become candidates."""
        from .tolino import _extract_all_tokens_from_storage

        storage = {
            "webreader.mytolino.com/settings":
                '{"font": "serif", "theme": "dark"}',
            "webreader.mytolino.com/lastPage": "42",
            "webreader.mytolino.com/refresh_token": "spent-old",
        }
        refreshes, _hardwares = _extract_all_tokens_from_storage(storage)
        self.assertEqual(["spent-old"], refreshes)

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

    def test_keepalive_refresh_skips_without_token(self):
        with patch("calibre_plugin.tolino.TolinoClient") as client_mock:
            result = tolino_module.keepalive_refresh(lambda: {}, None)
        self.assertIsNone(result)
        client_mock.assert_not_called()

    def test_keepalive_refresh_skips_when_already_running(self):
        with tolino_module._KEEPALIVE_LOCK, \
             patch("calibre_plugin.tolino.TolinoClient") as client_mock:
            result = tolino_module.keepalive_refresh(
                lambda: {"refresh_token": "tok"}, None)
        self.assertIsNone(result)
        client_mock.assert_not_called()

    def test_keycloak_assisted_login_opens_web_reader_not_authorize_url(self):
        """Keycloak partners must open the Web Reader page itself.

        A hand-built authorize URL is rejected by Keycloak with
        "Ung\u00fctiger Parameter: redirect_uri" (only registered redirect
        URIs are accepted), which is exactly the reported browser-login
        failure for Orell F\u00fcssli.
        """
        from .tolino import _keycloak_assisted_login

        clock = {"t": 1000.0}

        def fake_time():
            clock["t"] += 1000.0
            return clock["t"]

        with patch("calibre_plugin.tolino.webbrowser.open",
                   return_value=True) as open_mock, \
             patch("calibre_plugin.tolino.time.time",
                   side_effect=fake_time), \
             patch("calibre_plugin.tolino.time.sleep", lambda _s: None):
            with self.assertRaises(TolinoAuthError):
                _keycloak_assisted_login(4, "test_hardware")
        opened = open_mock.call_args[0][0]
        self.assertEqual(PARTNERS[4]["reader_url"], opened)
        self.assertNotIn("redirect_uri", opened)
        self.assertNotIn("response_type", opened)

    def test_scrape_browser_tokens_returns_none_when_not_found(self):
        with patch("calibre_plugin.tolino._find_browser_storage_paths",
                   return_value=[]):
            refresh, hardware = scrape_browser_tokens()
        self.assertIsNone(refresh)
        self.assertIsNone(hardware)

    def test_scrape_browser_tokens_diagnose_lists_scanned_locations(self):
        with patch("calibre_plugin.tolino._find_browser_storage_paths",
                   return_value=["/nonexistent/path"]):
            refresh, hardware, notes = scrape_browser_tokens(diagnose=True)
        self.assertIsNone(refresh)
        self.assertIsNone(hardware)
        self.assertTrue(notes)

    def test_walk_discovery_finds_chromium_profile_and_reports_diagnosis(self):
        from .tolino import _collect_storage_under
        import struct as _struct

        def varint(value):
            out = bytearray()
            while True:
                byte = value & 0x7F
                value >>= 7
                if value:
                    out.append(byte | 0x80)
                else:
                    out.append(byte)
                    return bytes(out)

        with tempfile.TemporaryDirectory() as tmp:
            profile = os.path.join(tmp, "Default")
            db_dir = os.path.join(profile, "Local Storage", "leveldb")
            os.makedirs(db_dir)
            key = b"_https://webreader.mytolino.com\x00\x01refresh_token"
            value = b"\x01tok-walk"
            payload = (_struct.pack("<QI", 1, 1) + b"\x01"
                       + varint(len(key)) + key + varint(len(value)) + value)
            with open(os.path.join(db_dir, "000003.log"), "wb") as fh:
                fh.write(_struct.pack("<IHB", 0, len(payload), 1) + payload)

            notes = []
            found = _collect_storage_under(tmp, notes)
            self.assertTrue(any("refresh" in k for k in found), found)
            self.assertTrue(any("tok-walk" == v for v in found.values()), found)
            self.assertTrue(any("Chromium-Storage" in n for n in notes), notes)

            # Full scrape path returns the token pair
            refresh, hardware = scrape_browser_tokens()
        self.assertIsNone(refresh)  # real machine: no real browser with token

    def test_diagnose_reports_found_tolino_keys_when_token_shape_missing(self):
        with patch("calibre_plugin.tolino._find_browser_storage_paths",
                   return_value=["/fake/profile"]), \
                patch("calibre_plugin.tolino.os.path.isdir", return_value=True), \
                patch("calibre_plugin.tolino._collect_storage_under",
                      return_value={
                          "webreader.mytolino.com/some_flag": "1",
                          "other.example.com/refresh_token": "hidden",
                      }):
            refresh, hardware, notes = scrape_browser_tokens(diagnose=True)
        self.assertIsNone(refresh)
        joined = "\n".join(notes)
        self.assertIn("webreader.mytolino.com/some_flag", joined)
        self.assertIn("geschwärzt", joined)
        self.assertNotIn("hidden", joined)

    def test_diagnose_hints_reader_when_no_tolino_keys(self):
        with patch("calibre_plugin.tolino._find_browser_storage_paths",
                   return_value=["/fake/profile"]), \
                patch("calibre_plugin.tolino.os.path.isdir", return_value=True), \
                patch("calibre_plugin.tolino._collect_storage_under",
                      return_value={"other.example.com/x": "1"}):
            refresh, hardware, notes = scrape_browser_tokens(diagnose=True)
        self.assertIsNone(refresh)
        self.assertTrue(any("Web Reader" in n for n in notes), notes)

    def test_diagnose_lists_checked_paths_when_nothing_found(self):
        missing = tempfile.mkdtemp()  # exists but empty -> no storage notes
        self.addCleanup(__import__("shutil").rmtree, missing, True)
        with patch("calibre_plugin.tolino._find_browser_storage_paths",
                   return_value=[missing, "/definitely/missing/path"]):
            refresh, hardware, notes = scrape_browser_tokens(diagnose=True)
        self.assertIsNone(refresh)
        self.assertIsNone(hardware)
        joined = "\n".join(notes)
        self.assertIn(missing, joined)
        self.assertIn("/definitely/missing/path", joined)
        self.assertIn("fehlt", joined)

    def test_extract_tokens_from_storage_reads_leveldb_records(self):
        from .tolino import _extract_tokens_from_storage
        storage = {
            "webreader.mytolino.com/refresh_token": "tok-abc",
            "webreader.mytolino.com/hardware_id": "hw-77",
            "unrelated.example.com/other": "nope",
        }
        self.assertEqual(
            ("tok-abc", "hw-77"),
            _extract_tokens_from_storage(storage),
        )

    def test_leveldb_log_parser_extracts_origin_keyed_records(self):
        from .tolino import (_leveldb_records_from_log, _scan_chromium_leveldb,
                             _TOLINO_ORIGIN_MARKER)
        import struct as _struct

        def varint(value):
            out = bytearray()
            while True:
                byte = value & 0x7F
                value >>= 7
                if value:
                    out.append(byte | 0x80)
                else:
                    out.append(byte)
                    return bytes(out)

        def writebatch(key, value):
            payload = (_struct.pack("<QI", 1, 1)  # sequence + count
                       + b"\x01" + varint(len(key)) + key
                       + varint(len(value)) + value)
            # FULL record: crc(4) + length(2) + type(1) + payload
            return _struct.pack("<IHB", 0, len(payload), 1) + payload

        key = b"_https://webreader.mytolino.com\x00\x01refresh_token"
        value = b"\x01tok-from-log"
        blob = writebatch(key, value)
        pairs = list(_leveldb_records_from_log(blob))
        self.assertEqual([(key, value)], pairs)

        # The directory scanner reads .log files and returns origin-keyed data
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = os.path.join(tmp, "leveldb")
            os.makedirs(db_dir)
            with open(os.path.join(db_dir, "000003.log"), "wb") as fh:
                fh.write(blob)
            found = _scan_chromium_leveldb(db_dir)
        self.assertTrue(any("refresh" in key for key in found), found)
        self.assertTrue(any("tok-from-log" == value for value in found.values()), found)
        self.assertIn(_TOLINO_ORIGIN_MARKER, blob)

    def test_firefox_lsng_reader_decodes_blob_values(self):
        import sqlite3
        from .tolino import _read_firefox_storage
        with tempfile.TemporaryDirectory() as tmp:
            # Modern LSNG per-origin database: storage/default/<origin>/ls/
            origin_dir = os.path.join(tmp, "storage", "default",
                                      "https+++webreader.mytolino.com", "ls")
            os.makedirs(origin_dir)
            db_path = os.path.join(origin_dir, "data.sqlite")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE data (key TEXT PRIMARY KEY, "
                         "utf16_length INTEGER, conversion_type INTEGER, "
                         "compression_type INTEGER, last_access_time INTEGER, "
                         "value BLOB)")
            # conversion_type 0 => raw UTF-16LE units (Firefox's NONE mode)
            conn.execute(
                "INSERT INTO data (key, conversion_type, value) "
                "VALUES (?, 0, ?)",
                ("refresh_token", "tok-ff-utf16".encode("utf-16-le")))
            # conversion_type 1 (UTF16_UTF8) => UTF-8 text
            conn.execute(
                "INSERT INTO data (key, conversion_type, value) "
                "VALUES (?, 1, ?)",
                ("hardware_id", b"hw-ff-utf8"))
            conn.commit()
            conn.close()
            storage = _read_firefox_storage(tmp)
        self.assertEqual(storage.get(
            "https+++webreader.mytolino.com/refresh_token"), "tok-ff-utf16")
        self.assertEqual(storage.get(
            "https+++webreader.mytolino.com/hardware_id"), "hw-ff-utf8")

    def test_legacy_webappsstore2_columns_are_read(self):
        import sqlite3
        from .tolino import _read_firefox_storage
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "webappsstore.sqlite")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE webappsstore2 (originAttributes TEXT, "
                         "originKey TEXT, scope TEXT, key TEXT, value BLOB)")
            conn.execute(
                "INSERT INTO webappsstore2 VALUES (?, ?, ?, ?, ?)",
                ("", "https://webreader.mytolino.com", "",
                 "refresh_token", "legacy-tok"))
            conn.commit()
            conn.close()
            storage = _read_firefox_storage(tmp)
        self.assertEqual(storage.get(
            "https://webreader.mytolino.com/refresh_token"), "legacy-tok")

    def test_firefox_session_storage_reads_mozlz4_sessionstore(self):
        import json as json_module
        import struct
        from .tolino import _read_firefox_session_storage, _read_mozlz4

        def lz4_block(data):
            # Final-sequence-only encoder: literals without a trailing match.
            n = len(data)
            if n <= 15:
                return bytes([n << 4]) + data
            continuation = bytearray()
            remaining = n - 15
            while remaining >= 255:
                continuation.append(255)
                remaining -= 255
            continuation.append(remaining)
            return bytes([0xF0]) + bytes(continuation) + data

        payload = json_module.dumps({
            "windows": [{"tabs": [{"entries": [{
                "url": "https://webreader.mytolino.com/library/",
                "storage": {"session": {
                    "https://webreader.mytolino.com": {
                        "t_auth_token": "sess-tok-1"},
                }},
            }]}]}],
        }).encode()
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = os.path.join(tmp, "sessionstore-backups")
            os.makedirs(backup_dir)
            path = os.path.join(backup_dir, "recovery.jsonlz4")
            with open(path, "wb") as handle:
                handle.write(b"mozLz40\x00"
                             + struct.pack("<I", len(payload))
                             + lz4_block(payload))
            self.assertEqual(_read_mozlz4(path), payload)
            storage = _read_firefox_session_storage(tmp)
        self.assertEqual(storage.get(
            "https://webreader.mytolino.com/t_auth_token"), "sess-tok-1")

    def test_cryptojs_decrypt_reads_reader_user_token(self):
        from .tolino import cryptojs_decrypt, _extract_tokens_from_storage
        import base64 as b64
        import hashlib
        import struct
        try:
            # Independent reference implementation of the CryptoJS blob;
            # without it there is nothing to cross-check against.
            from Crypto.Cipher import AES
        except ImportError:
            self.skipTest("pycryptodome missing: no reference AES available")

        def encrypt(plain, phrase=""):
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
            return b64.b64encode(b"Salted__" + salt + ct).decode()

        # The reader stores {"refresh": AES(refresh_token)} under userToken.
        encrypted = encrypt("bosh-refresh-inside")
        import json as json_module
        usertoken = json_module.dumps({"refresh": encrypted, "expireTime": 1})
        self.assertEqual(
            _extract_tokens_from_storage(
                {"webreader.mytolino.com/userToken": usertoken})[0],
            "bosh-refresh-inside")
        # Direct decrypt helper matches.
        self.assertEqual(cryptojs_decrypt(encrypted), "bosh-refresh-inside")
        # userInfos: AES(JSON with hardwareId) under userInfos.
        userinfos = encrypt(json_module.dumps(
            {"userId": "u", "devKey": "d", "hardwareId": "hw-99"}))
        self.assertEqual(
            _extract_tokens_from_storage(
                {"webreader.mytolino.com/userInfos": userinfos})[1],
            "hw-99")

    def test_validate_refresh_candidates_walks_candidate_list(self):
        """First candidate accepted wins; the rotated token is returned."""
        seen = []

        def fake_login(self):
            seen.append(self.refresh)
            if self.refresh in ("spent-old", "spent-newer"):
                raise TolinoAuthError("invalid_grant: Invalid refresh token")
            assert self.refresh == "fresh-now", self.refresh
            self.refresh = "rotated-fresh"

        with patch.object(tolino_module.TolinoClient, "_login", fake_login):
            result = validate_refresh_candidates(
                4, "hw-x", ["spent-old", "spent-newer", "fresh-now"])
        self.assertEqual(("rotated-fresh", "hw-x"), result)
        self.assertEqual(["spent-old", "spent-newer", "fresh-now"], seen)

    def test_validate_refresh_candidates_rejects_all_spent(self):
        def fake_login(self):
            raise TolinoAuthError("invalid_grant: Invalid refresh token")

        with patch.object(tolino_module.TolinoClient, "_login", fake_login):
            self.assertIsNone(validate_refresh_candidates(
                4, "hw-x", ["spent-a", "spent-b"]))

    def test_validate_refresh_candidates_skips_blank_and_bad_input(self):
        with patch.object(tolino_module.TolinoClient, "_login") as fake:
            fake.side_effect = TolinoAuthError("nope")
            self.assertIsNone(validate_refresh_candidates(4, "", []))
            self.assertIsNone(validate_refresh_candidates(4, "", [None, "  "]))
        fake.assert_not_called()

    def test_extract_all_tokens_returns_every_historical_candidate(self):
        from .tolino import _extract_all_tokens_from_storage

        storage = {
            "webreader.mytolino.com/refresh_token": "spent-old",
            "webreader.mytolino.com/userToken": 
                '{"refresh": "spent-newer", "expireTime": 1}',
            "webreader.mytolino.com/hardware_id": "hw-77",
        }
        refreshes, hardwares = _extract_all_tokens_from_storage(storage)
        self.assertEqual(["spent-old", "spent-newer"], refreshes)
        self.assertEqual(["hw-77"], hardwares)

    def test_scrape_browser_tokens_all_candidates_returns_lists(self):
        with patch("calibre_plugin.tolino._find_browser_storage_paths",
                   return_value=["/fake/profile"]), \
                patch("calibre_plugin.tolino.os.path.isdir", return_value=True), \
                patch("calibre_plugin.tolino._collect_storage_under",
                      return_value={
                          "webreader.mytolino.com/refresh_token": "spent-old",
                          "webreader.mytolino.com/hardware_id": "hw-77",
                      }):
            refreshes, hardwares, notes = scrape_browser_tokens(
                diagnose=True, all_candidates=True)
        self.assertEqual(["spent-old"], refreshes)
        self.assertEqual(["hw-77"], hardwares)
        self.assertTrue(any("Kandidat" in n for n in notes), notes)

    def test_recency_sorting_puts_freshest_and_uuid_candidates_first(self):
        from .tolino import (_sort_candidates_by_recency,
                             _sort_hardware_candidates, _RECENCY_BUCKETS,
                             _RECENCY_LIVE, _RECENCY_LDB, _RECENCY_LEGACY)
        try:
            _RECENCY_BUCKETS.clear()
            _RECENCY_BUCKETS["legacy-tok"] = _RECENCY_LEGACY
            _RECENCY_BUCKETS["ldb-tok"] = _RECENCY_LDB
            _RECENCY_BUCKETS["fresh-log-tok"] = _RECENCY_LIVE
            ordered = _sort_candidates_by_recency(
                ["ldb-tok", "legacy-tok", "fresh-log-tok"])
            self.assertEqual(["fresh-log-tok", "ldb-tok", "legacy-tok"],
                             ordered)
            uuid_hw = "da284d4b-6348-43c8-b0ab-17edcddfb25d"
            ordered_hw = _sort_hardware_candidates(
                ["plain-hw", uuid_hw, "0123456789abcdef0123456789abcdef"])
            self.assertEqual(uuid_hw, ordered_hw[0])
        finally:
            _RECENCY_BUCKETS.clear()

    def test_scan_chromium_leveldb_live_hint_reads_only_log_files(self):
        import struct as _struct
        from .tolino import (_scan_chromium_leveldb, _RECENCY_BUCKETS,
                             _RECENCY_LIVE, _RECENCY_LDB)

        def varint(value):
            out = bytearray()
            while True:
                byte = value & 0x7F
                value >>= 7
                if value:
                    out.append(byte | 0x80)
                else:
                    out.append(byte)
                    return bytes(out)

        def writebatch(key, value):
            payload = (_struct.pack("<QI", 1, 1)
                       + b"\x01" + varint(len(key)) + key
                       + varint(len(value)) + value)
            return _struct.pack("<IHB", 0, len(payload), 1) + payload

        key = b"_https://webreader.mytolino.com\x00\x01refresh_token"
        blob = writebatch(key, b"\x01tok-from-log")
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = os.path.join(tmp, "leveldb")
            os.makedirs(db_dir)
            with open(os.path.join(db_dir, "000003.log"), "wb") as fh:
                fh.write(blob)
            with open(os.path.join(db_dir, "000005.ldb"), "wb") as fh:
                fh.write(b"not-a-real-table-but-big-enough" * 40)
            try:
                _RECENCY_BUCKETS.clear()
                live = _scan_chromium_leveldb(
                    db_dir, recency_hint=_RECENCY_LIVE)
                self.assertEqual(
                    {"webreader.mytolino.com\x00\x01refresh_token":
                     "tok-from-log"},
                    live)
                self.assertEqual(
                    _RECENCY_LIVE,
                    _RECENCY_BUCKETS.get("tok-from-log"))
                _RECENCY_BUCKETS.clear()
                plain = _scan_chromium_leveldb(db_dir)
                self.assertEqual(
                    {"webreader.mytolino.com\x00\x01refresh_token":
                     "tok-from-log"},
                    plain)
                self.assertEqual(
                    _RECENCY_LDB, _RECENCY_BUCKETS.get("tok-from-log"))
            finally:
                _RECENCY_BUCKETS.clear()

    def test_scrape_all_candidates_ranks_fresh_log_token_first(self):
        """A .log-harvested token outranks the compacted-history token."""
        import struct as _struct
        from .tolino import (_CHROMIUM_STORAGE_DIRS, _RECENCY_BUCKETS)

        def varint(value):
            out = bytearray()
            while True:
                byte = value & 0x7F
                value >>= 7
                if value:
                    out.append(byte | 0x80)
                else:
                    out.append(byte)
                    return bytes(out)

        def writebatch(records):
            payload = _struct.pack("<QI", 1, len(records))
            for key, value in records:
                payload += (b"\x01" + varint(len(key)) + key
                            + varint(len(value)) + value)
            return _struct.pack("<IHB", 0, len(payload), 1) + payload

        with tempfile.TemporaryDirectory() as tmp:
            db_dir = os.path.join(tmp, "Local Storage", "leveldb")
            os.makedirs(db_dir)
            blob = writebatch([
                (b"_https://webreader.mytolino.com\x00\x01refresh_token",
                 b"\x01fresh-log-token"),
                (b"_https://webreader.mytolino.com\x00\x01hardware_id",
                 b"\x01hw-fresh-log"),
            ])
            with open(os.path.join(db_dir, "000003.log"), "wb") as fh:
                fh.write(blob)

            def fake_collect(path, notes):
                if path == "/fake/profile-old":
                    # Simulate compacted .ldb history found first; the real
                    # live dir is discovered like _collect_storage_under does.
                    _CHROMIUM_STORAGE_DIRS.append(db_dir)
                    return {
                        "webreader.mytolino.com/refresh_token":
                            "spent-ldb-token",
                        "webreader.mytolino.com/hardware_id": "hw-spent",
                    }
                return {}

            try:
                with patch("calibre_plugin.tolino._find_browser_storage_paths",
                           return_value=["/fake/profile-old", db_dir]), \
                        patch("calibre_plugin.tolino.os.path.isdir",
                              return_value=True), \
                        patch("calibre_plugin.tolino._collect_storage_under",
                              side_effect=fake_collect):
                    refreshes, hardwares, notes = scrape_browser_tokens(
                        diagnose=True, all_candidates=True)
            finally:
                _RECENCY_BUCKETS.clear()
                del _CHROMIUM_STORAGE_DIRS[:]
        self.assertIn("fresh-log-token", refreshes)
        self.assertIn("spent-ldb-token", refreshes)
        self.assertEqual("fresh-log-token", refreshes[0])
        self.assertEqual("hw-fresh-log", hardwares[0])
        self.assertTrue(any("Kandidat" in n for n in notes), notes)

    def test_firefox_lsng_rows_are_not_overwritten_by_legacy_shadow(self):
        import sqlite3
        from .tolino import _read_firefox_storage
        with tempfile.TemporaryDirectory() as tmp:
            origin_dir = os.path.join(
                tmp, "https+++webreader.mytolino.com", "ls")
            os.makedirs(origin_dir)
            conn = sqlite3.connect(os.path.join(origin_dir, "data.sqlite"))
            conn.execute(
                "CREATE TABLE data (key TEXT PRIMARY KEY, "
                "utf16_length INTEGER, conversion_type INTEGER, "
                "compression_type INTEGER, last_access_time INTEGER, "
                "value BLOB)")
            conn.execute(
                "INSERT INTO data (key, conversion_type, value) "
                "VALUES (?, 1, ?)", ("refresh_token", b"fresh-lsng-token"))
            conn.commit()
            conn.close()
            # Legacy shadow copy holds an OLDER token for the same key.
            legacy = sqlite3.connect(os.path.join(tmp, "webappsstore.sqlite"))
            legacy.execute(
                "CREATE TABLE webappsstore2 (originAttributes TEXT, "
                "originKey TEXT, scope TEXT, key TEXT, value BLOB)")
            legacy.execute(
                "INSERT INTO webappsstore2 VALUES (?, ?, ?, ?, ?)",
                ("", "https+++webreader.mytolino.com", "",
                 "refresh_token", "legacy-shadow-token"))
            legacy.commit()
            legacy.close()
            storage = _read_firefox_storage(tmp)
        self.assertEqual(
            storage.get("https+++webreader.mytolino.com/refresh_token"),
            "fresh-lsng-token")

class LiveGrabTests(unittest.TestCase):
    """v0.9.17: Der Live-Grab (CDP) ist der Primaerpfad der Browser-Anmeldung.

    Die Disk-Scrape-Validierung replays historische Tokens; Keycloaks
    Wiederverwendungsschutz reagiert darauf mit Session-Widerruf -- genau
    deshalb meldete der Web Reader die Benutzer sofort wieder ab. Der
    Live-Grab liest den AKTUELLEN Token aus dem Seiten-Speicher und
    tauscht ihn genau EINMAL am Token-Endpunkt.
    """

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
        """Flatpak-Browser werden als `flatpak run`-Launcher erkannt.

        Direkte Binary-Pfade aus dem Flatpak-Store (~/.var/app/...) laufen
        ohne die Sandbox-Runtime nicht -- der Launcher muss `flatpak run`
        sein (Nutzerreport: Chromium/Brave unter ~/.var/app).
        """
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
            with _patch("shutil.which", return_value=None):
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
                   return_value=grabbed):
            refresh, hardware = grab_live_refresh(4, "cfg-hw", timeout=1)
        self.assertEqual("rotated-token", refresh)
        self.assertEqual("fake-hw", hardware)
        self.assertEqual([fresh], logins)

    def test_grab_live_refresh_rejects_only_stale_candidates(self):
        """Nur veraltete Kandidaten => Wartephase ohne Nachschub =>
        klare Fehlermeldung statt Replay."""
        from .tolino import grab_live_refresh
        stale = self._jwt(time.time() - 7200, "sess-old")  # 2 h alt
        with patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [stale], "hardware": []}), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   return_value=None), \
             patch("calibre_plugin.tolino._NEW_TOKEN_ATTEMPTS", 2), \
             patch("calibre_plugin.tolino._NEW_TOKEN_INTERVAL", 0):
            with self.assertRaises(TolinoAuthError) as ctx:
                grab_live_refresh(4, "cfg-hw", timeout=1)
        self.assertIn("weder ein frischer Token", str(ctx.exception))

    def test_grab_live_refresh_rejection_recommends_fresh_grab(self):
        """Wird der Live-Token abgelehnt, wird KEIN zweiter Kandidat
        replayt -- der Fehler verweist auf einen erneuten Grab."""
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
        self.assertIn("erneut starten", message)
        # Genau ein Login-Versuch: der zweite Kandidat wird bewusst
        # NICHT mehr probiert (kein Replay-Feuerwerk).
        self.assertEqual([fresh], logins)

    def test_keycloak_assisted_login_uses_live_grab_first(self):
        """browser_login(4) geht ueber den Live-Grab und beruehrt weder
        webbrowser.open noch den Disk-Scrape."""
        from .tolino import _keycloak_assisted_login
        with patch("calibre_plugin.tolino.grab_live_refresh",
                   return_value=("rotated", "hw-live")) as grab, \
             patch("calibre_plugin.tolino.webbrowser.open",
                   side_effect=AssertionError("webbrowser.open called")), \
             patch("calibre_plugin.tolino.scrape_browser_tokens",
                   side_effect=AssertionError("scrape called")):
            result = _keycloak_assisted_login(4, "test_hardware")
        self.assertEqual(("rotated", "hw-live"), result)
        grab.assert_called_once()

    def test_keycloak_assisted_login_falls_back_without_chromium(self):
        """Fehlt ein Chromium-Browser, greift der Disk-Scrape-Flow."""
        from .tolino import _keycloak_assisted_login
        with patch("calibre_plugin.tolino.grab_live_refresh",
                   side_effect=TolinoAuthError(
                       "Kein Chromium-Browser gefunden (Chrome/Chromium/"
                       "Brave/Edge). Die Browser-Anmeldung ben\u00f6tigt "
                       "eines davon.")), \
             patch("calibre_plugin.tolino._disk_assisted_login",
                   return_value=("disk-rotated", "disk-hw")) as disk:
            result = _keycloak_assisted_login(4, "test_hardware")
        self.assertEqual(("disk-rotated", "disk-hw"), result)
        disk.assert_called_once()

    def test_grab_live_refresh_never_retries_a_rejected_candidate(self):
        """Ein am Endpunkt abgelehnter Kandidat wird nie wieder probiert
        (Keycloak-Reuse-Schutz) -- auch nicht, wenn der Seiten-Speicher
        dieselbe Kopie erneut liefert. Statt einer erzwungenen Rotation
        wartet das Plugin auf eine NEU geschriebene Kopie; kommt keine,
        endet es mit einer klaren Fehlermeldung."""
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
        self.assertIn("Letzter Fehler", str(ctx.exception))

    def test_ws_recv_json_parses_rfc6455_header_correctly(self):
        """Der minimale WS-Client liest den Länge+MASK-Header korrekt.

        Regression zu 0.9.18: das zweite Header-Byte (Länge) wurde
        verworfen und ein drittes Byte als Länge gelesen -- jede CDP-
        Antwort zerfiel an falschen Offsets und der Live-Grab fand
        "nichts", obwohl das Anmeldefenster angemeldet war.
        """
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
        """Tokens bis 30 Minuten Alter ueberleben den Live-Cutoff.

        Der Nutzer braucht fuer die Anmeldung oft laenger als 2 Minuten;
        Keycloak-Refresh-Tokens leben ~1 Stunde, der 2-Minuten-Cutoff von
        0.9.18 warf daher den gueltigen Live-Token weg.
        """
        from .tolino import _filter_live_candidates
        now = time.time()
        ten_minutes_old = self._jwt(now - 600, "sess-1")
        kept = _filter_live_candidates([ten_minutes_old])
        self.assertEqual([ten_minutes_old], kept)
        # Ausdruecklich mit dem Default aufgerufen bleibt es bei 1800s:
        self.assertIn("1800",
                      _filter_live_candidates.__defaults__[0].__str__())

    def test_try_live_grab_first_returns_none_without_window(self):
        """Ohne laufendes Grabber-Fenster -> None (Disk-Scrape erlaubt)."""
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
             patch("calibre_plugin.tolino.TolinoClient", FakeClient):
            result = try_live_grab_first(4, "hw")
        self.assertEqual(("rotated-token", "hw-from-grab"), result)

    def test_try_live_grab_first_raises_when_window_dead_yields_nothing(self):
        """Fenster lebt, liefert aber nichts -> Fehler statt Disk-Scrape.

        Wuerde der Aufrufer hier still scrapen, replayte er alte Storage-
        Kopien gegen die im Fenster offene Sitzung -- der Reuse-Schutz-
        fall, den der Live-Weg gerade vermeiden soll.
        """
        from .tolino import try_live_grab_first
        with patch("calibre_plugin.cdp.devtools_port_alive",
                   return_value=True), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   return_value={"refresh": [], "hardware": [], "idb": []}), \
             patch("calibre_plugin.cdp.describe_grab_state",
                   return_value="0 Refresh-Kandidat(en)"):
            with self.assertRaises(TolinoAuthError) as ctx:
                try_live_grab_first(4, "hw")
        self.assertIn("30 Minuten", str(ctx.exception))

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
        self.assertIn("kein Web-Reader-Tab", str(ctx.exception))

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
        einer Lauscherphase -- sofortige, klare Fehlermeldung."""
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
        self.assertIn("Letzter Fehler", str(ctx.exception))
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
        """200er Browser-Tausch schlaegt den Plugin-POST -- kein WAF-403."""
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
        Kopie des Readers -- und die Hardware-ID der aktuellen Sitzung
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
        verwendet -- nicht die alte konfigurierte (Nutzerfall: Plugin
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
        message = str(ctx.exception)
        self.assertIn("weder ein frischer Token", message)
        self.assertIn("Letzter Fehler", message)

    def test_keycloak_assisted_login_propagates_grab_errors(self):
        """Andere Grab-Fehler (Timeout, abgelehnt) werden weitergereicht
        -- kein stiller Wechsel in den Replay-gefaehrdeten Disk-Pfad."""
        from .tolino import _keycloak_assisted_login
        with patch("calibre_plugin.tolino.grab_live_refresh",
                   side_effect=TolinoAuthError(
                       "Timeout: Im Anmeldefenster wurde keine Web-"
                       "Reader-Anmeldung erkannt.")), \
             patch("calibre_plugin.tolino._disk_assisted_login",
                   side_effect=AssertionError("fallback called")):
            with self.assertRaises(TolinoAuthError) as ctx:
                _keycloak_assisted_login(4, "test_hardware")
        self.assertIn("Timeout", str(ctx.exception))

    def test_collect_grab_waits_while_window_is_on_partner_login_page(self):
        """Feldbefund 0.9.27: Waehrend der Anmeldung verlaesst der Tab
        mytolino.com zugunsten der Keycloak-Seite des Partners (andere
        Domain) -- das darf NIEMALS als "Fenster geschlossen" gemeldet
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
        nicht abgeschlossen), gibt es einen Timeout-Hinweis -- keine
        geschlossene-Fenster-Meldung."""
        from . import cdp as cdp_module
        with self.assertRaises(TolinoAuthError) as ctx:
            cdp_module.collect_grab(4, 9223, timeout=0)
        message = str(ctx.exception)
        self.assertIn("Timeout", message)
        self.assertIn("nicht abgeschlossen", message)
        self.assertNotIn("wurde geschlossen", message)

    def _ws_run_server(self, thread_state, handler, connections=1):
        """Minimaler In-Process-WS-Server (Schema der 0.9.19-Tests):
        bindet, meldet den Port via thread_state["ready"] und beantwortet
        bis zu `connections` aufeinanderfolgende Verbindungen per
        `handler(message, send_raw)` -- jeder CDP-Aufruf oeffnet eine
        eigene Verbindung."""
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
        JSON.stringify(out) -- einem STRING. _evaluate_in_target muss
        diese Antwort parsten, sonst bleibt jeder Grab leer ("kein
        frischer Token"); frueher antworteten nur die Test-Server mit
        dict, wodurch die Regression unsichtbar blieb."""
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
    def _load_ui_module(get_icons_result):
        """Import ui.py with Calibre and Qt imports stubbed."""
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
        from calibre_plugin import ui as ui_module
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
    """Tests for pytolino-derived client features: device list, download,
    sync-data collections/read-state, refresh expiry tracking."""

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
                         authenticated=True, content_type=None, _retry=True):
            calls.append({"url": url, "method": method, "data": data})
            return queue.pop(0)

        client._request = fake_request
        return client, calls

    def test_add_to_collection_sends_tag_add_patch(self):
        client, calls = self._patching_client([{"revision": 7}])
        revision = client.add_to_collection("book-1", "SciFi")
        self.assertEqual(7, revision)
        self.assertEqual("PATCH", calls[0]["method"])
        self.assertIn("sync-data", calls[0]["url"])
        body = calls[0]["data"]
        patch = body["patches"][0]
        self.assertEqual("add", patch["op"])
        self.assertEqual("/publications/book-1/tags", patch["path"])
        self.assertEqual("SciFi", patch["value"]["name"])
        self.assertEqual("collection", patch["value"]["category"])

    def test_remove_from_collection_requires_existing_tag(self):
        from .tolino import TolinoApiError
        existing = {
            "op": "add",
            "path": "/publications/book-1/tags",
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

        client2, _ = self._patching_client([
            {"revision": 7, "patches": []}])
        with self.assertRaises(TolinoApiError):
            client2.remove_from_collection("book-1", "SciFi")

    def test_mark_read_uses_system_tag(self):
        client, calls = self._patching_client([{"revision": 9}])
        revision = client.mark_read("book-2", finished=True)
        self.assertEqual(9, revision)
        patch = calls[0]["data"]["patches"][0]
        self.assertEqual("system", patch["value"]["category"])
        self.assertEqual("collection_finished_readings_name",
                         patch["value"]["name"])

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

    def test_403_bot_check_without_curl_cffi_appends_install_hint(self):
        from urllib.error import HTTPError
        import calibre_plugin.tolino as tolino_module

        body = b"<!DOCTYPE html><title>Zugriff geblockt</title>"
        error = HTTPError("https://example.invalid/token", 403, "Forbidden", {}, None)
        error.read = lambda: body
        client = TolinoClient(4, "", "old-refresh")
        with patch.object(tolino_module, "_impersonate_session", return_value=None), \
                patch.object(tolino_module, "_curl_binary", return_value=None), \
                patch.object(tolino_module, "urlopen", side_effect=error):
            with self.assertRaisesRegex(TolinoAuthError, "curl_cffi"):
                client.login()

    def test_403_bot_check_with_curl_cffi_has_no_install_hint(self):
        from urllib.error import HTTPError
        import calibre_plugin.tolino as tolino_module

        body = b"<!DOCTYPE html><title>Zugriff geblockt</title>"
        error = HTTPError("https://example.invalid/token", 403, "Forbidden", {}, None)
        error.read = lambda: body
        client = TolinoClient(4, "", "old-refresh")
        with patch.object(tolino_module, "_impersonate_session",
                          return_value=object()), \
                patch.object(tolino_module, "_curl_binary", return_value=None), \
                patch.object(tolino_module, "urlopen", side_effect=error):
            with self.assertRaisesRegex(TolinoAuthError, "Zugriff geblockt"):
                client.login()
        self.assertNotIn("curl_cffi", str(getattr(client, "last_error_text", "")))

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

    def test_http_post_via_curl_raises_without_binary(self):
        import calibre_plugin.tolino as tolino_module

        with patch.object(tolino_module, "_curl_binary", return_value=None):
            with self.assertRaises(RuntimeError):
                tolino_module._http_post_via_curl(
                    "https://example.test/token", "grant_type=x", {}, 5)


class BootstrapperTests(unittest.TestCase):
    """The curl_cffi one-click installer verifies checksums and projects a
    working import root into the plugin directory."""

    def _fake_plugin_dir(self):
        return tempfile.mkdtemp(prefix="tolino-bootstrap-test-")

    def test_wheel_tables_are_consistent(self):
        from . import bootstrapper

        # The curl_cffi wheel is abi3 (works on every CPython >= its tag).
        self.assertEqual(5, len(bootstrapper.CURL_CFFI_WHEELS))
        for platform, row in bootstrapper.CURL_CFFI_WHEELS.items():
            filename, sha256, url_path = row
            self.assertIn("cp310-abi3", filename, platform)
            self.assertEqual(64, len(sha256), platform)
            self.assertTrue(url_path.endswith(filename), platform)
        # cffi is version-specific: every supported CPython tag has a wheel
        # for every platform, each with a valid checksum.
        expected_keys = {("cp%d%d" % (3, minor), platform)
                         for minor in range(10, 15)
                         for platform in bootstrapper.CURL_CFFI_WHEELS}
        self.assertEqual(expected_keys, set(bootstrapper.CFFI_WHEELS))
        for (tag, platform), row in bootstrapper.CFFI_WHEELS.items():
            filename, sha256, url_path = row
            self.assertTrue(filename.startswith("cffi-2.1.1-%s-" % tag),
                            (tag, platform))
            self.assertEqual(64, len(sha256), (tag, platform))
            self.assertTrue(url_path.endswith(filename), (tag, platform))
        for filename, sha256, url_path in bootstrapper.WHEELS_ANY:
            self.assertTrue(filename.endswith(".whl"))
            self.assertEqual(64, len(sha256))
            self.assertNotIn("PLACEHOLDER", sha256)
            self.assertTrue(url_path.endswith(filename))
        # Pure-python deps appear exactly once (they are platform-neutral).
        seen = [row[0] for row in bootstrapper.WHEELS_ANY]
        self.assertEqual(1, sum(1 for name in seen if name.startswith("pycparser")))
        self.assertEqual(1, sum(1 for name in seen if name.startswith("certifi")))

    def test_cffi_tag_follows_running_interpreter(self):
        import sys
        from . import bootstrapper

        with patch.object(sys, "version_info", (3, 12, 3, "final", 0)):
            self.assertEqual("cp312", bootstrapper._cffi_tag())
        with patch.object(sys, "version_info", (3, 10, 0, "final", 0)):
            self.assertEqual("cp310", bootstrapper._cffi_tag())
        with patch.object(sys, "version_info", (3, 9, 18, "final", 0)):
            self.assertIsNone(bootstrapper._cffi_tag())

    def test_install_rejects_unsupported_python_with_manual_hint(self):
        import sys
        from . import bootstrapper

        with patch.object(sys, "version_info", (3, 9, 18, "final", 0)):
            with self.assertRaisesRegex(bootstrapper.BootstrapError,
                                        "Python 3.10-3.14"):
                bootstrapper.install(
                    plugin_dir=self._fake_plugin_dir(), progress=lambda t: None)

    def test_install_picks_cffi_wheel_matching_interpreter(self):
        import sys
        from . import bootstrapper

        tmp = self._fake_plugin_dir()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            tmp, ignore_errors=True))
        picked = []

        def fake_download(url, expected_sha256):
            filename = url.rsplit("/", 1)[-1]
            picked.append(filename)
            buf = __import__("io").BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                top = filename.split("-")[0]
                zf.writestr("%s/__init__.py" % top, "# fake package\n")
            return buf.getvalue()

        with patch.object(sys, "version_info", (3, 12, 3, "final", 0)), \
                patch.object(bootstrapper, "_download", fake_download):
            bootstrapper.install(plugin_dir=tmp, progress=lambda t: None)

        self.assertEqual(4, len(picked))
        self.assertTrue(
            any(name.startswith("cffi-2.1.1-cp312-") for name in picked),
            picked)
        self.assertTrue(
            any(name.startswith("curl_cffi-0.16.3-cp310-abi3-")
                for name in picked), picked)

    def test_download_rejects_checksum_mismatch(self):
        from . import bootstrapper

        blob = b"definitely-not-a-wheel"
        wrong = "0" * 64
        with patch.object(bootstrapper, "urlopen") as fake:
            fake.return_value.__enter__ = lambda s: type(
                "R", (), {"read": lambda self: blob})()
            fake.return_value.__exit__ = lambda s, *a: False
            with self.assertRaisesRegex(bootstrapper.BootstrapError,
                                        "SHA-256 mismatch"):
                bootstrapper._download("https://example.invalid/x.whl", wrong)

    def test_install_extracts_and_verifies_fake_wheels(self):
        from . import bootstrapper

        tmp = self._fake_plugin_dir()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            tmp, ignore_errors=True))

        def fake_wheel_blob(name):
            buf = __import__("io").BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                top = name.split("-")[0]
                zf.writestr("%s/__init__.py" % top, "# fake package\n")
            return buf.getvalue()

        platform = bootstrapper._platform_key()
        entries = (list(bootstrapper.WHEELS_ANY)
                   + [bootstrapper.CURL_CFFI_WHEELS[platform],
                      bootstrapper.CFFI_WHEELS[(bootstrapper._cffi_tag(),
                                                platform)]])

        def fake_download(url, expected_sha256):
            filename = url.rsplit("/", 1)[-1]
            return fake_wheel_blob(filename)

        with patch.object(bootstrapper, "_download", fake_download):
            installed = bootstrapper.install(plugin_dir=tmp, progress=lambda t: None)
        self.assertEqual(len(entries), len(installed))
        root = os.path.join(tmp, "curl_cffi-libs")
        for name in ("curl_cffi", "cffi", "pycparser", "certifi"):
            self.assertTrue(os.path.isdir(os.path.join(root, name)), name)

    def test_import_from_plugin_dir_loads_fresh_projected_copy(self):
        """A healthy projected copy is imported (shadowing any other
        curl_cffi); a broken one yields None instead of raising."""
        import sys
        import importlib
        from . import bootstrapper

        tmp = self._fake_plugin_dir()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            tmp, ignore_errors=True))
        root = os.path.join(tmp, "curl_cffi-libs")

        def cleanup():
            if root in sys.path:
                sys.path.remove(root)
            for name in [m for m in list(sys.modules)
                         if m.split(".")[0] in ("curl_cffi", "cffi",
                                                "_cffi_backend", "pycparser",
                                                "certifi")]:
                sys.modules.pop(name, None)
        self.addCleanup(cleanup)

        # Broken projected install, shadowing everything else on sys.path.
        os.makedirs(os.path.join(root, "curl_cffi"))
        with open(os.path.join(root, "curl_cffi", "__init__.py"), "w") as fh:
            fh.write("raise ImportError('broken install')\n")
        sys.path.insert(0, root)
        with patch.object(bootstrapper, "calibre_plugin_dir",
                          return_value=tmp):
            self.assertIsNone(bootstrapper.import_from_plugin_dir())

        # Repair the projected copy in place (like a fresh one-click install).
        os.makedirs(os.path.join(root, "curl_cffi", "requests"))
        with open(os.path.join(root, "curl_cffi", "__init__.py"), "w") as fh:
            fh.write("")
        with open(os.path.join(root, "curl_cffi", "requests", "__init__.py"),
                  "w") as fh:
            fh.write("class Session:\n"
                     "    def __init__(self, impersonate=None):\n"
                     "        self.impersonate = impersonate\n")
        future = __import__("time").time() + 5
        for base, dirs, files in os.walk(root):
            for name in dirs + files:
                os.utime(os.path.join(base, name), (future, future))
        importlib.invalidate_caches()
        with patch.object(bootstrapper, "calibre_plugin_dir",
                          return_value=tmp):
            session_factory = bootstrapper.import_from_plugin_dir()
        self.assertIsNotNone(session_factory)
        self.assertEqual("chrome",
                         session_factory(impersonate="chrome").impersonate)
        self.assertIn(root, sys.path)

    def test_setup_status_reports_missing_install(self):
        from . import bootstrapper

        tmp = self._fake_plugin_dir()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            tmp, ignore_errors=True))
        with patch.object(bootstrapper, "calibre_plugin_dir", return_value=tmp), \
                patch.object(bootstrapper, "is_available", return_value=False):
            installed, importable, plugin_dir = bootstrapper.setup_status()
        self.assertFalse(installed)
        self.assertFalse(importable)
        self.assertEqual(tmp, plugin_dir)


if __name__ == "__main__":
    unittest.main()
