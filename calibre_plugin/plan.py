"""Pure sync-plan construction and collection/metadata action planning."""
from .identifiers import valid_book_uuid
from .library import normalize_formats


def plan_sync(metadata_by_id, state, preferred_formats, upload_book_ids):
    """Build upload jobs for selected Calibre book IDs with a supported format."""
    if not isinstance(metadata_by_id, dict) or not isinstance(state, dict):
        raise ValueError("Preparation requires metadata and sync state mappings.")
    selected_ids = set(upload_book_ids or ())
    preferred_formats = normalize_formats(preferred_formats)
    uploads = []
    for book_id in selected_ids:
        metadata = metadata_by_id.get(book_id)
        if not isinstance(metadata, dict):
            continue
        book_uuid = valid_book_uuid(metadata.get("uuid"))
        if not book_uuid:
            continue
        available = set(normalize_formats(metadata.get("formats")))
        selected_format = next(
            (fmt for fmt in preferred_formats if fmt in available), None)
        if not selected_format:
            continue
        old = state.get(book_uuid)
        old_id = old.get("tolino_id") if isinstance(old, dict) else None
        uploads.append((book_id, book_uuid, selected_format, old_id))
    return uploads


def merge_removals(explicit=()):
    """Combine user-picked cloud deletions.

    `explicit` holds (book_uuid, tolino_id, title) triples marked in the
    comparison dialog. A cloud copy is deleted once. Returns the selected
    pairs and a title map keyed by tolino id.
    """
    merged = []
    seen = set()
    titles = {}
    for entry in tuple(explicit or ()):
        values = tuple(entry) if isinstance(entry, (tuple, list)) else ()
        if len(values) < 2:
            continue
        book_uuid, tolino_id = values[0], values[1]
        title = str(values[2] or "") if len(values) > 2 else ""
        tolino_id = str(tolino_id or "").strip()
        if not tolino_id:
            continue
        if title:
            titles[tolino_id] = title
        if tolino_id in seen:
            continue
        seen.add(tolino_id)
        merged.append((str(book_uuid or ""), tolino_id))
    return merged, titles


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


def series_map_from_metadata(metadata_by_id):
    """uuid -> Serienname fuer alle Buecher mit Serie.

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
    """Serien, die für diese Cloud-Kopie schon zugeordnet wurden."""
    if not isinstance(entry, dict):
        return set()
    recorded = entry.get("collections")
    if not isinstance(recorded, list):
        return set()
    return {str(name).strip() for name in recorded if str(name or "").strip()}


def remember_collection(state, book_uuid, series):
    """Nach erfolgreichem Schreiben: Zuordnung im Zustand merken."""
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
    """Offene Serien-Zuordnungen als (Buch-UUID, Tolino-ID, Serienname)."""
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
        if not tolino_id or series in recorded_collections(entry):
            continue
        pending.add((str(book_uuid), tolino_id, series))
    return sorted(pending, key=lambda item: (item[1], item[2], item[0]))


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


def plan_metadata_updates(metadata_by_id, state, uploads, only_book_ids):
    """In-place Metadaten-Updates (/meta) für die ausgewählten Bücher.

    Ein Buch kommt in Frage, wenn es bereits eine Cloud-Kopie hat und sich
    Titel/Autor(en)/ISBN/Serie geändert haben oder der gespeicherte Zustand
    keinen passenden Metadaten-Fingerabdruck kennt. `only_book_ids` enthält
    die vom Nutzer ausgewählten Bücher. Bücher, die ohnehin hochgeladen werden, werden NIE
    zusätzlich gepatcht – der Upload trägt die Metadaten mit.

    Returns the planned metadata updates.
    """
    if not isinstance(metadata_by_id, dict) or not isinstance(state, dict):
        raise ValueError("Preparation requires metadata and sync state mappings.")
    upload_uuids = {str(unpack_upload_record(record)["book_uuid"])
                    for record in uploads}
    requested = {str(item) for item in only_book_ids}
    updates = []
    for book_id, metadata in metadata_by_id.items():
        if not isinstance(metadata, dict):
            raise ValueError("Preparation returned invalid metadata for book %r." % (book_id,))
        if str(book_id) not in requested:
            continue
        book_uuid = valid_book_uuid(metadata.get("uuid"))
        if not book_uuid or book_uuid in upload_uuids:
            continue
        old = state.get(book_uuid) or {}
        tolino_id = str(old.get("tolino_id") or "")
        if not tolino_id:
            continue
        fields = metadata_update_fields(metadata)
        if not fields:
            continue
        updates.append((book_id, book_uuid, tolino_id, fields))
    return updates
