"""Adapters for Calibre library and Qt APIs used by the plugin."""
import json
import re
from collections.abc import Iterable

TOLINO_COLUMN = "#tolino_id"
# Optionale Ja/Nein-Spalten: #tolino_ignore nimmt Buecher vom Abgleich aus,
# #tolino_read spiegelt den Lesestatus in die Bibliothek.
TOLINO_IGNORE_COLUMN = "#tolino_ignore"
TOLINO_READ_COLUMN = "#tolino_read"

# Calibre liefert ein Ja/Nein-Feld je nach Weg als Bool, Text oder Zahl.
_TRUE_VALUES = frozenset(("true", "yes", "ja", "1"))
_FALSE_VALUES = frozenset(("false", "no", "nein", "0"))


def _column_value(metadata):
    """Read the raw Calibre custom-column value."""
    getter = getattr(metadata, "get", None)
    if callable(getter):
        return getter(TOLINO_COLUMN, "") or getter("tolino_id", "") or ""
    return ""


def _decode_tolino_ids(value):
    """Return the account map, or None when value is a legacy single ID."""
    if isinstance(value, dict):
        decoded = value
    elif isinstance(value, str) and value.strip().startswith("{"):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return None
    else:
        return None
    if not isinstance(decoded, dict):
        return None
    return {str(name): str(book_id).strip()
            for name, book_id in decoded.items()
            if name and isinstance(book_id, (str, int)) and str(book_id).strip()}


def _encode_tolino_ids(ids):
    return json.dumps(ids, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")) if ids else ""


def legacy_tolino_id(metadata):
    """Return a pre-account ID so the first active account can adopt it."""
    value = _column_value(metadata)
    return "" if _decode_tolino_ids(value) is not None else str(value or "").strip()


def metadata_tolino_id(metadata, account_name=None):
    """Read this account's ID from a legacy value or account-map column."""
    value = _column_value(metadata)
    account_ids = _decode_tolino_ids(value)
    if account_ids is not None:
        return str(account_ids.get(str(account_name), "") or "").strip()
    return str(value or "").strip()


def _field_info(database, column):
    """(vorhanden, Feldinfo) aus Calibres oeffentlichen Feldmetadaten."""
    fields = getattr(database, "field_metadata", None)
    if callable(fields):
        fields = fields()
    if fields is None:
        return False, None
    try:
        if column not in fields:
            return False, None
    except (TypeError, AttributeError):
        return False, None
    # Ein Test-Stub oder eine aeltere Calibre-Fassung liefert moeglicherweise
    # keine Feldinfo: dann gilt die Spalte als vorhanden, aber unbekannt.
    try:
        return True, fields[column]
    except (TypeError, AttributeError, KeyError, IndexError):
        return True, None


def custom_column_available(database):
    """Check Calibre's public field metadata for the configured custom column."""
    return _field_info(database, TOLINO_COLUMN)[0]


def bool_column_available(database, column):
    """True, wenn Calibre ``column`` als Ja/Nein-Feld kennt.

    Ein Text- oder Zahlenfeld gilt nicht: dort stuenden fremde Werte, die
    wie ein versehentliches «ignoriert» aussehen koennten.
    """
    present, info = _field_info(database, column)
    if not present:
        return False
    datatype = info.get("datatype") if isinstance(info, dict) else None
    return datatype in (None, "bool")


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


def ignore_column_available(database):
    """True, wenn Calibre die Spalte ``#tolino_ignore`` kennt."""
    return bool_column_available(database, TOLINO_IGNORE_COLUMN)


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
    ("tolino_ignore", "Ignoriert (tolino)", "bool"),
)

# Klartext des Calibre-Typs fuer die Oberflaeche (je Spalte ein Knopf).
COLUMN_KIND_NAMES = {"text": "Text", "bool": "Ja/Nein"}

# In dieser Calibre-Sitzung angelegte Spalten. Calibre fuehrt eine neue
# Spalte in `field_metadata` erst nach einem Neustart -- ohne diesen Merker
# wollte ein zweiter Klick dieselben Spalten erneut anlegen.
CREATED_COLUMNS = set()


def _column_present(database, label):
    """True, wenn die Spalte in der Bibliothek (oder in dieser Sitzung) da ist."""
    return label in CREATED_COLUMNS or _field_info(database, "#" + label)[0]


