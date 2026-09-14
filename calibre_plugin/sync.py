import hashlib
import json
import os
import re
import unicodedata


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
        candidate = row[0]
        return candidate if isinstance(candidate, int) and candidate > 0 else None
    return None


def normalize_formats(value):
    """Return format names from Calibre's string, sequence, or empty values."""
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (tuple, list, set)):
        return [str(part).strip() for part in value
                if part is not None and str(part).strip()]
    return []


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
    current = {}
    uploads = []
    for book_id, metadata in metadata_by_id.items():
        book_uuid = metadata.get("uuid")
        if not book_uuid:
            continue
        available = {x.upper() for x in normalize_formats(metadata.get("formats"))}
        selected = next((f for f in preferred_formats if f.upper() in available), None)
        if not selected:
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
