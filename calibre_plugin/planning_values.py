"""Qt-freie Werte und Job-Helfer der Sync-Planung.

Dieses Modul importiert bewusst kein Qt: `dashboard_plan` und die
Planungstests kommen damit ohne UI-Stubs aus.
"""
import os
from typing import NamedTuple

from .identifiers import display_title
from .library import safe_format_path


def metadata_value(item, name, default=""):
    """Feld aus einem Calibre-Metadatenobjekt oder -Dict lesen."""
    try:
        return getattr(item, name)
    except AttributeError:
        try:
            return item.get(name, default)
        except AttributeError:
            return default


def plural(n, singular, plural_form):
    """Return the requested German noun form for a count."""
    return singular if int(n) == 1 else plural_form


def display_value(value):
    """Anzeigetext fuer Listen, Mengen und Dicts."""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(x) for x in value)
    if isinstance(value, dict):
        return ", ".join("%s: %s" % (key, val)
                         for key, val in sorted(value.items()))
    return str(value or "")


def format_signatures(database, book_id, formats):
    """size:mtime je Format-Datei – das billige Content-Signal.

    Ersetzt eine Datei, ändern sich Grösse und mtime; eine reine
    Metadatenänderung in Calibre berührt die Datei nicht.
    """
    signatures = {}
    for fmt in formats:
        try:
            stat = os.stat(safe_format_path(database, book_id, fmt))
        except (ValueError, OSError):
            continue
        signatures[fmt] = "%d:%d" % (stat.st_size, int(stat.st_mtime * 1000))
    return signatures


class SyncJob(NamedTuple):
    """Ein geplanter Upload: Calibre-Buch, Datei, Cover und alte Cloud-Kopie."""

    book_id: object
    book_uuid: object
    format_name: object
    old_id: object
    path: object
    cover: object
    title: object

    def label(self):
        """Sichtbarer Name des Buchs: Titel, sonst «Buch <Nummer>»."""
        return display_title(self.title, self.book_id)
