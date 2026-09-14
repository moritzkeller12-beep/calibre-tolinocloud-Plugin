import unittest
import ast
import os
import tempfile
import zipfile
from pathlib import Path

from . import config
from .sync import (compare_inventory, cover_bytes, fingerprint, iter_book_ids,
                   normalize_formats, plan_sync, safe_format_path,
                   selected_book_ids, selected_table_rows, sync_summary,
                   unpack_plan_result, unpack_upload_record)
from .tolino import (PARTNERS, TolinoAuthError, callback_redirect_uri,
                     hardware_id, validate_callback)


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

    def test_normalize_formats_handles_all_calibre_shapes(self):
        self.assertEqual(["EPUB", "PDF"], normalize_formats(("EPUB", "PDF")))
        self.assertEqual(["EPUB", "PDF"], normalize_formats("EPUB, PDF"))
        self.assertEqual([], normalize_formats(None))
        self.assertEqual([], normalize_formats(("", None)))

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
            def format_abspath(self, book_id, format_name):
                return ("/library/book.epub", format_name)

        self.assertEqual("/library/book.epub", safe_format_path(Database(), 1, "EPUB"))

        class EmptyDatabase:
            def format_abspath(self, book_id, format_name):
                return ()

        with self.assertRaises(ValueError):
            safe_format_path(EmptyDatabase(), 1, "EPUB")

    def test_cover_bytes_handles_bytes_paths_and_unexpected_values(self):
        class Database:
            def __init__(self, value):
                self.value = value

            def cover(self, book_id, as_file=False):
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
        self.assertEqual({2}, selected_book_ids(rows))

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

    def test_empty_settings_are_migrated_without_calibre(self):
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            values = config.settings()
            self.assertEqual(config.DEFAULTS["partner_id"], values["partner_id"])
            self.assertEqual(config.DEFAULTS["preferred_formats"], values["preferred_formats"])
            self.assertEqual(config.DEFAULTS["state"], values["state"])
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
            self.assertIsInstance(values["state"], dict)
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


if __name__ == "__main__":
    unittest.main()
