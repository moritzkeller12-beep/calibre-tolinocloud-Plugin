import hashlib
import inspect
import json
import os
import re
import traceback
import unicodedata
from collections.abc import Iterable

from .tolino import sanitize_error


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
    if not isinstance(fields, dict):
        return False
    return TOLINO_COLUMN in fields


def tolino_column_active(values, database):
    """ONE rule for the Tolino-ID column: read AND write with exactly one account."""
    if not isinstance(values, dict) or not values.get("use_tolino_column"):
        return False
    if not custom_column_available(database):
        return False
    return len(values.get("accounts") or []) == 1


def update_tolino_ids(database, updates, enabled=True):
    """Persist IDs through Calibre's supported set_field API when enabled."""
    if not enabled or not updates or not custom_column_available(database):
        return 0
    setter = getattr(database, "set_field", None)
    if not callable(setter):
        raise AttributeError("Calibre database does not provide set_field().")
    values = {book_id: str(tolino_id) for book_id, tolino_id in updates
              if book_id is not None and tolino_id}
    if not values:
        return 0
    setter(TOLINO_COLUMN, values)
    return len(values)


def iter_book_ids(database):
    """Yield each existing Calibre book id once, across supported DB APIs."""
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


def _row_book_id(row):
    """Read a book id from Calibre row objects, mappings, or row sequences."""
    book_id = getattr(row, "book_id", None)
    if book_id is not None:
        return book_id
    if isinstance(row, dict):
        return row.get("book_id")
    if isinstance(row, (tuple, list)) and row:
        candidate = next(iter(row), None)
        return candidate if isinstance(candidate, int) and candidate > 0 else None
    return None


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
    """Return canonical format names from Calibre's string or iterable values."""
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


try:
    import shiboken6 as _shiboken
except Exception:
    try:
        import shiboken2 as _shiboken
    except Exception:
        _shiboken = None


def _qt_object_alive(obj):
    """False for Qt wrappers whose C++ object was already deleted."""
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


def unpack_plan_result(result):
    """Validate the three named parts returned by plan_sync."""
    if not isinstance(result, (tuple, list)) or len(result) != 3:
        raise ValueError("Preparation returned an invalid sync plan (expected uploads, removals, state).")
    uploads, removals, current = result
    if not isinstance(uploads, (tuple, list)):
        raise ValueError("Preparation returned invalid upload entries.")
    if not isinstance(removals, (tuple, list)):
        raise ValueError("Preparation returned invalid removal entries.")
    if not isinstance(current, dict):
        raise ValueError("Preparation returned invalid sync state.")
    return uploads, removals, current


def unpack_upload_record(record):
    """Read one upload plan record with a contextual validation error."""
    if not isinstance(record, (tuple, list)) or len(record) != 4:
        raise ValueError("Preparation returned an invalid upload record: %r" % (record,))
    book_id, book_uuid, format_name, old_id = record
    if book_id is None or not book_uuid or not format_name:
        raise ValueError("Preparation returned an incomplete upload record: %r" % (record,))
    return {
        "book_id": book_id,
        "book_uuid": str(book_uuid),
        "format_name": str(format_name),
        "old_id": old_id,
    }


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
    resolver = getattr(database, "format_abspath", None)
    if not callable(resolver):
        raise ValueError("Calibre database does not provide format_abspath().")
    try:
        parameters = inspect.signature(resolver).parameters
    except (TypeError, ValueError):
        parameters = None
    supports_id_flag = parameters is None or "index_is_id" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in (parameters.values() if parameters is not None else ())
    )
    if not supports_id_flag:
        direct_api = getattr(getattr(database, "new_api", None),
                             "format_abspath", None)
        if not callable(direct_api):
            raise ValueError(
                "Calibre format API does not expose an ID-safe resolver.")
    try:
        if supports_id_flag:
            path = resolver(book_id, format_name, index_is_id=True)
        else:
            path = direct_api(book_id, format_name)
    except (IndexError, KeyError, OSError, ValueError) as exc:
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
    try:
        parameters = inspect.signature(database.cover).parameters
    except (TypeError, ValueError):
        parameters = None
    supports_id_flag = parameters is None or "index_is_id" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in (parameters.values() if parameters is not None else ())
    )
    if supports_id_flag:
        cover = database.cover(book_id, as_file=False, index_is_id=True)
    else:
        direct_api = getattr(getattr(database, "new_api", None), "cover", None)
        if callable(direct_api):
            cover = direct_api(book_id, as_file=False)
        else:
            raise ValueError(
                "Calibre cover API does not expose an ID-safe resolver.")
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


