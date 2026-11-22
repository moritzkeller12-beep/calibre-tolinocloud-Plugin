"""Tests for building a Calibre-installable plugin archive."""
import ast
import io
import tempfile
import tokenize
import unittest
import zipfile
from pathlib import Path

from build_plugin import (FILES, ICON, MARKER, ROOT, SOURCE, build_plugin,
                          strip_source)


def _docstring_owners(text):
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return [node for node in ast.walk(ast.parse(text))
            if isinstance(node, owners) and ast.get_docstring(node) is not None]


class PluginArchiveTests(unittest.TestCase):
    def test_build_includes_plugin_modules_and_excludes_test_runner(self):
        self.assertIn("token_vault.py", FILES)
        self.assertIn("planning.py", FILES)
        self.assertIn("ui_dialogs.py", FILES)
        self.assertNotIn("test_sync.py", FILES)

        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "plugin.zip"
            build_plugin(archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(set(FILES), set(archive.namelist()))
                self.assertEqual(b"", archive.read(MARKER))
                for name in FILES:
                    if name == MARKER or name in (
                            "__init__.py", ICON, "README.md", "CHANGELOG.md"):
                        continue
                    source_text = (SOURCE / name).read_text(encoding="utf-8")
                    packaged_text = archive.read(name).decode("utf-8")
                    # Das Release bleibt zeilengleich und ausführbar, enthält
                    # aber weder Kommentare noch Docstrings.
                    self.assertEqual(source_text.count("\n"),
                                     packaged_text.count("\n"), name)
                    compile(packaged_text, name, "exec")
                    self.assertEqual([], _docstring_owners(packaged_text), name)
                    comments = [token for token in tokenize.generate_tokens(
                        io.StringIO(packaged_text).readline)
                        if token.type == tokenize.COMMENT]
                    self.assertEqual([], comments, name)
                self.assertEqual((ROOT / "__init__.py").read_bytes(),
                                 archive.read("__init__.py"))
                self.assertTrue(archive.read(ICON).startswith(
                    b"\x89PNG\r\n\x1a\n"),
                    "built toolbar icon is not a PNG")
                metadata = ast.parse(archive.read("__init__.py").decode("utf-8"))
                entrypoints = [
                    node.value.value
                    for node in ast.walk(metadata)
                    if isinstance(node, ast.Assign)
                    and any(getattr(target, "id", "") == "actual_plugin"
                            for target in node.targets)
                    and isinstance(node.value, ast.Constant)
                ]
                self.assertEqual(
                    ["calibre_plugins.tolino_cloud_sync.ui:TolinoSyncAction"],
                    entrypoints)

    def test_source_stripping_keeps_line_numbers_and_runtime_code(self):
        source = (b"#!/usr/bin/env python3\n"
                  b"# -*- coding: utf-8 -*-\n"
                  b'"""Doc."""\n'
                  b"\n"
                  b"# ganz weg\n"
                  b"def answer():\n"
                  b'    """Antwort."""\n'
                  b"    return 42  # auch weg\n")
        stripped = strip_source(source)
        self.assertEqual(source.count(b"\n"), stripped.count(b"\n"))
        self.assertNotIn(b"Doc.", stripped)
        self.assertNotIn(b"Antwort", stripped)
        self.assertNotIn(b"weg", stripped)
        self.assertTrue(stripped.startswith(
            b"#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n"))
        self.assertEqual(b"    return 42", stripped.splitlines()[7])
        namespace = {}
        exec(compile(stripped, "<test>", "exec"), namespace)
        self.assertEqual(42, namespace["answer"]())
        # Nicht zerlegbarer Quelltext wird unverändert ausgeliefert.
        self.assertEqual(b"x = (\n", strip_source(b"x = (\n"))

    def test_a_module_docstring_before_a_future_import_is_removed(self):
        """Ein ``0``-Platzhalter machte den Future-Import ungueltig."""
        source = (b'"""Modul."""\n'
                  b"# Hinweis\n"
                  b"from __future__ import annotations\n"
                  b"X = 1\n")
        stripped = strip_source(source)
        self.assertEqual(source.count(b"\n"), stripped.count(b"\n"))
        self.assertNotIn(b"Modul.", stripped)
        self.assertEqual(b"from __future__ import annotations",
                         stripped.splitlines()[2])
        namespace = {}
        exec(compile(stripped, "<test>", "exec"), namespace)
        self.assertEqual(1, namespace["X"])
        # Leere Rumpfe behalten ihren Platzhalter.
        self.assertIn(b"    0\n", strip_source(
            b'class Leer:\n    """Nur Text."""\n'))

    def test_a_docstring_followed_by_a_comment_is_still_stripped(self):
        source = (b"def answer():\n"
                  b'    """Antwort."""  # Hinweis\n'
                  b"    return 42\n")
        stripped = strip_source(source)
        self.assertEqual(source.count(b"\n"), stripped.count(b"\n"))
        self.assertNotIn(b"Antwort", stripped)
        self.assertNotIn(b"Hinweis", stripped)
        self.assertEqual(b"    return 42", stripped.splitlines()[2])
        namespace = {}
        exec(compile(stripped, "<test>", "exec"), namespace)
        self.assertEqual(42, namespace["answer"]())

    def test_a_module_docstring_does_not_precede_a_future_import(self):
        source = (b'"""Modul.\nBeschreibung."""  # Hinweis\n'
                  b"from __future__ import annotations\n"
                  b"class Empty:\n"
                  b'    """Klasse."""\n'
                  b"def empty():\n"
                  b'    """Funktion."""\n')
        stripped = strip_source(source)
        self.assertEqual(source.count(b"\n"), stripped.count(b"\n"))
        self.assertEqual([], _docstring_owners(stripped))
        self.assertNotIn(b"Hinweis", stripped)
        compile(stripped, "<test>", "exec")

    def test_the_documented_version_matches_the_plugin_version(self):
        """README und CHANGELOG zeigen dieselbe Nummer wie __init__.py."""
        tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
        assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(getattr(target, "id", "") == "PLUGIN_VERSION"
                    for target in node.targets))
        version = "%d.%d.%02d" % ast.literal_eval(assignment.value)
        for name in ("README.md", "CHANGELOG.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn(version, text, name)

    def test_the_token_refresh_tool_defaults_to_calibres_plugin_zip_name(self):
        """Calibre installiert das Plugin als ``<Name>.zip`` (siehe README)."""
        tree = ast.parse((ROOT / "tools" / "token_refresh.py")
                         .read_text(encoding="utf-8"))
        constant = next(node.value for node in tree.body
                        if isinstance(node, ast.Assign)
                        and any(getattr(target, "id", "") == "PLUGIN_ZIP"
                                for target in node.targets))
        self.assertEqual(ast.literal_eval(constant), "Tolino Cloud Sync.zip")
