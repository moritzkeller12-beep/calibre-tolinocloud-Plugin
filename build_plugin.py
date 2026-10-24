"""Build a Calibre-installable plugin zip without requiring Calibre locally.

The plugin metadata (``__init__.py``) is the repository-root file; the
modules live under ``calibre_plugin/`` and are copied into the zip. That
keeps a single source of truth for ``PLUGIN_VERSION``.

The archive is reproducible: fixed timestamps and permissions mean the same
source state produces a byte-identical zip. Every Python entry is
read back and compared with its source file before the build is declared
successful -- the earlier hand-written checks in the test reports could (and
did) verify the wrong file.
"""
import ast
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


def _is_import_module_call(func, imported_names, module_names):
    """Recognize imported and qualified importlib.import_module calls."""
    if isinstance(func, ast.Name):
        return func.id in imported_names
    return (isinstance(func, ast.Attribute)
            and func.attr == "import_module"
            and isinstance(func.value, ast.Name)
            and func.value.id in module_names)


def _static_relative_import(args, source_file):
    """Resolve the module string of a relative dynamic import, if literal."""
    if args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
        module_name = args[0].value
        if module_name.startswith("."):
            return module_name.lstrip(".").split(".", 1)[0]
        return None
    raise RuntimeError(
        "cannot resolve dynamic relative import in %s" % source_file.name)


def plugin_modules(source=SOURCE):
    """Return modules reachable from the Calibre entrypoint's local imports."""
    pending = ["ui"]
    included = set()
    while pending:
        name = pending.pop()
        if name in included:
            continue
        source_file = source / (name + ".py")
        if not source_file.is_file():
            raise RuntimeError("Plugin module %r has no source file" % name)
        included.add(name)
        tree = ast.parse(source_file.read_text(encoding="utf-8"),
                         filename=str(source_file))
        import_module_names = {"import_module"}
        importlib_names = {"importlib"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "importlib":
                        importlib_names.add(alias.asname or "importlib")
            elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
                for alias in node.names:
                    if alias.name == "import_module":
                        import_module_names.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                dependencies = ([node.module.split(".", 1)[0]]
                                if node.module else
                                [alias.name for alias in node.names])
                for dependency in dependencies:
                    if (source / (dependency + ".py")).is_file():
                        pending.append(dependency)
            elif (isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Name)
                  and node.func.id == "__import__"):
                dependency = _static_relative_import(node.args, source_file)
                if dependency is not None:
                    pending.append(dependency)
            elif isinstance(node, ast.Call) and _is_import_module_call(
                    node.func, import_module_names, importlib_names):
                dependency = _static_relative_import(node.args, source_file)
                if dependency is not None:
                    pending.append(dependency)
    return tuple(sorted(name + ".py" for name in included))


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
    if archive_path.resolve() == (ROOT / "calibre_plugin.test_sync").resolve():
        raise ValueError("Plugin archive path must not overwrite the test runner module")
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
        for name in CONTENT_ENTRIES:
            if archive.read(name) != source_for(name).read_bytes():
                raise RuntimeError(
                    "Plugin archive entry %r does not match its source file" % name)

    print("Wrote %s (%d entries, %d bytes)"
          % (archive_path.name, len(EXPECTED), archive_path.stat().st_size))


if __name__ == "__main__":
    build_plugin(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ARCHIVE)
