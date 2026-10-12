"""Build a Calibre-installable plugin zip without requiring Calibre locally.

The plugin metadata (``__init__.py``) is the repository-root file; the
modules live under ``calibre_plugin/`` and are copied into the zip. That
keeps a single source of truth for ``PLUGIN_VERSION``.
"""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).parent
SOURCE = ROOT / "calibre_plugin"
MARKER = "plugin-import-name-tolino_cloud_sync.txt"
FILES = ("ui.py", "config.py", "sync.py", "tolino.py", "cdp.py",
         "icons.py")
ICON = "images/tolino_cloud_sync.png"
EXPECTED = {"__init__.py", MARKER, *FILES, ICON}


with ZipFile(ROOT / "tolino_cloud_sync.zip", "w", ZIP_DEFLATED) as archive:
    archive.write(ROOT / "__init__.py", "__init__.py")
    archive.writestr(MARKER, "")
    for name in FILES:
        archive.write(SOURCE / name, name)
    archive.write(SOURCE / ICON, ICON)

with ZipFile(ROOT / "tolino_cloud_sync.zip") as archive:
    names = set(archive.namelist())
    if names != EXPECTED:
        raise RuntimeError("Plugin archive does not contain the expected Calibre package layout")
print("Wrote tolino_cloud_sync.zip")