def normalize_match_text(value):
    """Normalize human-readable matching text without dropping Unicode letters."""
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    parts = []
    for character in value:
        category = unicodedata.category(character)
        if character.isspace() or category[0] in ("P", "S"):
            parts.append(" ")
        else:
            parts.append(character)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def normalize_title(value):
    """Normalize a title, ignoring only a clearly technical file extension."""
    title = unicodedata.normalize("NFKC", str(value or "")).strip()
    title = re.sub(r"\.(?:epub|pdf|mobi|azw3|azw|fb2|txt|html?)\s*$",
                   "", title, flags=re.IGNORECASE)
    return normalize_match_text(title)


def _normalize_isbn(value):
    return re.sub(r"[^0-9x]+", "", str(value or "").casefold())


def _metadata_text(metadata, *names):
    for name in names:
        value = metadata.get(name, "")
        if value:
            return str(value)
    return ""


def _inventory_records(value):
    """Yield inventory records from dict/list, including edata/ebook wrappers."""
    if isinstance(value, dict):
        found_wrapper = False
        for key in ("edata", "ebook"):
            if key in value:
                found_wrapper = True
                yield from _inventory_records(value[key])
        if not found_wrapper:
            yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _inventory_records(item)


def _nested_inventory_value(item, names):
    """Find a value in a Tolino record without relying on response tuple indexes."""
    if not isinstance(item, dict):
        return ""
    for name in names:
        value = item.get(name)
        if value not in (None, "", [], {}):
            return value
    for container in ("epubMetaData", "deliverable", "metadata", "ebook", "edata"):
        nested = item.get(container)
        value = _nested_inventory_value(nested, names)
        if value not in (None, "", [], {}):
            return value
    return ""


def _inventory_author(value):
    if isinstance(value, dict):
        return str(value.get("name") or value.get("displayName") or "").strip()
    if isinstance(value, (list, tuple)):
        names = [_inventory_author(item) for item in value]
        return ", ".join(name for name in names if name)
    return str(value or "").strip()


def normalize_inventory_item(item):
    """Return display and matching fields for one Tolino inventory record."""
    title = _nested_inventory_value(
        item, ("title", "bookTitle", "name")
    )
    author = _nested_inventory_value(
        item, ("authors", "author", "creator")
    )
    isbn = _nested_inventory_value(
        item, ("isbn", "ISBN", "isbn13", "isbn10", "identifier")
    )
    remote_id = _nested_inventory_value(
        item, ("deliverableId", "deliverable_id", "id")
    )
    return {
        "title": str(title or "").strip(),
        "authors": _inventory_author(author),
        "isbn": str(isbn or "").strip(),
        "tolino_id": str(remote_id or "").strip(),
    }


def _file_changed(metadata, state_entry, format_name):
    """True, wenn sich die Buchdatei seit dem letzten Sync geändert hat.

    Nur aussagekräftig, wenn beide Seiten einen Content-Fingerabdruck
    haben; sonst False – die Oberfläche schlägt dann den Metadaten-Abgleich
    vor, der ohne erneuten Upload auskommt.
    """
    if not format_name:
        return False
    current = content_fingerprint(metadata, format_name)
    previous = str(state_entry.get("content_fp") or "")
    return bool(current and previous and current != previous)


