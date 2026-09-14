import hashlib
import json
import os
import re
import traceback
import unicodedata
from collections.abc import Iterable


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


def selected_table_rows(table):
    """Return valid selected Qt row numbers without assuming QModelIndex shape."""
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
    """Call format_abspath without assuming a particular return container."""
    path = database.format_abspath(book_id, format_name)
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
    cover = database.cover(book_id, as_file=False)
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
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^0-9a-z]+", " ", value).strip()


def _normalize_isbn(value):
    return re.sub(r"[^0-9x]+", "", str(value or "").casefold())


def _metadata_text(metadata, *names):
    for name in names:
        value = metadata.get(name, "")
        if value:
            return str(value)
    return ""


def _inventory_value(item, *names):
    if not isinstance(item, dict):
        return ""
    for name in names:
        value = item.get(name)
        if value:
            return value
    return ""


def compare_inventory(metadata_by_id, state, inventory, preferred_formats=(),
                      comparison_fields=("authors", "title", "isbn")):
    """Return deterministic local/remote rows for the pre-sync confirmation view."""
    if not isinstance(metadata_by_id, dict) or not isinstance(state, dict):
        raise ValueError("Preparation requires metadata and sync state mappings.")
    preferred_formats = normalize_formats(preferred_formats)
    remote_rows = []
    for item in inventory or ():
        remote_id = _inventory_value(item, "id", "deliverableId", "deliverable_id")
        if not remote_id:
            continue
        remote_rows.append({
            "book_id": None,
            "uuid": _inventory_value(item, "uuid", "calibreUuid", "calibre_uuid"),
            "title": _inventory_value(item, "title", "bookTitle", "name"),
            "authors": _inventory_value(item, "authors", "author", "creator"),
            "isbn": _inventory_value(item, "isbn", "ISBN", "isbn13"),
            "tolino_id": str(remote_id),
            "status": "only_tolino",
            "selected": False,
        })
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
        stored_id = str(old.get("tolino_id") or "")
        candidates = []
        if stored_id:
            candidates = [i for i, row in enumerate(remote_rows)
                          if row["tolino_id"] == stored_id]
        if not candidates and book_uuid:
            candidates = [i for i, row in enumerate(remote_rows)
                          if str(row["uuid"]) == book_uuid]
        if not candidates and "isbn" in comparison_fields and isbn:
            normalized_isbn = _normalize_isbn(isbn)
            candidates = [i for i, row in enumerate(remote_rows)
                          if normalized_isbn and _normalize_isbn(row["isbn"]) == normalized_isbn]
        if not candidates and {"authors", "title"} <= set(comparison_fields) and title and authors:
            key = (normalize_match_text(authors), normalize_match_text(title))
            candidates = [i for i, row in enumerate(remote_rows)
                          if (normalize_match_text(row["authors"]),
                              normalize_match_text(row["title"])) == key]
        remote_index = next((i for i in candidates if i not in matched), None)
        selected_format = next(
            (fmt for fmt in preferred_formats
             if str(fmt).upper() in {x.upper() for x in normalize_formats(metadata.get("formats"))}),
            None,
        )
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
            status = "identical" if (
                old.get("fingerprint") and old.get("fingerprint") == current_fp
            ) or same_fields else "changed"
        rows.append({
            "book_id": book_id,
            "uuid": book_uuid,
            "title": title,
            "authors": authors,
            "isbn": isbn,
            "tolino_id": tolino_id,
            "status": status,
            "selected": status in ("new_in_calibre", "changed"),
        })
    rows.extend(row for i, row in enumerate(remote_rows) if i not in matched)
    return rows


def selected_book_ids(comparison_rows):
    return {
        row["book_id"] for row in comparison_rows
        if row.get("selected") and row.get("book_id") is not None
    }


def fingerprint(metadata, format_name):
    value = "%s|%s|%s|%s" % (
        metadata.get("uuid", ""),
        metadata.get("last_modified", ""),
        format_name,
        metadata.get("title", ""),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
        current[book_uuid] = {"tolino_id": state.get(book_uuid, {}).get("tolino_id"),
                              "fingerprint": fp, "calibre_id": book_id}
        old = state.get(book_uuid, {})
        if (not old.get("tolino_id") or old.get("fingerprint") != fp or
                (upload_book_ids is not None and book_id in upload_book_ids)):
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
    text = str(value)
    text = re.sub(r"(?i)(authorization\s*:\s*)([^,;]+)", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)((?:access|refresh)[_-]?token|password|secret)\s*[=:]\s*[^\s,;]+",
                  r"\1=[REDACTED]", text)
    return text


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
            lines.append("Fehlermeldung: %s" % result.get("error_message", ""))
            lines.append("Traceback:")
            lines.extend(str(result.get("traceback", "")).rstrip().splitlines())
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
        step("Cover-Typ/Größe", lambda: _diagnostic_covers(database, book_ids))
    state_result = step("Lokaler Sync-Status", lambda: state if isinstance(state, dict) else {})
    state = state_result.get("value", {}) if state_result.get("status") == "ok" else {}
    step("plan_sync-Ergebnisform", lambda: _diagnostic_plan(metadata, state, preferred_formats))
    return results


def _calibre_version():
    try:
        import calibre
        return getattr(calibre, "__version__", "unbekannt")
    except ImportError:
        return "Calibre-Modul nicht verfügbar (Diagnose außerhalb Calibre)"


def _diagnostic_ids(database):
    ids = list(iter_book_ids(database))
    return {"count": len(ids), "ids": ids[:20], "truncated": len(ids) > 20}


def _diagnostic_metadata(database, book_id):
    item = database.get_metadata(book_id)
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
