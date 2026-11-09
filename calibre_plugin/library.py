"""Adapters for Calibre library and Qt APIs used by the plugin."""
import hashlib
import json
import re
from collections.abc import Iterable

TOLINO_COLUMN = "#tolino_id"
# Optionale Ja/Nein-Spalten: #tolino_ignore nimmt Buecher vom Abgleich aus;
# #tolino_read zeigt den lokalen Lesestatus nur informativ an.
TOLINO_IGNORE_COLUMN = "#tolino_ignore"
TOLINO_READ_COLUMN = "#tolino_read"
# #tolino_progress spiegelt den aus tolino gelesenen Lesefortschritt (0-100).
TOLINO_PROGRESS_COLUMN = "#tolino_progress"

# Calibre liefert ein Ja/Nein-Feld je nach Weg als Bool, Text oder Zahl.
_TRUE_VALUES = frozenset(("true", "yes", "ja", "1"))
_FALSE_VALUES = frozenset(("false", "no", "nein", "0"))
# Ohne Bibliothekskennung gibt es keinen echten Scope. Ein fester Ersatz
# verhindert, dass der alte ungescopte Schluessel stillschweigend entsteht.
DEFAULT_LIBRARY_SCOPE = hashlib.sha256(
    b"tolino-cloud-sync-default-library").hexdigest()[:12]


def library_scope(database):
    """Return a stable short namespace for one Calibre library.

    `library_id` is preferred; `dbpath` is the fallback for older Calibre
    versions. Hashing keeps the state key short and avoids embedding a
    filesystem path in preferences. If both are unavailable, a fixed
    placeholder scope keeps fallback keys namespaced instead of recreating
    the old unscoped ``calibre-<id>`` form.
    """
    raw = (getattr(database, "library_id", "")
           or getattr(database, "dbpath", ""))
    text = str(raw or "").strip()
    if not text:
        return DEFAULT_LIBRARY_SCOPE
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _column_value(metadata):
    """Read the raw Calibre custom-column value."""
    getter = getattr(metadata, "get", None)
    if callable(getter):
        return getter(TOLINO_COLUMN, "") or ""
    return ""


def _decode_tolino_ids(value):
    """Return the account map; malformed or plain values are empty."""
    if isinstance(value, dict):
        decoded = value
    elif isinstance(value, str) and value.strip().startswith("{"):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
    else:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(name): str(book_id).strip()
            for name, book_id in decoded.items()
            if name and isinstance(book_id, (str, int)) and str(book_id).strip()}