def _row_explanation(status, match_reason, file_changed):
    """Klartext-Hinweis für eine Bestandszeile."""
    if match_reason == "duplicate_title":
        return "Titel mehrfach in der Cloud – bitte prüfen."
    if status == "identical":
        if match_reason == "title":
            return "Titel stimmt mit der Cloud überein."
        return "In beiden vorhanden, keine Unterschiede gefunden."
    if status == "new_in_calibre":
        return "Noch nicht in der Cloud – kann hochgeladen werden."
    if file_changed:
        return "Buchdatei unterscheidet sich – Hochladen lädt die neue Datei."
    return "Nur Metadaten unterscheiden sich – kein erneuter Upload nötig."


def compare_inventory(metadata_by_id, state, inventory, preferred_formats=(),
                      comparison_fields=("authors", "title", "isbn"),
                      use_metadata_ids=True, read_ids=()):
    """Return deterministic local/remote rows for the pre-sync confirmation view.

    `read_ids` sind die Tolino-IDs, die im Konto schon als gelesen markiert
    sind (0.15.0). Sie belegen die Spalte «Gelesen» vor: was in der Cloud
    gelesen ist, ist angehakt – und muss nicht erneut geschrieben werden.
    """
    if not isinstance(metadata_by_id, dict) or not isinstance(state, dict):
        raise ValueError("Preparation requires metadata and sync state mappings.")
    preferred_formats = normalize_formats(preferred_formats)
    read_ids = {str(item) for item in (read_ids or ()) if str(item or "").strip()}
    remote_rows = []
    for item in _inventory_records(inventory):
        normalized = normalize_inventory_item(item)
        if not normalized["tolino_id"]:
            continue
        remote_rows.append({
            "book_id": None,
            "uuid": _nested_inventory_value(item, ("uuid", "calibreUuid", "calibre_uuid")),
            **normalized,
            "status": "only_tolino",
            "selected": False,
            "file_changed": False,
            "read_in_cloud": str(normalized["tolino_id"]) in read_ids,
            "read": str(normalized["tolino_id"]) in read_ids,
            "explanation": "Nur in der Cloud – kann heruntergeladen werden.",
            "normalized_title": normalize_title(normalized["title"]),
        })
    title_counts = {}
    # Suchindexe: die Zuordnung suchte früher fuer JEDES Buch die ganze
    # Cloud-Liste erneut ab -- quadratisch mit der Bibliotheksgroesse.
    # Die Indexe enthalten dieselben Zeilen in derselben Reihenfolge.
    by_tolino_id = {}
    by_uuid = {}
    by_isbn = {}
    by_title = {}
    for index, row in enumerate(remote_rows):
        if row["tolino_id"]:
            by_tolino_id.setdefault(row["tolino_id"], []).append(index)
        row_uuid = str(row.get("uuid") or "")
        if row_uuid:
            by_uuid.setdefault(row_uuid, []).append(index)
        row_isbn = _normalize_isbn(row["isbn"])
        if row_isbn:
            by_isbn.setdefault(row_isbn, []).append(index)
        if row["normalized_title"]:
            title_counts[row["normalized_title"]] = (
                title_counts.get(row["normalized_title"], 0) + 1
            )
            by_title.setdefault(row["normalized_title"], []).append(index)
    matched = set()
    rows = []
    for book_id, metadata in metadata_by_id.items():
        if not isinstance(metadata, dict):
            raise ValueError("Preparation returned invalid metadata for book %r." % (book_id,))
        book_uuid = str(metadata.get("uuid") or "")
        title = _metadata_text(metadata, "title")
        authors = _metadata_text(metadata, "authors", "author")
        isbn = _metadata_text(metadata, "isbn", "identifiers")
        old = state.get(book_uuid, {})
        stored_id = str(old.get("tolino_id") or
                        (metadata_tolino_id(metadata) if use_metadata_ids else "") or "")
        candidates = []
        if stored_id:
            candidates = list(by_tolino_id.get(stored_id, ()))
        if not candidates and book_uuid:
            candidates = list(by_uuid.get(book_uuid, ()))
        if not candidates and "isbn" in comparison_fields and isbn:
            normalized_isbn = _normalize_isbn(isbn)
            candidates = list(by_isbn.get(normalized_isbn, ())) if normalized_isbn else []
        title_key = normalize_title(title)
        matched_by_title = not candidates and bool(title_key)
        if matched_by_title:
            candidates = list(by_title.get(title_key, ()))
        remote_index = next((i for i in candidates if i not in matched), None)
        match_reason = ""
        selected_format = next(
            (fmt for fmt in preferred_formats
             if str(fmt).upper() in {x.upper() for x in normalize_formats(metadata.get("formats"))}),
            None,
        )
        file_changed = False
        if remote_index is None:
            status = "new_in_calibre"
            tolino_id = stored_id
        else:
            matched.add(remote_index)
            remote = remote_rows[remote_index]
            tolino_id = remote["tolino_id"]
            current_fp = fingerprint(metadata, selected_format) if selected_format else ""
            values = ((title, "title"), (authors, "authors"), (isbn, "isbn"))
            same_fields = bool(comparison_fields) and all(
                not value or (
                    _normalize_isbn(value) == _normalize_isbn(remote[field])
                    if field == "isbn" else
                    normalize_match_text(value) == normalize_match_text(remote[field])
                )
                for value, field in values if field in comparison_fields
            )
            title_match = bool(matched_by_title and title_key and
                               normalize_title(remote["title"]) == title_key)
            status = "identical" if title_match or (
                old.get("fingerprint") and old.get("fingerprint") == current_fp
            ) or same_fields else "changed"
            # Nur die Buchdatei kann nicht per /meta nachgezogen werden --
            # die Zeile soll das sagen können (0.13.0).
            file_changed = _file_changed(metadata, old, selected_format)
            match_reason = (
                "title" if title_match else
                "stored_id_or_uuid" if stored_id or book_uuid == remote["uuid"] else
                "isbn_or_metadata"
            )
            duplicate_title = title_counts.get(title_key, 0) > 1
            if duplicate_title and title_match:
                match_reason = "duplicate_title"
            state.setdefault(book_uuid, {})["tolino_id"] = tolino_id
        rows.append({
            "book_id": book_id,
            "uuid": book_uuid,
            "title": title,
            "authors": authors,
            "isbn": isbn,
            "tolino_id": tolino_id,
            "status": status,
            "selected": status == "new_in_calibre",
            "match_reason": match_reason,
            "duplicate_count": title_counts.get(title_key, 0),
            "file_changed": file_changed,
            "read_in_cloud": bool(tolino_id) and str(tolino_id) in read_ids,
            "read": bool(tolino_id) and str(tolino_id) in read_ids,
            "explanation": _row_explanation(status, match_reason,
                                             file_changed),
        })
    for i, row in enumerate(remote_rows):
        if i in matched:
            continue
        if not row["title"]:
            entry = dict(row, status="not_matchable",
                         explanation=(
                             "Tolino-Datensatz ohne Titel – nicht zuordenbar."
                         ))
        elif title_counts.get(row["normalized_title"], 0) > 1:
            entry = dict(row, status="duplicate_tolino",
                         explanation="Titel mehrfach in der Cloud – nicht automatisch hochladen.")
        else:
            entry = row
        rows.append(entry)
    return rows


