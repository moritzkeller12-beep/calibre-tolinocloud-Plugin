"""Adapters for Calibre library and Qt APIs used by the plugin."""
import inspect
import os
import re
from collections.abc import Iterable

try:
    import shiboken6 as _shiboken
except Exception:
    try:
        import shiboken2 as _shiboken
    except Exception:
        _shiboken = None


TOLINO_COLUMN = "#tolino_id"


def metadata_tolino_id(metadata):
    """Read the custom column from Calibre metadata without requiring Calibre."""
    if not isinstance(metadata, dict):
        value = getattr(metadata, "get", lambda *_args: "")(TOLINO_COLUMN, "")
        return str(value or "").strip()
    for key in (TOLINO_COLUMN, "tolino_id"):
        value = metadata.get(key)
        if value:
            return str(value).strip()
    return ""


def custom_column_available(database):
    """Check Calibre's public field metadata for the configured custom column."""
    fields = getattr(database, "field_metadata", None)
    if callable(fields):
        fields = fields()
    if fields is None:
        return False
    try:
        return TOLINO_COLUMN in fields
    except (TypeError, AttributeError):
        return False


def tolino_column_active(values, database):
    """Read and write the ID column only when enabled for one account."""
    if not isinstance(values, dict) or not values.get("use_tolino_column"):
        return False
    if not custom_column_available(database):
        return False
    return len(values.get("accounts") or []) == 1


def update_tolino_ids(database, updates, enabled=True):
    """Persist IDs through Calibre's supported set_field API when enabled."""
    if not enabled or not updates or not custom_column_available(database):
        return 0
    api = getattr(database, "new_api", database)
    setter = getattr(api, "set_field", None)
    if not callable(setter):
        raise AttributeError("Calibre database API does not provide set_field().")
    values = {book_id: str(tolino_id or "") for book_id, tolino_id in updates
              if book_id is not None}
    data = getattr(database, "data", None)
    if data is None:
        data = api
    has_id = getattr(data, "has_id", None)
    if callable(has_id):
        values = {book_id: value for book_id, value in values.items()
                  if has_id(book_id)}
    if not values:
        return 0
    setter(TOLINO_COLUMN, values)
    return len(values)


def _row_book_id(row):
    """Read a book id from Calibre row objects, mappings, or row sequences."""
    book_id = getattr(row, "book_id", None)
    if book_id is not None:
        return book_id
    if isinstance(row, dict):
        return row.get("book_id")
    if isinstance(row, (tuple, list)) and row:
        candidate = row[0]
        return candidate if isinstance(candidate, int) and candidate > 0 else None
    return None


def _unique_ids(book_ids):
    seen = set()
    for book_id in book_ids:
        try:
            duplicate = book_id in seen
        except TypeError:
            continue
        if not duplicate:
            seen.add(book_id)
            yield book_id


def iter_book_ids(database):
    """Yield each existing Calibre book id once across supported DB APIs."""
    data = getattr(database, "data", None)
    iterall = getattr(data, "iterall", None)
    if callable(iterall):
        rows = iterall() or ()
        book_ids = []
        for row in rows:
            book_id = _row_book_id(row)
            if book_id is not None:
                book_ids.append(book_id)
        yield from _unique_ids(book_ids)
        return

    iterallids = getattr(data, "iterallids", None)
    if callable(iterallids):
        yield from _unique_ids(iterallids() or ())
        return

    all_book_ids = getattr(database, "all_book_ids", None)
    if callable(all_book_ids):
        yield from _unique_ids(all_book_ids() or ())
        return

    all_ids = getattr(database, "all_ids", None)
    if callable(all_ids):
        yield from _unique_ids(all_ids() or ())


def database_uuid_values(database, book_id):
    """Yield raw UUID values from Calibre's field, backend, and public APIs."""
    api = getattr(database, "new_api", None) or database
    field_for = getattr(api, "field_for", None)
    if callable(field_for):
        try:
            yield "field_for", field_for("uuid", book_id)
        except Exception:
            pass
    backend = getattr(api, "backend", None)
    execute = getattr(backend, "execute", None)
    if callable(execute):
        try:
            rows = execute("SELECT uuid FROM books WHERE id=?", (int(book_id),))
            row = next(iter(rows), None)
            if row:
                yield "sql", row[0]
        except Exception:
            pass
    getter = getattr(database, "uuid", None)
    if callable(getter):
        try:
            yield "db.uuid", getter(book_id, index_is_id=True)
        except Exception:
            pass


def metadata_by_id(database, book_id):
    """Read metadata by a real Calibre id, never by a filtered-view index."""
    has_id = getattr(database, "has_id", None)
    if callable(has_id) and not has_id(book_id):
        raise ValueError("Calibre database has no book with id %r." % (book_id,))
    getter = getattr(database, "get_metadata", None)
    if not callable(getter):
        raise AttributeError("Calibre database does not provide get_metadata().")
    return getter(book_id, index_is_id=True)


