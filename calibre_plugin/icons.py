"""
Icons for the Tolino Cloud Sync plugin.

The bundled toolbar image lives at images/tolino_cloud_sync.png (a real
PNG, 256x256 RGBA, rendered from the SVG below). The SVG is kept as an
in-process fallback in case the bundled resource cannot be located.
"""

# Flat-bottom cloud in Tolino blue, contributed artwork (original: 90x90
# art space, fill rgb(0,137,239)).
TOLINO_ICON_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" version="1.1" width="256" height="256" viewBox="0 0 90 90">
  <g fill="#0089EF">
    <circle cx="36.89" cy="43.88" r="20.12"/>
    <circle cx="21.5" cy="52" r="11"/>
    <circle cx="56" cy="50" r="13"/>
    <rect x="0" y="45.05" width="90" height="21.64" rx="5.635" ry="5.635"/>
  </g>
</svg>
"""


def _png_bytes():
    """Return the bundled PNG bytes, or None if it cannot be located."""
    try:
        from importlib import resources
        pkg = __package__ or "calibre_plugin"
        files = resources.files(pkg)
        return (files / "images" / "tolino_cloud_sync.png").read_bytes()
    except Exception:
        pass
    # Fallback for exotic loaders (e.g. zipimport in some Calibre builds):
    # read the PNG straight out of this package's plugin zip.
    try:
        import zipfile
        import os
        module_file = globals().get("__file__") or ""
        if module_file.endswith(".zip"):
            with zipfile.ZipFile(module_file) as archive:
                return archive.read("images/tolino_cloud_sync.png")
        base = os.path.dirname(os.path.abspath(module_file))
        with open(os.path.join(base, "images", "tolino_cloud_sync.png"), "rb") as handle:
            return handle.read()
    except Exception:
        return None


def _svg_bytes():
    return TOLINO_ICON_SVG.encode("utf-8")


def pixmap_from_bytes(data, fmt=None):
    """Decode bytes into a QPixmap; returns a null pixmap on failure."""
    from qt.core import QByteArray, QPixmap
    pixmap = QPixmap()
    if not data:
        return pixmap
    try:
        if pixmap.loadFromData(QByteArray(data), fmt):
            return pixmap
    except Exception:
        pass
    try:
        pixmap = QPixmap()
        if pixmap.loadFromData(bytes(data)):
            return pixmap
    except Exception:
        pass
    return pixmap


def toolbar_icon():
    """Build the toolbar QIcon with a robust fallback chain.

    1. Calibre's resource system (reads images/… from the plugin zip).
    2. The bundled PNG bytes decoded directly.
    3. The embedded SVG decoded via QPixmap (QIcon itself has no
       loadFromData - routing the bytes through a QPixmap is required).
    """
    from qt.core import QIcon

    icon = QIcon()
    try:
        from calibre.gui2 import get_icons
        icon = get_icons("images/tolino_cloud_sync.png", "tolino_cloud_sync")
    except Exception:
        icon = QIcon()
    if icon is not None and not icon.isNull():
        return icon

    png = _png_bytes()
    if png:
        pixmap = pixmap_from_bytes(png, "png")
        if not pixmap.isNull():
            return QIcon(pixmap)

    pixmap = pixmap_from_bytes(_svg_bytes(), "svg")
    if not pixmap.isNull():
        return QIcon(pixmap)
    return QIcon()


def get_icon_pixmap():
    """Get the icon as a QPixmap for Qt GUI code (None when unavailable)."""
    try:
        pixmap = pixmap_from_bytes(_png_bytes() or b"", "png")
        if not pixmap.isNull():
            return pixmap
    except Exception:
        pass
    try:
        pixmap = pixmap_from_bytes(_svg_bytes(), "svg")
        if not pixmap.isNull():
            return pixmap
    except Exception:
        pass
    return None


def get_icon_data():
    """Return raw SVG icon data."""
    return TOLINO_ICON_SVG


def get_icon_path():
    """Return path to icon file if available, otherwise None."""
    return None
