"""Tests for building a Calibre-installable plugin archive."""
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


if __name__ == "__main__":
    unittest.main()