def selected_book_ids(comparison_rows):
    return {
        row["book_id"] for row in comparison_rows
        if row.get("selected") and row.get("book_id") is not None
    }


def sort_comparison_rows(comparison_rows):
    """Bestandsvergleich sortieren: erst ausgewählte Aktionen, dann Titel, Autoren.

    Zeilen mit einer Aktion ("upload"/"metadata") stehen oben. Ältere Zeilen
    ohne diese Felder fallen auf das frühere "selected" zurück.
    """
    def sort_key(row):
        upload = row.get("upload")
        if upload is None:
            upload = bool(row.get("selected"))
        return (
            0 if upload else 1,
            0 if row.get("metadata") else 1,
            str(row.get("title") or "").casefold(),
            str(row.get("authors") or "").casefold(),
            str(row.get("isbn") or ""),
        )
    return sorted(comparison_rows, key=sort_key)


def series_map_from_metadata(metadata_by_id):
    """uuid -> Serienname fuer alle Buecher mit Serie (0.12.0).

    Reine Zuordnung ohne Cloud-Zugriff: die Sammlungen der Tolino Cloud
    entstehen erst, wenn der Nutzer "Serien in der Cloud anlegen"
    eingeschaltet hat.
    """
    series_map = {}
    if not isinstance(metadata_by_id, dict):
        return series_map
    for metadata in metadata_by_id.values():
        if not isinstance(metadata, dict):
            continue
        book_uuid = str(metadata.get("uuid") or "")
        series = str(metadata.get("series") or "").strip()
        if book_uuid and series:
            series_map[book_uuid] = series
    return series_map


