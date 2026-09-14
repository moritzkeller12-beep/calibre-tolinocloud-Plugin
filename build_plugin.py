"""Build a Calibre-installable plugin zip without requiring Calibre locally."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).parent
SOURCE = ROOT / "calibre_plugin"
FILES = [path for path in SOURCE.glob("*.py") if path.name != "test_sync.py"]

with ZipFile(ROOT / "tolino_cloud_sync.zip", "w", ZIP_DEFLATED) as archive:
    for path in FILES:
        # Calibre requires __init__.py and the actual_plugin modules at ZIP root.
        archive.write(path, path.name)

with ZipFile(ROOT / "tolino_cloud_sync.zip") as archive:
    names = set(archive.namelist())
    if "__init__.py" not in names or any(name.startswith("calibre_plugin/") for name in names):
        raise RuntimeError("Plugin archive must contain flat top-level Python modules")
print("Wrote tolino_cloud_sync.zip")
