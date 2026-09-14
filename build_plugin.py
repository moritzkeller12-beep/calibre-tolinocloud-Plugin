"""Build a Calibre-installable plugin zip without requiring Calibre locally."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).parent
SOURCE = ROOT / "calibre_plugin"
FILES = [path for path in SOURCE.glob("*.py")
         if path.name not in {"test_sync.py", "main.py", "__init__.py"}]
PLUGIN_NAME = "tolino_cloud_sync"
MARKER = "plugin-import-name-" + PLUGIN_NAME + ".txt"

with ZipFile(ROOT / "tolino_cloud_sync.zip", "w", ZIP_DEFLATED) as archive:
    archive.write(ROOT / "__init__.py", "__init__.py")
    archive.writestr(MARKER, "")
    for path in FILES:
        archive.write(path, path.name)

with ZipFile(ROOT / "tolino_cloud_sync.zip") as archive:
    names = set(archive.namelist())
    expected = {"__init__.py", MARKER, *(path.name for path in FILES)}
    if names != expected:
        raise RuntimeError("Plugin archive does not contain the expected Calibre namespace package")
print("Wrote tolino_cloud_sync.zip")