def _encode_tolino_ids(ids):
    return json.dumps(ids, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")) if ids else ""


def metadata_tolino_id(metadata, account_name=None):
    """Read this account's ID from its account-map column."""
    account_ids = _decode_tolino_ids(_column_value(metadata))
    return str(account_ids.get(str(account_name), "") or "").strip()


def _column_available(database, column, datatypes):
    fields = database.field_metadata
    return column in fields and fields[column].get("datatype") in datatypes


def bool_column_available(database, column):
    """True, wenn Calibre ``column`` als Ja/Nein-Feld kennt.

    Ein Text- oder Zahlenfeld gilt nicht: dort stuenden fremde Werte, die
    wie ein versehentliches «ignoriert» aussehen koennten.
    """
    return _column_available(database, column, ("bool",))


def integer_column_available(database, column):
    """True, wenn Calibre ``column`` als Zahl (int/float) kennt."""
    return _column_available(database, column, ("int", "float"))


def bool_column_value(metadata, column):
    """Ja/Nein-Wert einer Spalte: True, False oder None (leer/unbekannt)."""
    getter = getattr(metadata, "get", None)
    value = getter(column, None) if callable(getter) else None
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().casefold()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return None


def metadata_ignored(metadata):
    """True, wenn das Buch vom Abgleich ausgeschlossen ist.

    Nur ein deutliches «Ja» zaehlt: ein leeres oder unbekanntes Feld ist
    KEIN Ausschluss -- sonst verschwaende ein Buch wegen eines Tippfehlers
    aus dem Vergleich.
    """
    return bool_column_value(metadata, TOLINO_IGNORE_COLUMN) is True


# Spalten, die das Plugin benutzt: (Label ohne «#», Anzeigename, Calibre-Typ).
CUSTOM_COLUMNS = (
    ("tolino_id", "tolino-ID", "text"),
    ("tolino_read", "Gelesen (tolino)", "bool"),
    ("tolino_progress", "tolino gelesen %", "int"),
    ("tolino_ignore", "Ignoriert (tolino)", "bool"),
)

# Klartext des Calibre-Typs fuer die Oberflaeche (je Spalte ein Knopf).
COLUMN_KIND_NAMES = {"text": "Text", "bool": "Ja/Nein",
                     "int": "Zahl", "float": "Zahl"}

# In dieser Calibre-Sitzung angelegte Spalten. Calibre fuehrt eine neue
# Spalte in `field_metadata` erst nach einem Neustart -- ohne diesen Merker
# wollte ein zweiter Klick dieselben Spalten erneut anlegen.
CREATED_COLUMNS = set()


def _column_present(database, label):
    """True, wenn die Spalte in der Bibliothek (oder in dieser Sitzung) da ist."""
    return label in CREATED_COLUMNS or ("#" + label) in database.field_metadata


def create_custom_columns(database, labels=None):
    """Fehlende Plugin-Spalten anlegen; gibt die angelegten Labels zurueck.

    Calibre kennt dafuer ``create_custom_column(label, name, datatype,
    is_multiple, editable, display)`` -- auf ``new_api`` wie auf dem
    Datenbankobjekt. Vorhandene Spalten werden nie angefasst: es wird weder
    etwas geloescht noch ein Typ geaendert.

    ``labels`` grenzt das auf einzelne Spalten ein -- in den Einstellungen
    legt so ein Knopf je Spalte nur SEINE Spalte an. Fehlt nichts, wird
    auch die Calibre-Schnittstelle nicht befragt.

    Eine neu angelegte Spalte ist in der laufenden Oberflaeche erst nach
    einem Neustart von Calibre nutzbar.
    """
    wanted = None if labels is None else set(labels)
    selected = (CUSTOM_COLUMNS if wanted is None else
                tuple(spec for spec in CUSTOM_COLUMNS if spec[0] in wanted))
    missing = [spec for spec in selected
               if not _column_present(database, spec[0])]
    if not missing:
        return []
    create = getattr(database, "create_custom_column", None)
    if not callable(create):
        create = getattr(getattr(database, "new_api", None),
                         "create_custom_column", None)
    if not callable(create):
        raise RuntimeError(
            "Diese Calibre-Version kennt create_custom_column nicht. Lege "
            "die Spalten bitte von Hand an: %s."
            % ", ".join("#" + label for label, _name, _datatype
                        in missing))
    created = []
    for label, name, datatype in missing:
        create(label, name, datatype, False, editable=True, display={})
        CREATED_COLUMNS.add(label)
        created.append(label)
    return created


def column_state(database):
    """Welche tolino-Spalten es gibt -- einmal je Lauf gefragt.

    Ein Dict statt zweier Flags und wiederholter Datenbankabfragen: der
    Lauf reicht es an den Worker weiter und schreibt nur in vorhandene
    Spalten.
    """
    return {"id": TOLINO_COLUMN in database.field_metadata,
            "read": bool_column_available(database, TOLINO_READ_COLUMN),
            "progress": integer_column_available(
                database, TOLINO_PROGRESS_COLUMN)}


_SKIP = object()


def _progress_value(value):
    if value is None:
        return None
    try:
        value = round(float(value))
    except (TypeError, ValueError, OverflowError):
        return _SKIP
    return value if 0 <= value <= 100 else _SKIP


def _update_column(database, column, updates, convert, datatypes):
    """Gemeinsamer Weg für die informativen Calibre-Spalten."""
    if not updates or not _column_available(database, column, datatypes):
        return 0
    api = database.new_api
    field_for = getattr(api, "field_for", None)
    values = {}
    for book_id, raw in updates:
        if book_id is None or not api.has_id(book_id):
            continue
        value = None if raw is None else convert(raw)
        if value is _SKIP:
            continue
        # Kein Schreibvorgang, wenn der Wert schon stimmt: Calibre markiert
        # sonst bei jedem Vergleich die metadata.opf als geändert.
        if callable(field_for) and field_for(column, book_id) == value:
            continue
        values[book_id] = value
    if values:
        api.set_field(column, values)
    return len(values)


def update_read_status(database, updates):
    """Bestätigten Cloud-Lesestatus in ``#tolino_read`` spiegeln.

    Die Spalte bleibt informativ: geschrieben wird nur, was tolino nach einem
    Lauf bestätigt hat -- sie ist nie Quelle für eine Übertragung. ``None``
    leert sie beim Konto-Wechsel, statt «unbekannt» als ``False`` zu zeigen.
    """
    return _update_column(database, TOLINO_READ_COLUMN, updates, bool,
                          ("bool",))


def update_progress_status(database, updates):
    """Cloud-Fortschritt (0-100) in ``#tolino_progress`` spiegeln.

    Die Spalte ist rein informativ. ``None`` leert sie, wenn die Cloud für
    ein zugeordnetes Buch keinen Bookmark mehr liefert; unbekannte Stände
    werden vom Aufrufer gar nicht erst übergeben.
    """
    return _update_column(database, TOLINO_PROGRESS_COLUMN, updates,
                          _progress_value, ("int", "float"))


def update_tolino_ids(database, updates, account_name=None):
    """Merge this account's IDs into the shared Calibre custom column.

    A historic plain value is replaced by the account map after a warning;
    it cannot be assigned to an account safely and would otherwise block
    the per-account updates.
    """
    if not updates or TOLINO_COLUMN not in database.field_metadata:
        return 0
    account_name = str(account_name or "").strip()
    if not account_name:
        return 0
    api = database.new_api
    values = {}
    legacy_replaced = False
    for book_id, tolino_id in updates:
        if book_id is None or not api.has_id(book_id):
            continue
        new_id = str(tolino_id or "").strip()
        raw = api.field_for(TOLINO_COLUMN, book_id)
        if raw and not str(raw).strip().startswith("{"):
            legacy_replaced = True
        account_ids = _decode_tolino_ids(raw)
        if new_id:
            account_ids[account_name] = new_id
        else:
            account_ids.pop(account_name, None)
        values[book_id] = _encode_tolino_ids(account_ids)
    if not values:
        return 0
    api.set_field(TOLINO_COLUMN, values)
    if legacy_replaced:
        from .tolino import log_warning
        log_warning("Alte Einzel-ID in #tolino_id wurde durch die "
                    "kontobezogene Zuordnung ersetzt.")
    return len(values)


def normalize_formats(value):
    """Return canonical format names from Calibre string or iterable values."""
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        return []
    cleaned = ("" if part is None else str(part).strip().lstrip(".").upper()
               for part in values)
    return list(dict.fromkeys(text for text in cleaned if text))



def safe_format_path(database, book_id, format_name):
    """Resolve a real Calibre id without falling back to view-index semantics."""
    if not isinstance(format_name, str):
        raise ValueError("Invalid format name: %r" % (format_name,))
    format_name = format_name.strip().lstrip(".").upper()
    if not format_name or not re.match(r"^[A-Z0-9][A-Z0-9._-]*$", format_name):
        raise ValueError("Invalid format name: %r" % (format_name,))
    api = database.new_api
    if not api.has_id(book_id):
        raise ValueError("Calibre database has no book with id %r." % (book_id,))
    try:
        path = api.format_abspath(book_id, format_name)
    except (ValueError, IndexError, KeyError, OSError) as exc:
        raise ValueError("Calibre returned no path for book %r format %s" %
                         (book_id, format_name)) from exc
    if not isinstance(path, str) or not path:
        raise ValueError("Calibre returned no path for book %r format %s" %
                         (book_id, format_name))
    return path


def cover_bytes(database, book_id):
    """Return the cover image bytes from Calibre, or None when unavailable."""
    api = database.new_api
    if not api.has_id(book_id):
        raise ValueError("Calibre database has no book with id %r." % (book_id,))
    return api.cover(book_id, as_file=False)


def image_suffix(data):
    """Return a recognized image suffix, or '' for unknown cover bytes."""
    if not isinstance(data, bytes) or not data:
        return ""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ""

