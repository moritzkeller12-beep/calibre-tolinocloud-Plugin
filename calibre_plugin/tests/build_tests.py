"""Tests for building a Calibre-installable plugin archive."""
import ast
import tempfile
import unittest
import zipfile
from pathlib import Path

from build_plugin import FILES, ICON, MARKER, ROOT, SOURCE, build_plugin


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
                    if name == MARKER or name in ("__init__.py", ICON):
                        continue
                    self.assertEqual((SOURCE / name).read_bytes(), archive.read(name))
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

    def test_the_current_version_is_defined_once(self):
        """README and CHANGELOG link to __init__.py instead of repeating it."""
        tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
        assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(getattr(target, "id", "") == "PLUGIN_VERSION"
                    for target in node.targets))
        version = "%d.%d.%02d" % ast.literal_eval(assignment.value)
        for name in ("README.md", "CHANGELOG.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn(version, text, name)


if __name__ == "__main__":
    unittest.main()
