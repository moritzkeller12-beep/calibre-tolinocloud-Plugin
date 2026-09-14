"""Stable top-level Calibre InterfaceAction entry point.

Calibre loads ``actual_plugin`` from a ZipPlugin context.  Keeping this
adapter separate avoids relying on package-relative imports in that context.
"""
from ui import TolinoSyncAction

__all__ = ["TolinoSyncAction"]
