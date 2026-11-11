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
        self.assertIn("plan.py", FILES)
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


if __name__ == "__main__":
    unittest.main()
