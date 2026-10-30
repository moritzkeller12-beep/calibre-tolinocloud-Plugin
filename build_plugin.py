"""Build a Calibre-installable plugin zip without requiring Calibre locally.

The plugin metadata (``__init__.py``) is the repository-root file; the
modules live under ``calibre_plugin/`` and are copied into the zip. That
keeps a single source of truth for ``PLUGIN_VERSION``.

The archive is reproducible: fixed timestamps and permissions mean the same
source state produces a byte-identical zip. A name check catches layout
mistakes without reading the just-written entries back against themselves.
"""
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).parent
SOURCE = ROOT / "calibre_plugin"
# Optional output path, e.g.
#   python3 build_plugin.py out.zip
DEFAULT_ARCHIVE = ROOT / "tolino_cloud_sync.zip"
MARKER = "plugin-import-name-tolino_cloud_sync.txt"
ICON = "images/tolino_cloud_sync.png"


def plugin_modules(source=SOURCE):
    """Return the plugin's top-level Python modules, excluding the test runner."""
    return tuple(sorted(path.name for path in source.glob("*.py")
                        if path.name != "test_sync.py"))


FILES = plugin_modules()
PYTHON_ENTRIES = ("__init__.py",) + FILES
CONTENT_ENTRIES = PYTHON_ENTRIES + (ICON,)
EXPECTED = {"__init__.py", MARKER, *FILES, ICON}

# Feste Metadaten: gleicher Quellstand -> gleiche Bytes, auch im Git-Diff.
FIXED_DATE = (2026, 1, 1, 0, 0, 0)
FIXED_PERMISSIONS = 0o644 << 16


def source_for(name):
    """Repo file behind a zip entry: ``__init__.py`` is the root metadata."""
    return ROOT / name if name == "__init__.py" else SOURCE / name


def add_entry(archive, name, data):
    """Write one deterministic entry (fixed time, mode, compression)."""
    info = ZipInfo(name, date_time=FIXED_DATE)
    info.compress_type = ZIP_DEFLATED
    info.external_attr = FIXED_PERMISSIONS
    archive.writestr(info, data, compress_type=ZIP_DEFLATED, compresslevel=9)


def build_plugin(archive_path=DEFAULT_ARCHIVE):
    """Create and verify the deterministic Calibre plugin archive."""
    archive_path = Path(archive_path)
    with ZipFile(archive_path, "w", ZIP_DEFLATED, compresslevel=9) as archive:
        for name in CONTENT_ENTRIES:
            add_entry(archive, name, source_for(name).read_bytes())
        add_entry(archive, MARKER, b"")

    with ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        if names != EXPECTED:
            raise RuntimeError(
                "Plugin archive does not contain the expected Calibre package "
                "layout (missing: %s, unexpected: %s)"
                % (sorted(EXPECTED - names), sorted(names - EXPECTED)))

    print("Wrote %s (%d entries, %d bytes)"
          % (archive_path.name, len(EXPECTED), archive_path.stat().st_size))


if __name__ == "__main__":
    build_plugin(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ARCHIVE)