def recorded_collections(entry):
    """Serien, die für diese Cloud-Kopie schon zugeordnet wurden.

    Wird von `remember_collection` gefüllt und macht den Serien-Abgleich
    dauerhaft: der nächste Sync plant nur noch, was noch nicht geschrieben
    wurde, und fragt die Cloud überhaupt nicht mehr danach.
    """
    if not isinstance(entry, dict):
        return set()
    recorded = entry.get("collections")
    if not isinstance(recorded, list):
        return set()
    return {str(name).strip() for name in recorded if str(name or "").strip()}


def remember_collection(state, book_uuid, series):
    """Nach erfolgreichem Schreiben: Zuordnung im Zustand merken (0.14.0)."""
    if not isinstance(state, dict) or not str(book_uuid or "").strip():
        return False
    series = str(series or "").strip()
    if not series:
        return False
    entry = state.get(book_uuid)
    if not isinstance(entry, dict):
        entry = {}
        state[book_uuid] = entry
    recorded = entry.get("collections")
    if not isinstance(recorded, list):
        recorded = []
        entry["collections"] = recorded
    if series in recorded:
        return False
    recorded.append(series)
    return True


def plan_collection_updates(series_map, state):
    """Offene Serien-Zuordnungen als (Buch-UUID, Tolino-ID, Serienname).

    Geplant wird nur, was noch nicht geschrieben wurde: bereits zugeordnete
    Serien stehen als `collections` am Buch (siehe `remember_collection`) und
    fallen hier heraus. Ohne offene Zuordnungen ist das Ergebnis leer – dann
    liest der Sync die Cloud gar nicht erst. Ohne Serien (oder ohne bekannte
    Tolino-ID) ebenfalls leer: Ordner entstehen nie automatisch.
    """
    if not isinstance(series_map, dict) or not isinstance(state, dict):
        return []
    pending = set()
    for book_uuid, series in series_map.items():
        series = str(series or "").strip()
        if not book_uuid or not series:
            continue
        entry = state.get(book_uuid)
        if not isinstance(entry, dict):
            continue
        tolino_id = str(entry.get("tolino_id") or "").strip()
        if not tolino_id:
            continue
        if series in recorded_collections(entry):
            continue
        pending.add((str(book_uuid), tolino_id, series))
    return sorted(pending, key=lambda item: (item[1], item[2], item[0]))


