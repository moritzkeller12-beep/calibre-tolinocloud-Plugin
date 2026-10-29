"""Tests for building a Calibre-installable plugin archive."""
import ast
import tempfile
import unittest
import zipfile
from pathlib import Path

from build_plugin import (ICON, MARKER, ROOT, SOURCE, build_plugin,
                          plugin_modules)


class PluginArchiveTests(unittest.TestCase):
    def test_workflow_is_stored_under_github_actions_directory(self):
        workflow = ROOT / ".github" / "workflows" / "ci.yml"
        self.assertTrue(workflow.is_file(), "GitHub Actions workflow path is invalid")

    def test_build_includes_plugin_modules_and_excludes_test_runner(self):
        modules = plugin_modules()
        self.assertIn("token_vault.py", modules)
        self.assertIn("plan.py", modules)
        self.assertNotIn("test_sync.py", modules)

        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "plugin.zip"
            build_plugin(archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual({"__init__.py", MARKER, ICON,
                                  *plugin_modules()}, set(archive.namelist()))
                self.assertEqual(b"", archive.read(MARKER))
                for name in plugin_modules():
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


if __name__ == "__main__":
    unittest.main()
