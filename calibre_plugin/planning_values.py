"""Qt-freie Werte und Job-Helfer der Sync-Planung.

Dieses Modul importiert bewusst kein Qt: `dashboard_plan` und die
Planungstests kommen damit ohne UI-Stubs aus.
"""
import os

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


class SyncJob:
    """Ein geplanter Upload: Calibre-Buch, Datei, Cover und alte Cloud-Kopie."""

    # `title` gehoert dazu: Fortschritt und Bericht nennen den
    # Titel, nicht die Kennung. Der Standardwert haelt aeltere Aufrufer
    # (und Tests) unveraendert am Laufen.
    __slots__ = ("book_id", "book_uuid", "format_name", "old_id", "path",
                 "cover", "title")

    def __init__(self, book_id, book_uuid, format_name, old_id, path,
                 cover, title=""):
        self.book_id = book_id
        self.book_uuid = book_uuid
        self.format_name = format_name
        self.old_id = old_id
        self.path = path
        # Cover-Bytes statt einer Temp-Datei: kein Aufraeumen noetig.
        self.cover = cover
        self.title = title

    def label(self):
        """Sichtbarer Name des Buchs: Titel, sonst «Buch <Nummer>»."""
        return display_title(self.title, self.book_id)