def fingerprint(metadata, format_name):
    value = "%s|%s|%s|%s" % (
        metadata.get("uuid", ""),
        metadata.get("last_modified", ""),
        format_name,
        metadata.get("title", ""),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def metadata_fingerprint(metadata):
    """Hash of the fields pushed in place to Tolino instead of uploaded."""
    value = "%s|%s|%s|%s" % (
        metadata.get("title", ""),
        metadata.get("authors", ""),
        metadata.get("isbn", ""),
        metadata.get("series", ""),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def content_fingerprint(metadata, format_name):
    """Hash of the format FILE itself (uuid|format|size:mtime); '' when the
    signature was never collected – an unknown file must never be mistaken
    for an unchanged one, so '' blocks in-place metadata updates."""
    signatures = metadata.get("format_signatures")
    signature = (str(signatures.get(format_name) or "")
                 if isinstance(signatures, dict) else "")
    if not signature:
        return ""
    value = "%s|%s|%s" % (metadata.get("uuid", ""), format_name, signature)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def metadata_update_fields(metadata):
    """Tolino /meta payload fields for an in-place metadata update."""
    fields = {}
    title = str(metadata.get("title") or "").strip()
    if title:
        fields["title"] = title
    authors = metadata.get("authors")
    if isinstance(authors, (list, tuple, set)):
        authors = ", ".join(str(name) for name in authors)
    authors = str(authors or "").strip()
    if authors:
        fields["author"] = authors
    isbn = str(metadata.get("isbn") or "").strip()
    if isbn:
        fields["isbn"] = isbn
    return fields


def unpack_meta_update_record(record):
    """Read one in-place metadata update record with a contextual error."""
    if not isinstance(record, (tuple, list)) or len(record) != 4:
        raise ValueError("Preparation returned an invalid metadata update record: %r" % (record,))
    book_id, book_uuid, tolino_id, fields = record
    if (book_id is None or not book_uuid or not tolino_id
            or not isinstance(fields, dict) or not fields):
        raise ValueError("Preparation returned an incomplete metadata update record: %r" % (record,))
    return {
        "book_id": book_id,
        "book_uuid": str(book_uuid),
        "tolino_id": str(tolino_id),
        "fields": dict(fields),
    }


def plan_metadata_updates(metadata_by_id, state, uploads, preferred_formats=(),
                          only_book_ids=None):
    """In-place Metadaten-Updates (/meta) für die ausgewählten Bücher.

    Ein Buch kommt in Frage, wenn es bereits eine Cloud-Kopie hat, ein
    bevorzugtes Format vorliegt und sich Titel/Autor(en)/ISBN/Serie seit
    dem letzten Sync geändert haben (oder der Zustand noch keinen
    Metadaten-Fingerabdruck kennt). `only_book_ids` grenzt auf die vom
    Nutzer ausgewählten Bücher ein; ohne Angabe werden alle passenden Bücher
    berücksichtigt. Bücher, die ohnehin hochgeladen werden, werden NIE
    zusätzlich gepatcht – der Upload trägt die Metadaten mit.

    Returns (kept_uploads, meta_updates).
    """
    if not isinstance(metadata_by_id, dict) or not isinstance(state, dict):
        raise ValueError("Preparation requires metadata and sync state mappings.")
    preferred_formats = normalize_formats(preferred_formats)
    upload_uuids = {str(unpack_upload_record(record)["book_uuid"])
                    for record in uploads}
    requested = None if only_book_ids is None else {str(item) for item in only_book_ids}
    updates = []
    for book_id, metadata in metadata_by_id.items():
        if not isinstance(metadata, dict):
            raise ValueError("Preparation returned invalid metadata for book %r." % (book_id,))
        if requested is not None and str(book_id) not in requested:
            continue
        book_uuid = str(metadata.get("uuid") or "")
        if not book_uuid or book_uuid in upload_uuids:
            continue
        old = state.get(book_uuid) or {}
        tolino_id = str(old.get("tolino_id") or "")
        if not tolino_id:
            continue
        available = set(normalize_formats(metadata.get("formats")))
        selected_format = next((f for f in preferred_formats if f in available), None)
        if not selected_format:
            continue
        fingerprint = old.get("meta_fp")
        if fingerprint and fingerprint == metadata_fingerprint(metadata):
            continue
        fields = metadata_update_fields(metadata)
        if not fields:
            continue
        updates.append((book_id, book_uuid, tolino_id, fields))
    return list(uploads), updates


def revert_unsynced_fingerprints(current, previous, synced_uuids):
    """Fingerabdrücke von Büchern ohne Aktion auf den alten Stand zurück.

    `plan_sync` schreibt für JEDES Buch den neuen Stand in den Zustand --
    auch für Bücher, die der Nutzer gar nicht ausgewählt hat. Ohne diese
    Korrektur hielte der nächste Vergleich die Cloud für aktuell, obwohl
    dort weiterhin die alten Daten liegen (und ein späteres "Metadaten"
    wäre ein stiller Leerlauf).
    """
    synced = {str(book_uuid) for book_uuid in synced_uuids}
    for book_uuid, entry in current.items():
        if str(book_uuid) in synced or not isinstance(entry, dict):
            continue
        old = previous.get(book_uuid)
        if not isinstance(old, dict):
            continue
        for key in ("fingerprint", "content_fp", "meta_fp"):
            if key in old:
                entry[key] = old[key]
            else:
                entry.pop(key, None)
    return current


def plan_sync(metadata_by_id, state, preferred_formats, deletions=False,
              upload_book_ids=None):
    if not isinstance(metadata_by_id, dict) or not isinstance(state, dict):
        raise ValueError("Preparation requires metadata and sync state mappings.")
    preferred_formats = normalize_formats(preferred_formats)
    current = {}
    uploads = []
    for book_id, metadata in metadata_by_id.items():
        if not isinstance(metadata, dict):
            raise ValueError("Preparation returned invalid metadata for book %r." % (book_id,))
        book_uuid = metadata.get("uuid")
        if not book_uuid:
            continue
        available = set(normalize_formats(metadata.get("formats")))
        selected = next((f for f in preferred_formats if f in available), None)
        if not selected:
            if upload_book_ids is not None and book_id in upload_book_ids:
                title = metadata.get("title") or "ohne Titel"
                requested = ", ".join(preferred_formats) or "kein bevorzugtes Format"
                raise ValueError(
                    "Buch %r (%s): kein bevorzugtes Format gefunden (gesucht: %s)." %
                    (book_id, title, requested)
                )
            continue
        fp = fingerprint(metadata, selected)
        old = state.get(book_uuid, {})
        entry = {"tolino_id": old.get("tolino_id"),
                 "fingerprint": fp, "calibre_id": book_id,
                 "content_fp": content_fingerprint(metadata, selected),
                 "meta_fp": metadata_fingerprint(metadata)}
        # Gemerkte Serien-Zuordnungen gehören zum Buch und überleben den
        # Neuaufbau des Zustands (0.14.0).
        recorded = old.get("collections")
        if isinstance(recorded, list) and recorded:
            entry["collections"] = list(recorded)
        current[book_uuid] = entry
        if upload_book_ids is not None:
            should_upload = book_id in upload_book_ids
        else:
            should_upload = not old.get("tolino_id") or old.get("fingerprint") != fp
        if should_upload:
            uploads.append((book_id, book_uuid, selected, old.get("tolino_id")))
    removals = []
    if deletions:
        removals = [(book_uuid, item["tolino_id"]) for book_uuid, item in state.items()
                    if book_uuid not in current and item.get("tolino_id")]
    return uploads, removals, current


def load_state(raw):
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def sync_summary(upload_count, delete_count, error_count=0):
    """Return stable UI text data without depending on Qt or Calibre."""
    return {
        "uploads": int(upload_count),
        "deletions": int(delete_count),
        "errors": int(error_count),
        "total": int(upload_count) + int(delete_count),
    }


_SECRET_KEYS = {
    "access_token", "authorization", "password", "refresh", "refresh_token",
    "secret", "token", "t_auth_token",
}


def redact_sensitive(value):
    """Return a printable copy with credentials and authorization data removed."""
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if (
                str(key).casefold() in _SECRET_KEYS or
                "token" in str(key).casefold() or
                "password" in str(key).casefold()
            ) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, bytes):
        return "[%d bytes]" % len(value)
    return sanitize_error(value)


def format_diagnostic_report(results):
    """Format diagnostic step dictionaries as copyable, stable text."""
    lines = ["Tolino Cloud Sync Diagnose", "===========================", ""]
    for result in results:
        lines.append("[%s] %s" % (result.get("status", "unknown").upper(),
                                  result.get("step", "unnamed")))
        if "value" in result:
            value = redact_sensitive(result["value"])
            lines.append(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    default=str))
        if result.get("error_type"):
            lines.append("Fehlertyp: %s" % result["error_type"])
            lines.append("Fehlermeldung: %s" %
                         sanitize_error(result.get("error_message", "")))
            lines.append("Traceback:")
            lines.extend(sanitize_error(result.get("traceback", "")).rstrip().splitlines())
        lines.append("")
    return "\n".join(lines)


