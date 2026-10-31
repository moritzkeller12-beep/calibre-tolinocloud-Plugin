"""Build a deterministic Calibre plugin archive without Calibre installed."""
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT, SOURCE = Path(__file__).parent, Path(__file__).parent / "calibre_plugin"
MARKER = "plugin-import-name-tolino_cloud_sync.txt"
ICON = "images/tolino_cloud_sync.png"
FILES = ("__init__.py", *sorted(p.name for p in SOURCE.glob("*.py")
                                if p.name != "test_sync.py"), ICON, MARKER)
DATE, MODE = (2026, 1, 1, 0, 0, 0), 0o644 << 16


def build_plugin(archive_path=ROOT / "tolino_cloud_sync.zip"):
    archive_path = Path(archive_path)
    with ZipFile(archive_path, "w", ZIP_DEFLATED, compresslevel=9) as archive:
        for name in FILES:
            data = b"" if name == MARKER else (
                ROOT / name if name == "__init__.py" else SOURCE / name).read_bytes()
            info = ZipInfo(name, date_time=DATE)
            info.compress_type, info.external_attr = ZIP_DEFLATED, MODE
            archive.writestr(info, data, compress_type=ZIP_DEFLATED, compresslevel=9)
    with ZipFile(archive_path) as archive:
        if set(archive.namelist()) != set(FILES):
            raise RuntimeError("plugin archive layout mismatch")
    print(f"Wrote {archive_path.name} ({len(FILES)} entries, "
          f"{archive_path.stat().st_size} bytes)")
    return archive_path


if __name__ == "__main__":
    build_plugin(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tolino_cloud_sync.zip")