def normalize_formats(value):
    """Return canonical format names from Calibre string or iterable values."""
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (bytes, bytearray)):
        values = [os.fsdecode(value)]
    elif isinstance(value, Iterable):
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


def _id_safe_call(database, method_name, *args, **kwargs):
    """Call a legacy Calibre DB method with ID semantics, or its new API."""
    method = getattr(database, method_name, None)
    if not callable(method):
        raise ValueError("Calibre database does not provide %s()." % method_name)
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        parameters = None
    supports_flag = parameters is None or "index_is_id" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in (parameters.values() if parameters is not None else ())
    )
    if supports_flag:
        return method(*args, index_is_id=True, **kwargs)
    direct_api = getattr(getattr(database, "new_api", None), method_name, None)
    if not callable(direct_api):
        raise ValueError("Calibre %s API does not expose an ID-safe resolver."
                         % method_name)
    return direct_api(*args, **kwargs)


def safe_format_path(database, book_id, format_name):
    """Resolve a real Calibre id without falling back to view-index semantics."""
    if isinstance(book_id, bool) or not isinstance(book_id, int) or book_id <= 0:
        raise ValueError("Invalid Calibre book id: %r" % (book_id,))
    if not isinstance(format_name, str):
        raise ValueError("Invalid format name: %r" % (format_name,))
    format_name = format_name.strip().lstrip(".").upper()
    if not format_name or not re.match(r"^[A-Z0-9][A-Z0-9._-]*$", format_name):
        raise ValueError("Invalid format name: %r" % (format_name,))
    has_id = getattr(database, "has_id", None)
    if callable(has_id) and not has_id(book_id):
        raise ValueError("Calibre database has no book with id %r." % (book_id,))
    try:
        path = _id_safe_call(database, "format_abspath", book_id, format_name)
    except ValueError as exc:
        if "ID-safe resolver" in str(exc):
            raise
        raise ValueError("Calibre returned no path for book %r format %s" %
                         (book_id, format_name)) from exc
    except (IndexError, KeyError, OSError) as exc:
        raise ValueError("Calibre returned no path for book %r format %s" %
                         (book_id, format_name)) from exc
    if isinstance(path, (tuple, list)):
        path = next((item for item in path if isinstance(item, (str, bytes, os.PathLike))), None)
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not isinstance(path, (str, os.PathLike)) or not os.fspath(path):
        raise ValueError("Calibre returned no path for book %r format %s" %
                         (book_id, format_name))
    return os.fspath(path)


def cover_bytes(database, book_id):
    """Normalize cover() results from bytes, paths, or wrapped Calibre values."""
    if isinstance(book_id, bool) or not isinstance(book_id, int) or book_id <= 0:
        raise ValueError("Invalid Calibre book id: %r" % (book_id,))
    has_id = getattr(database, "has_id", None)
    if callable(has_id) and not has_id(book_id):
        raise ValueError("Calibre database has no book with id %r." % (book_id,))
    cover = _id_safe_call(database, "cover", book_id, as_file=False)
    if isinstance(cover, (bytes, bytearray, memoryview)):
        return bytes(cover)
    if isinstance(cover, (str, os.PathLike)):
        path = os.fspath(cover)
        if os.path.isfile(path):
            with open(path, "rb") as cover_file:
                return cover_file.read()
        return None
    if isinstance(cover, (tuple, list)):
        for value in cover:
            if isinstance(value, (bytes, bytearray, memoryview)):
                return bytes(value)
            if isinstance(value, (str, os.PathLike)):
                path = os.fspath(value)
                if os.path.isfile(path):
                    with open(path, "rb") as cover_file:
                        return cover_file.read()
    return None


def image_suffix(data):
    """Return a recognized image suffix, or '' for unknown cover bytes."""
    if not isinstance(data, (bytes, bytearray, memoryview)) or not data:
        return ""
    raw = bytes(data)
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return ""


def _qt_object_alive(obj):
    if obj is None:
        return False
    if _shiboken is not None:
        try:
            return bool(_shiboken.isValid(obj))
        except Exception:
            return False
    return True


def selected_table_rows(table):
    """Return valid selected Qt row numbers without assuming QModelIndex shape."""
    if not _qt_object_alive(table):
        return []
    selection_model = getattr(table, "selectionModel", None)
    selected = selection_model().selectedRows() if callable(selection_model) else ()
    rows = []
    for index in selected or ():
        row_value = getattr(index, "row", None)
        row = row_value() if callable(row_value) else row_value
        if isinstance(row, int) and row >= 0:
            rows.append(row)
    return rows