def diagnose_preparation(database, preferred_formats=(), upload_covers=False, state=None):
    """Inspect local preparation inputs without logging in or contacting Tolino."""
    results = []

    def step(name, action):
        try:
            results.append({"step": name, "status": "ok", "value": action()})
        except Exception as exc:
            results.append({
                "step": name,
                "status": "error",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "traceback": traceback.format_exc(),
            })
        return results[-1]

    step("Calibre-Version", lambda: _calibre_version())
    step("Datenbankobjekt-Typ", lambda: {
        "type": type(database).__name__,
        "module": type(database).__module__,
    })
    ids_result = step("Erkannte Buch-ID-Anzahl", lambda: _diagnostic_ids(database))
    book_ids = ids_result.get("value", {}).get("ids", []) if ids_result.get("status") == "ok" else []

    metadata = {}
    for book_id in book_ids:
        result = step("Metadaten Buch %s" % book_id,
                      lambda book_id=book_id: _diagnostic_metadata(database, book_id))
        if result.get("status") == "ok":
            metadata[book_id] = result["value"]["metadata"]

    step("Metadaten-Schlüssel/-Typen", lambda: _metadata_shapes(metadata))
    step("Format-Rückgaben", lambda: _diagnostic_formats(database, metadata))
    if upload_covers:
        step("Titelbild-Typ/-Grösse",
             lambda: _diagnostic_covers(database, book_ids))
    state_result = step("Lokaler Sync-Status", lambda: state if isinstance(state, dict) else {})
    state = state_result.get("value", {}) if state_result.get("status") == "ok" else {}
    step("plan_sync-Ergebnisform", lambda: _diagnostic_plan(metadata, state, preferred_formats))
    return results


