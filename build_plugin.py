"""Build a Calibre-installable plugin zip without requiring Calibre locally."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).parent
SOURCE = ROOT / "calibre_plugin"
PACKAGE = "calibre_plugins/tolino_cloud_sync"
FILES = ("ui.py", "config.py", "sync.py", "tolino.py")
EXPECTED = {
    "__init__.py",
    "calibre_plugins/__init__.py",
    f"{PACKAGE}/__init__.py",
    *(f"{PACKAGE}/{name}" for name in FILES),
}

with ZipFile(ROOT / "tolino_cloud_sync.zip", "w", ZIP_DEFLATED) as archive:
    archive.write(ROOT / "__init__.py", "__init__.py")
    archive.writestr("calibre_plugins/__init__.py", "")
    archive.writestr(f"{PACKAGE}/__init__.py", "")
    for name in FILES:
        archive.write(SOURCE / name, f"{PACKAGE}/{name}")

with ZipFile(ROOT / "tolino_cloud_sync.zip") as archive:
    names = set(archive.namelist())
    if names != EXPECTED:
        raise RuntimeError("Plugin archive does not contain the expected Calibre package layout")
print("Wrote tolino_cloud_sync.zip")