def create_custom_columns(database, labels=None):
    """Fehlende Plugin-Spalten anlegen; gibt die angelegten Labels zurueck.

    Calibre kennt dafuer ``create_custom_column(label, name, datatype,
    is_multiple, editable, display)`` -- auf ``new_api`` wie auf dem
    Legacy-Objekt. Vorhandene Spalten werden nie angefasst: es wird weder
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
        try:
            create(label, name, datatype, False, editable=True, display={})
        except TypeError:
            # Aeltere Fassungen kennen die Schluesselwoerter nicht.
            create(label, name, datatype, False)
        CREATED_COLUMNS.add(label)
        created.append(label)
    return created


def read_column_available(database):
    """True, wenn Calibre die Ja/Nein-Spalte ``#tolino_read`` kennt.

    Eine Spalte mit anderem Typ wird nicht beschrieben: ein Text- oder
    Zahlenfeld würde ``True``/``False`` als fremden Wert speichern.
    """
    return bool_column_available(database, TOLINO_READ_COLUMN)


def metadata_read_value(metadata):
    """Lesestatus aus ``#tolino_read``: True, False oder None (leer)."""
    return bool_column_value(metadata, TOLINO_READ_COLUMN)


def update_read_column(database, updates):
    """Lesestatus ``(book_id, bool)`` in ``#tolino_read`` schreiben."""
    if not updates or not read_column_available(database):
        return 0
    api = database.new_api
    values = {book_id: bool(value) for book_id, value in updates
              if book_id is not None and api.has_id(book_id)}
    if not values:
        return 0
    api.set_field(TOLINO_READ_COLUMN, values)
    return len(values)


def update_tolino_ids(database, updates, enabled=True, account_name=None):
    """Merge this account's IDs into the shared Calibre custom column."""
    if not enabled or not updates or not custom_column_available(database):
        return 0
    api = database.new_api
    account_name = str(account_name or "").strip()
    values = {}
    for book_id, tolino_id in updates:
        if book_id is None or not api.has_id(book_id):
            continue
        new_id = str(tolino_id or "").strip()
        if not account_name:
            values[book_id] = new_id
            continue
        metadata = api.get_metadata(book_id)
        raw = _column_value(metadata)
        account_ids = _decode_tolino_ids(raw)
        if account_ids is None:
            # Migration from the old one-ID format: attribute it to the
            # account currently active when the column was first written.
            legacy = str(raw or "").strip()
            account_ids = {}
            if legacy:
                account_ids[account_name] = legacy
        if new_id:
            account_ids[account_name] = new_id
        else:
            account_ids.pop(account_name, None)
        values[book_id] = _encode_tolino_ids(account_ids)
    if not values:
        return 0
    api.set_field(TOLINO_COLUMN, values)
    return len(values)


def iter_book_ids(database):
    """Yield book ids from Calibre 5+'s supported database API."""
    api = database.new_api
    yield from api.all_book_ids()


def metadata_by_id(database, book_id):
    """Read metadata by a real Calibre id, never by a filtered-view index."""
    return database.new_api.get_metadata(book_id)


def normalize_formats(value):
    """Return canonical format names from Calibre string or iterable values."""
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        return []
    formats = []
    for part in values:
        if part is None:
            continue
        text = str(part).strip().lstrip(".").upper()
        if text and text not in formats:
            formats.append(text)
    return formats



def safe_format_path(database, book_id, format_name):
    """Resolve a real Calibre id without falling back to view-index semantics."""
    if isinstance(book_id, bool) or not isinstance(book_id, int) or book_id <= 0:
        raise ValueError("Invalid Calibre book id: %r" % (book_id,))
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
    except ValueError as exc:
        raise ValueError("Calibre returned no path for book %r format %s" %
                         (book_id, format_name)) from exc
    except (IndexError, KeyError, OSError) as exc:
        raise ValueError("Calibre returned no path for book %r format %s" %
                         (book_id, format_name)) from exc
    if not isinstance(path, str) or not path:
        raise ValueError("Calibre returned no path for book %r format %s" %
                         (book_id, format_name))
    return path


def cover_bytes(database, book_id):
    """Normalize cover() results from bytes, paths, or wrapped Calibre values."""
    if isinstance(book_id, bool) or not isinstance(book_id, int) or book_id <= 0:
        raise ValueError("Invalid Calibre book id: %r" % (book_id,))
    api = database.new_api
    if not api.has_id(book_id):
        raise ValueError("Calibre database has no book with id %r." % (book_id,))
    cover = api.cover(book_id, as_file=False)
    return cover


def image_suffix(data):
    """Return a recognized image suffix, or '' for unknown cover bytes."""
    if not isinstance(data, bytes) or not data:
        return ""
    raw = data
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return ""


def selected_table_rows(table):
    """Return valid selected Qt row numbers without assuming QModelIndex shape."""
    if table is None:
        return []
    try:
        selection_model = table.selectionModel()
        selected = selection_model.selectedRows() if selection_model else ()
    except RuntimeError:
        return []
    rows = []
    for index in selected or ():
        row_value = getattr(index, "row", None)
        row = row_value() if callable(row_value) else row_value
        if isinstance(row, int) and row >= 0:
            rows.append(row)
    return rows
