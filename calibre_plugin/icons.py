"""
Embedded icons for the Tolino Cloud Sync plugin.
SVG icons encoded as base64 for easy distribution.
"""

import base64

# Tolino Cloud Sync icon - Blue cloud with book
TOLINO_ICON_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">
  <defs>
    <linearGradient id="tolinoGradient" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" style="stop-color:#0066CC"/>
      <stop offset="100%" style="stop-color:#004499"/>
    </linearGradient>
  </defs>
  <!-- Cloud shape -->
  <path d="M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96z"
        fill="url(#tolinoGradient)" stroke="#003366" stroke-width="0.5"/>
  <!-- Book shape on cloud -->
  <rect x="10" y="11" width="6" height="4" rx="0.5" fill="white" stroke="#003366" stroke-width="0.3"/>
  <path d="M10 13h6" stroke="#003366" stroke-width="0.5"/>
  <rect x="11" y="10" width="4" height="1" fill="white" stroke="#003366" stroke-width="0.2"/>
</svg>
"""

# Simple book icon as fallback
BOOK_ICON_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">
  <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 15v-5H7v-2h4V5h2v5h4v2h-4v5h-2z"
        fill="#0066CC"/>
</svg>
"""


def get_icon_pixmap():
    """Get icon as QPixmap for Qt GUI."""
    try:
        from qt.core import QPixmap, QByteArray, QBuffer, QImage
        import io
        
        # Create SVG bytes
        svg_bytes = TOLINO_ICON_SVG.encode('utf-8')
        
        # Try to create pixmap from SVG
        pixmap = QPixmap()
        if pixmap.loadFromData(svg_bytes):
            return pixmap
        
        # Fallback: Return None and let Calibre use default
        return None
    except ImportError:
        return None


def get_icon_data():
    """Return raw SVG icon data."""
    return TOLINO_ICON_SVG


def get_icon_path():
    """Return path to icon file if available, otherwise None."""
    return None