def _calibre_version():
    try:
        import calibre
        return getattr(calibre, "__version__", "unbekannt")
    except ImportError:
        return "Calibre-Modul nicht verfügbar (Diagnose ausserhalb Calibre)"


def _diagnostic_ids(database):
    ids = list(iter_book_ids(database))
    return {"count": len(ids), "ids": ids[:20], "truncated": len(ids) > 20}


def _diagnostic_metadata(database, book_id):
    item = metadata_by_id(database, book_id)
    metadata = {}
    for name in ("uuid", "title", "authors", "author", "isbn", "identifiers",
                 "formats", "last_modified"):
        value = getattr(item, name, None)
        if value is None and hasattr(item, "get"):
            value = item.get(name)
        metadata[name] = value
    return {
        "type": type(item).__name__,
        "keys": sorted(str(key) for key in metadata if metadata[key] is not None),
        "types": {key: type(value).__name__ for key, value in metadata.items()
                  if value is not None},
        "metadata": metadata,
    }


def _metadata_shapes(metadata):
    return {
        str(book_id): {
            "keys": sorted(value),
            "types": {key: type(item).__name__ for key, item in value.items()},
        }
        for book_id, value in metadata.items()
    }


def _diagnostic_formats(database, metadata):
    values = {}
    for book_id, item in metadata.items():
        formats = normalize_formats(item.get("formats"))
        values[str(book_id)] = {
            "metadata_formats": formats,
            "paths": {fmt: _diagnostic_path(database, book_id, fmt) for fmt in formats},
        }
    return values


def _diagnostic_path(database, book_id, format_name):
    try:
        path = safe_format_path(database, book_id, format_name)
        return {
            "status": "found" if os.path.isfile(path) else "missing",
            "path": redact_sensitive(path),
        }
    except Exception as exc:
        return {
            "status": "missing",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def _diagnostic_covers(database, book_ids):
    result = {}
    for book_id in book_ids[:20]:
        value = database.cover(book_id, as_file=False)
        data = cover_bytes(database, book_id)
        result[str(book_id)] = {
            "returned_type": type(value).__name__,
            "size": len(data) if data is not None else None,
        }
    return result


def _diagnostic_plan(metadata, state, preferred_formats):
    plan = plan_sync(metadata, state, preferred_formats)
    uploads, removals, current = unpack_plan_result(plan)
    return {
        "type": type(plan).__name__,
        "length": len(plan),
        "parts": [type(part).__name__ for part in (uploads, removals, current)],
        "upload_record_lengths": [
            len(record) if isinstance(record, (tuple, list)) else None
            for record in uploads
        ],
    }
