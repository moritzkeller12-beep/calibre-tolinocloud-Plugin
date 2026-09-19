import unittest
import ast
import os
import tempfile
import time
import zipfile
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
from .tolino import (PARTNERS, TolinoAuthError, callback_redirect_uri,
                     TolinoClient, extract_login_tokens, hardware_id,
                     normalize_refresh_token, sanitize_error,
                     validate_callback, scrape_browser_tokens, browser_login)
try:
    from . import weblogin
except ImportError:
    import weblogin


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
        for partner_id in (3, 4, 6, 8, 13, 23, 30):
            self.assertIn(partner_id, PARTNERS)
        self.assertRegex(hardware_id(), r"^[123x]..A-00BCD-EFGHI-JKLMN-OPQRh$")

    def test_partner_8_refresh_configuration_matches_reference(self):
        self.assertEqual("webreader", PARTNERS[8]["client_id"])
        self.assertEqual("SCOPE_BOSH", PARTNERS[8]["scope"])
        self.assertEqual("https://www.orellfuessli.ch/auth/oauth2/token",
                         PARTNERS[8]["token_url"])
        self.assertEqual(
            "https://www.orellfuessli.ch/auth/oauth2/autologin",
            PARTNERS[8]["auth_url"])
        self.assertEqual("https://webreader.mytolino.com/library/",
                         PARTNERS[8]["reader_url"])
        self.assertEqual("37", PARTNERS[8]["x_buchde.mandant_id"])
        self.assertEqual("17", PARTNERS[8]["x_buchde.skin_id"])
        self.assertEqual("TOLINO_WEBREADER", PARTNERS[8]["client_type"])
        self.assertEqual("5.2.0", PARTNERS[8]["client_version"])
        self.assertEqual("https://webreader.mytolino.com",
                         PARTNERS[8]["token_headers"]["Origin"])
        self.assertEqual("https://webreader.mytolino.com/",
                         PARTNERS[8]["token_headers"]["Referer"])

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

        with patch("calibre_plugin.tolino.urlopen", request):
            client = TolinoClient(8, "3xxA-00BCD-EFGHI-JKLMN-OPQRh",
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

        with patch("calibre_plugin.tolino.urlopen", request):
            client = TolinoClient(8, "3xxA-00BCD-EFGHI-JKLMN-OPQRh",
                                  " refresh-token ")
            client.login()
        self.assertEqual(PARTNERS[8]["token_url"], captured["url"])
        self.assertEqual(
            "client_id=webreader&grant_type=refresh_token&"
            "refresh_token=refresh-token",
            captured["body"])
        self.assertEqual("application/x-www-form-urlencoded",
                         captured["content_type"])
        headers = {key.casefold(): value for key, value in captured["headers"].items()}
        self.assertEqual("https://webreader.mytolino.com", headers["origin"])
        self.assertEqual("https://webreader.mytolino.com/", headers["referer"])
        self.assertNotIn("scope=", captured["body"])
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

        with patch("calibre_plugin.tolino.urlopen", return_value=Response()):
            client = TolinoClient(8, "hardware", "old-refresh",
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
        with patch("calibre_plugin.tolino.urlopen", side_effect=error) as request:
            with self.assertRaisesRegex(TolinoAuthError, "Web Reader again"):
                TolinoClient(8, "", "old-refresh").login()
        self.assertEqual(1, request.call_count)

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

        with patch("calibre_plugin.tolino.urlopen", request):
            TolinoClient(3, "3xxA-00BCD-EFGHI-JKLMN-OPQRh",
                         "refresh-token").login()
        self.assertEqual(
            "client_id=webreader&grant_type=refresh_token&"
            "refresh_token=refresh-token&scope=SCOPE_BOSH",
            captured["body"])

    def test_auth_diagnostics_redacts_token_content(self):
        client = TolinoClient(8, "", '"secret-refresh"')
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
        client = TolinoClient(8, "", token)
        with patch("calibre_plugin.tolino.urlopen", side_effect=error):
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
            self.assertEqual([{"name": "default", "partner_id": 8,
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
                "weblogin.py",
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

    def test_browser_login_works_for_all_partners_with_auth_url(self):
        for partner_id in (3, 4, 8, 13, 23, 30):
            partner = PARTNERS[partner_id]
            if partner.get("auth_url") and partner.get("token_url"):
                try:
                    browser_login(partner_id, "test_hardware")
                except TolinoAuthError as exc:
                    error_msg = str(exc)
                    self.assertNotIn("local browser callback is not supported", error_msg)
                    self.assertNotIn("only registers its Web Reader redirect URI", error_msg)

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
        from .tolino import _read_firefox_storage, _read_firefox_data_sqlite
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

        def encrypt(plain, phrase=""):
            from Crypto.Cipher import AES
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

    def test_extract_login_tokens_reads_plain_and_json_values(self):
        storage = {
            "refresh_token": "plain-refresh",
            "t_auth_token": "ignored-second",
            "device_id": "hw-123",
            "unrelated": "value",
        }
        self.assertEqual(
            ("plain-refresh", "hw-123"),
            extract_login_tokens(storage),
        )
        self.assertEqual(("json-refresh", None), extract_login_tokens({
            "oauth": '{"access_token":"a","refresh_token":"json-refresh"}',
        }))
        self.assertEqual((None, None), extract_login_tokens({}))
        self.assertEqual((None, None), extract_login_tokens(None))
        self.assertEqual((None, None), extract_login_tokens({"key": 42}))

    def test_extract_login_tokens_matches_keycloak_style_keys(self):
        refresh, hardware = extract_login_tokens({
            "https://webreader.mytolino.com/refreshToken": " case-refresh ",
            "hardwareId": "hw-9",
        })
        self.assertEqual("case-refresh", refresh)
        self.assertEqual("hw-9", hardware)

    def test_embedded_login_reports_unavailable_webengine(self):
        with patch.object(weblogin, "QWebEngineView", None), \
                patch.object(weblogin, "QWebEngineProfile", None):
            self.assertFalse(weblogin.embedded_login_available())
            with self.assertRaisesRegex(TolinoAuthError, "QtWebEngine"):
                weblogin.run_embedded_login(8, "hw")

    def test_embedded_start_url_builds_oauth_and_reader_fallback(self):
        dialog = object.__new__(weblogin.EmbeddedLoginDialog)
        dialog.partner = dict(PARTNERS[8])
        url = dialog._start_url()
        self.assertIn("auth/oauth2/autologin", url)
        self.assertIn("client_id=webreader", url)
        self.assertIn("x_buchde.mandant_id=37", url)

        dialog.partner = {"client_id": "c", "scope": "s"}
        url = dialog._start_url()
        self.assertTrue(url.startswith("https://webreader.mytolino.com"))

    def test_embedded_storage_ready_accepts_token_and_ignores_garbage(self):
        import json as json_module
        dialog = object.__new__(weblogin.EmbeddedLoginDialog)
        dialog.completed = False
        dialog.hardware_id_value = "configured-hw"
        dialog.refresh_token = None
        dialog.hardware_id = None
        dialog.status = type("Label", (), {"setText": staticmethod(lambda *_: None)})()
        stopped = []

        class Timer:
            def stop(self):
                stopped.append(True)

        dialog._timer = Timer()
        dialog._storage_ready(json_module.dumps({
            "refresh_token": "embedded-refresh", "device_id": "embedded-hw",
        }))
        self.assertTrue(dialog.completed)
        self.assertEqual("embedded-refresh", dialog.refresh_token)
        self.assertEqual("embedded-hw", dialog.hardware_id)

        dialog.completed = False
        dialog._storage_ready("not-json")
        self.assertFalse(dialog.completed)

    def test_oauth_code_exchange_from_redirect_url(self):
        dialog = object.__new__(weblogin.EmbeddedLoginDialog)
        dialog.completed = False
        dialog.partner = dict(PARTNERS[8])
        dialog.partner_id = 8
        dialog.hardware_id_value = "hw-99"
        dialog.refresh_token = None
        dialog.hardware_id = None
        dialog._exchanged_code = None
        dialog._redirect_hint = "https://webreader.mytolino.com/callback"
        dialog.status = type("Label", (), {"setText": staticmethod(lambda *_: None)})()
        dialog._timer = type("Timer", (), {
            "stop": staticmethod(lambda: None),
            "start": staticmethod(lambda: None)})()

        class FakeUrl:
            def query(self):
                return "code=abc123&state=xyz"

        exchanged = {}

        def fake_request(self, url, method="GET", data=None, form=False,
                         authenticated=True, content_type=None, _retry=True):
            exchanged["url"] = url
            exchanged["data"] = data
            return {"access_token": "a", "refresh_token": "code-refresh",
                    "hardware_id": "hw-from-code"}

        with patch.object(weblogin.TolinoClient, "_request", fake_request):
            handled = dialog._maybe_exchange_oauth_code(FakeUrl())
        self.assertTrue(handled)
        self.assertTrue(dialog.completed)
        self.assertEqual("code-refresh", dialog.refresh_token)
        self.assertEqual("hw-from-code", dialog.hardware_id)
        self.assertEqual(PARTNERS[8]["token_url"], exchanged["url"])
        self.assertEqual("abc123", exchanged["data"]["code"])
        self.assertEqual("authorization_code", exchanged["data"]["grant_type"])
        self.assertEqual("https://webreader.mytolino.com/callback",
                         exchanged["data"]["redirect_uri"])

    def test_oauth_code_exchange_without_code_is_noop(self):
        dialog = object.__new__(weblogin.EmbeddedLoginDialog)
        dialog.completed = False
        dialog.partner = dict(PARTNERS[8])
        dialog._exchanged_code = None

        class FakeUrl:
            query = ""  # attribute, not callable -> guarded

        self.assertFalse(dialog._maybe_exchange_oauth_code(FakeUrl()))
        self.assertFalse(dialog.completed)

    def test_record_redirect_hint_captures_code_url(self):
        class FakeUrl:
            def __init__(self, q):
                self._q = q

            def query(self):
                return self._q

            def toString(self, *_flags):
                return "https://webreader.mytolino.com/cb"

        dialog = object.__new__(weblogin.EmbeddedLoginDialog)
        dialog._redirect_hint = None
        dialog._record_redirect_hint(FakeUrl("code=k1&session_state=s"))
        self.assertEqual("https://webreader.mytolino.com/cb",
                         dialog._redirect_hint)
        dialog._redirect_hint = None
        dialog._record_redirect_hint(FakeUrl(""))
        self.assertIsNone(dialog._redirect_hint)

    def test_resolve_enum_supports_qt6_scoped_and_qt5_flat_names(self):
        class FakeQWebEngineProfile:  # Qt6/PyQt6 shape, as in Calibre 7.x
            class PersistentCookiesPolicy:
                ForcePersistentCookies = "qt6-scoped"

        class FakeQt5Profile:  # legacy flat shape
            ForcePersistentCookies = "qt5-flat"

        self.assertEqual(
            "qt6-scoped",
            weblogin._resolve_enum(
                FakeQWebEngineProfile, *weblogin._FORCE_PERSISTENT_COOKIES),
        )
        self.assertEqual(
            "qt5-flat",
            weblogin._resolve_enum(
                FakeQt5Profile, *weblogin._FORCE_PERSISTENT_COOKIES),
        )
        self.assertIsNone(
            weblogin._resolve_enum(FakeQWebEngineProfile, "NoSuch.Policy"))
        self.assertIsNone(weblogin._resolve_enum(None, "Close"))

    def test_clean_user_agent_strips_qt_token_and_keeps_chrome(self):
        qt6_ua = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, "
                  "like Gecko) QtWebEngine/5.15.2 Chrome/87.0.4280.144 "
                  "Safari/537.36")
        cleaned = weblogin._clean_user_agent(qt6_ua)
        self.assertNotIn("QtWebEngine", cleaned)
        self.assertIn("Chrome/87.0.4280.144", cleaned)
        self.assertIn("Mozilla/5.0 (X11; Linux x86_64)", cleaned)
        self.assertNotIn("  ", cleaned)

        # Older builds may embed the token with different casing/spacing.
        self.assertNotIn(
            "qtwebengine", weblogin._clean_user_agent(
                "Mozilla/5.0 qtwebengine/6.7.0 Chrome/118").casefold())

        # Empty/None falls back to a sensible Chrome UA.
        self.assertIn("Chrome/", weblogin._clean_user_agent(""))
        self.assertIn("Chrome/", weblogin._clean_user_agent(None))

    def test_stealth_script_covers_key_bot_signals(self):
        script = weblogin._stealth_js("118", "Linux")
        self.assertIn("webdriver", script)
        self.assertIn("window.chrome", script)
        self.assertIn("plugins", script)
        self.assertIn("languages", script)
        self.assertIn("WebGLRenderingContext", script)
        self.assertIn("37445", script)  # UNMASKED_VENDOR_WEBGL spoof
        # Every spoof block must be individually guarded.
        self.assertGreaterEqual(script.count("catch (err) {}"), 6)
        self.assertIn('"118"', script)
        self.assertIn('"Linux"', script)

    def test_client_hint_headers_match_cleaned_ua(self):
        ua = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, "
              "like Gecko) Chrome/118.0.0.0 Safari/537.36")
        hints = weblogin._client_hint_headers(ua)
        self.assertEqual(
            '"Not A(Brand";v="99", "Chromium";v="118", '
            '"Google Chrome";v="118"',
            hints["Sec-CH-UA"])
        self.assertEqual("?0", hints["Sec-CH-UA-Mobile"])
        self.assertEqual('"Linux"', hints["Sec-CH-UA-Platform"])

        windows_hints = weblogin._client_hint_headers(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0")
        self.assertEqual('"Windows"', windows_hints["Sec-CH-UA-Platform"])

        # Non-Chrome user agents get no hints rather than wrong ones.
        self.assertEqual({}, weblogin._client_hint_headers("Firefox/128.0"))
        self.assertEqual({}, weblogin._client_hint_headers(None))

    def test_external_fallback_adopts_and_requires_tokens(self):
        dialog = object.__new__(weblogin.EmbeddedLoginDialog)
        dialog.completed = False
        dialog.refresh_token = None
        dialog.hardware_id = None
        dialog.hardware_id_value = "hw-fallback"
        dialog.status = type("Label", (), {"setText": staticmethod(lambda *_: None)})()
        stopped = []

        class Timer:
            def stop(self):
                stopped.append(True)

        dialog._timer = Timer()

        self.assertFalse(dialog._apply_external_tokens(None, None))
        self.assertFalse(dialog._apply_external_tokens("  ", "hw"))
        self.assertFalse(dialog.completed)

        self.assertTrue(dialog._apply_external_tokens("external-refresh", "hw-x"))
        self.assertTrue(dialog.completed)
        self.assertEqual("external-refresh", dialog.refresh_token)
        self.assertEqual("hw-x", dialog.hardware_id)
        self.assertEqual([True], stopped)

        # Missing hardware falls back to the configured value.
        dialog.completed = False
        dialog.refresh_token = None
        dialog._apply_external_tokens("r2", None)
        self.assertEqual("hw-fallback", dialog.hardware_id)

    def test_external_login_page_targets_partner_auth_url(self):
        dialog = object.__new__(weblogin.EmbeddedLoginDialog)
        dialog.partner = dict(PARTNERS[8])
        self.assertIn("auth/oauth2/autologin", dialog._start_url())

    def test_stealth_script_registration_is_guarded_without_qt(self):
        dialog = object.__new__(weblogin.EmbeddedLoginDialog)
        dialog.profile = object()  # no scripts() attribute in the stub
        # Must not raise even though QWebEngineScript exists in real Qt builds
        # but the profile stub lacks the scripts collection.
        try:
            dialog._install_stealth_script("Chrome/118")
        except AttributeError:
            pass  # tolerated on the stub; real builds have profile.scripts()

    def test_quiet_page_class_exists_when_webengine_available(self):
        # With the real Calibre/Qt modules unavailable, the quiet page stub is
        # None; the guard must be importable without Qt either way.
        if weblogin.QWebEnginePage is None:
            self.assertIsNone(weblogin._QuietWebEnginePage)
        else:
            self.assertTrue(issubclass(weblogin._QuietWebEnginePage,
                                       weblogin.QWebEnginePage))

    def test_teardown_is_idempotent_and_tolerates_gone_objects(self):
        dialog = object.__new__(weblogin.EmbeddedLoginDialog)
        dialog._torn_down = False

        class G:
            def __init__(self):
                self.calls = []

            def stop(self):
                self.calls.append("stop")

            def setPage(self, value):
                self.calls.append(("setPage", value))

            def deleteLater(self):
                self.calls.append("deleteLater")

        class RuntimeErrorView(G):
            def setPage(self, value):
                raise RuntimeError("underlying C/C++ object has been deleted")

        dialog.view = RuntimeErrorView()
        dialog.page = G()
        dialog.profile = G()
        dialog._teardown_webengine()
        self.assertTrue(dialog._torn_down)
        # The view is only stopped and detached (setPage raised, swallowed);
        # Qt destroys the child widget with the dialog itself.
        self.assertEqual(["stop"], dialog.view.calls)
        self.assertEqual(["deleteLater"], dialog.page.calls)
        self.assertEqual(["deleteLater"], dialog.profile.calls)

        # Second run must do nothing.
        dialog.view = G()
        dialog.page = G()
        dialog.profile = G()
        dialog._teardown_webengine()
        self.assertEqual([], dialog.view.calls)
        self.assertEqual([], dialog.page.calls)
        self.assertEqual([], dialog.profile.calls)

    def test_teardown_handles_missing_attributes(self):
        dialog = object.__new__(weblogin.EmbeddedLoginDialog)
        dialog._torn_down = False
        dialog._teardown_webengine()  # no view/page/profile attributes at all
        self.assertTrue(dialog._torn_down)


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
                     "QMessageBox", "QProgressBar", "QPushButton", "QThread",
                     "QVBoxLayout", "QHBoxLayout", "QTableWidget",
                     "QTableWidgetItem", "QTextEdit", "QObject",
                     "QInputDialog"):
            setattr(qt_core, name, type(name, (), {}))
        qt_core.pyqtSignal = lambda *a, **k: None
        qt_core.Signal = lambda *a, **k: None
        calibre = types.ModuleType("calibre")
        calibre_gui2 = types.ModuleType("calibre.gui2")
        calibre_gui2.error_dialog = None
        calibre_gui2.info_dialog = None
        calibre_gui2.get_icons = lambda name: get_icons_result
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

if __name__ == "__main__":
    unittest.main()
