"""Tests for building a Calibre-installable plugin archive."""
import tempfile
import unittest
import zipfile
from pathlib import Path

from build_plugin import (ICON, MARKER, ROOT, SOURCE, build_plugin,
                          plugin_modules)


class PluginModuleGraphTests(unittest.TestCase):
    def test_graph_includes_static_and_relative_from_import_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "ui.py").write_text(
                "import os\nfrom . import dialogs\n"
                "from .workers import Worker\n"
                "from importlib import import_module\n"
                "extra = import_module('.model', __package__)\n",
                encoding="utf-8")
            (source / "dialogs.py").write_text(
                "from .model import Item\n", encoding="utf-8")
            (source / "workers.py").write_text(
                "from . import model\n", encoding="utf-8")
            (source / "model.py").write_text("VALUE = 1\n", encoding="utf-8")
            (source / "unused.py").write_text("VALUE = 2\n", encoding="utf-8")

            self.assertEqual(
                ("dialogs.py", "model.py", "ui.py", "workers.py"),
                plugin_modules(source),
            )

    def test_static_relative_dunder_import_is_included(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "ui.py").write_text(
                "__import__('.worker', globals(), locals(), ['Worker'], 1)\n",
                encoding="utf-8")
            (source / "worker.py").write_text("VALUE = 1\n", encoding="utf-8")
            self.assertEqual(("ui.py", "worker.py"), plugin_modules(source))

    def test_unresolvable_dynamic_relative_import_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "ui.py").write_text(
                "from importlib import import_module\n"
                "def load(name):\n"
                "    return import_module('.' + name, __package__)\n",
                encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError,
                                        "cannot resolve dynamic relative import"):
                plugin_modules(source)

    def test_build_includes_every_reachable_module_and_excludes_unused_modules(self):
        self.assertEqual(tuple(sorted(name + ".py" for name in
                                      ("ui", "ui_common", "ui_dialogs",
                                       "ui_workers", "ui_dashboard",
                                       "ui_auth_workers", "ui_settings",
                                       "dashboard_plan", "config", "identifiers",
                                       "comparison", "sync_data", "plan",
                                       "matching", "library", "inventory",
                                       "partners", "client", "transport",
                                       "tolino", "sessions", "browser_crypto",
                                       "token_candidates", "browser_login",
                                       "browser_finder", "cdp_scripts",
                                       "cdp_transport", "cdp", "icons",
                                       "inventory_selection"))),
                         plugin_modules())
        self.assertNotIn("sync.py", plugin_modules())
        self.assertNotIn("state_store.py", plugin_modules())

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
