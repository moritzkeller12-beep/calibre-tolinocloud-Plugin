"""Build a Calibre-installable plugin zip without requiring Calibre locally."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).parent
FILES = [path for path in (ROOT / "calibre_plugin").glob("*.py")
         if path.name != "test_sync.py"]

with ZipFile(ROOT / "tolino_cloud_sync.zip", "w", ZIP_DEFLATED) as archive:
    for path in FILES:
        archive.write(path, path.name)
print("Wrote tolino_cloud_sync.zip")
