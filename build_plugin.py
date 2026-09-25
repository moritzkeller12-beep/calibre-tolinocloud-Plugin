"""Build a Calibre-installable plugin zip without requiring Calibre locally."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).parent
SOURCE = ROOT / "calibre_plugin"
MARKER = "plugin-import-name-tolino_cloud_sync.txt"
FILES = ("ui.py", "config.py", "sync.py", "tolino.py", "cdp.py",
         "icons.py",
         "bootstrapper.py")
IMAGE_FILES = ("tolino_cloud_sync.png",)
EXPECTED = {
    "__init__.py",
    MARKER,
    *FILES,
    "images/tolino_cloud_sync.png",
}

def _plugin_version(source_path):
    """PLUGIN_VERSION tuple declared by an __init__.py copy."""
    for line in source_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("PLUGIN_VERSION"):
            return line
    raise RuntimeError("%s does not declare PLUGIN_VERSION" % source_path)


_PACKAGED_VERSION = _plugin_version(ROOT / "__init__.py")
if _PACKAGED_VERSION != _plugin_version(SOURCE / "__init__.py"):
    raise RuntimeError("PLUGIN_VERSION drift between packaged and test copies:\n  %s\n  %s"
                       % (_PACKAGED_VERSION, _plugin_version(SOURCE / "__init__.py")))

with ZipFile(ROOT / "tolino_cloud_sync.zip", "w", ZIP_DEFLATED) as archive:
    archive.write(ROOT / "__init__.py", "__init__.py")
    archive.writestr(MARKER, "")
    for name in FILES:
        archive.write(SOURCE / name, name)
    # Add images directory
    archive.write(SOURCE / "images" / "tolino_cloud_sync.png", "images/tolino_cloud_sync.png")

with ZipFile(ROOT / "tolino_cloud_sync.zip") as archive:
    names = set(archive.namelist())
    if names != EXPECTED:
        raise RuntimeError("Plugin archive does not contain the expected Calibre package layout")
print("Wrote tolino_cloud_sync.zip")
