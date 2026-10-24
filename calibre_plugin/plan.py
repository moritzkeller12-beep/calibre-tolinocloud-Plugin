"""Pure sync-plan construction, validation, and fingerprint confirmation."""
from .comparison import (content_fingerprint, fingerprint,
                         metadata_fingerprint)
from .identifiers import valid_book_uuid
from .library import normalize_formats


# Die drei Werte, die einen «abgeglichenen» Stand ausmachen. Sie werden
# erst nach einer erfolgreichen Cloud-Aktion bestaetigt.
FINGERPRINT_KEYS = ("fingerprint", "content_fp", "meta_fp")


def split_pending_fingerprints(current, previous, book_uuids):
    """Neue Fingerabdruecke aus dem Plan nehmen, bis die Aktion gelingt.

    `plan_sync` schreibt den Zielzustand fuer JEDES Buch -- ausgefuehrt ist
    zu diesem Zeitpunkt noch nichts. Fuer die uebergebenen Buecher wandern
    die neuen Werte deshalb in ein getrenntes Dict, und der Zustand bekommt
    den alten Stand zurueck. Der Worker uebernimmt sie erst nach einer
    erfolgreichen Aktion; ein gescheiterter oder gar nicht gestarteter
    Upload kann so nicht als «synchronisiert» gelten.
    """
    pending = {}
    for book_uuid in book_uuids:
        entry = current.get(book_uuid)
        if not isinstance(entry, dict):
            continue
        values = {key: entry[key] for key in FINGERPRINT_KEYS if key in entry}
        old = previous.get(book_uuid)
        for key in FINGERPRINT_KEYS:
            if isinstance(old, dict) and key in old:
                entry[key] = old[key]
            else:
                entry.pop(key, None)
        if values:
            pending[str(book_uuid)] = values
    return pending


def confirm_fingerprints(entry, fingerprints, keys=FINGERPRINT_KEYS):
    """Geplante Fingerabdruecke nach einer erfolgreichen Aktion bestaetigen."""
    if not isinstance(entry, dict) or not isinstance(fingerprints, dict):
        return entry
    for key in keys:
        if key in fingerprints:
            entry[key] = fingerprints[key]
    return entry


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


def plan_sync(metadata_by_id, state, preferred_formats,
              upload_book_ids=None):
    """Build uploads and the next sync state."""
    if not isinstance(metadata_by_id, dict) or not isinstance(state, dict):
        raise ValueError("Preparation requires metadata and sync state mappings.")
    preferred_formats = normalize_formats(preferred_formats)
    current = {}
    uploads = []
    for book_id, metadata in metadata_by_id.items():
        if not isinstance(metadata, dict):
            raise ValueError("Preparation returned invalid metadata for book %r." % (book_id,))
        book_uuid = valid_book_uuid(metadata.get("uuid"))
        if not book_uuid:
            continue
        available = set(normalize_formats(metadata.get("formats")))
        selected = next((f for f in preferred_formats if f in available), None)
        if not selected:
            if upload_book_ids is not None and book_id in upload_book_ids:
                title = metadata.get("title") or "ohne Titel"
                requested = ", ".join(preferred_formats) or "kein bevorzugtes Format"
                raise ValueError(
                    "Buch %r (%s): kein unterstütztes Buchformat gefunden "
                    "(gesucht: %s)." %
                    (book_id, title, requested)
                )
            # Das Buch existiert weiterhin in Calibre, ist aber gerade
            # nicht hochladbar. Den bisherigen Zustand unverändert
            # übernehmen: tolino-ID, Fingerabdrücke und Sammlungen bleiben
            # erhalten, damit ein später ergänztes Format kein Duplikat
            # erzeugt und die Löschprüfung das Buch nicht als verschwunden
            # ansieht. Die Fingerabdrücke werden bewusst NICHT aktualisiert.
            old = state.get(book_uuid)
            if isinstance(old, dict) and old:
                entry = dict(old)
                entry["calibre_id"] = book_id
                current[book_uuid] = entry
            continue
        fp = fingerprint(metadata, selected)
        old = state.get(book_uuid) or {}
        if not isinstance(old, dict):
            old = {}
        entry = {"tolino_id": old.get("tolino_id"),
                 "fingerprint": fp, "calibre_id": book_id,
                 "title": str(metadata.get("title") or ""),
                 "content_fp": content_fingerprint(metadata, selected),
                 "meta_fp": metadata_fingerprint(metadata)}
        # Gemerkte Serien-Zuordnungen gehören zum Buch und überleben den
        # Neuaufbau des Zustands.
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
    return uploads, [], current


def merge_removals(removals, explicit=()):
    """Combine automatic and user-picked cloud deletions.

    `removals` are the (book_uuid, tolino_id) pairs found by `plan_sync`;
    `explicit` holds (book_uuid, tolino_id, title) triples marked in the
    comparison dialog. A cloud copy is deleted once, even when both paths
    point at it. Returns the merged pairs and a title map keyed by tolino
    id (used for the confirmation list).
    """
    merged = []
    seen = set()
    titles = {}
    for entry in tuple(removals or ()):
        try:
            book_uuid, tolino_id = entry
        except (TypeError, ValueError):
            continue
        tolino_id = str(tolino_id or "").strip()
        if not tolino_id or tolino_id in seen:
            continue
        seen.add(tolino_id)
        merged.append((str(book_uuid or ""), tolino_id))
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

    Returns (uploads, meta_updates); uploads are returned unchanged.
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
        # Kennungen kommen aus der Datenbankschicht und koennen ein
        # `uuid.UUID`-Objekt oder eine
        # anders geschriebene Form sein. Nur die normalisierte Kennung
        # trifft den Zustandseintrag -- sonst waere der Abgleich stumm
        # wirkungslos.
        book_uuid = valid_book_uuid(metadata.get("uuid"))
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
        stored_meta_fp = old.get("meta_fp")
        if stored_meta_fp and stored_meta_fp == metadata_fingerprint(metadata):
            continue
        fields = metadata_update_fields(metadata)
        if not fields:
            continue
        updates.append((book_id, book_uuid, tolino_id, fields))
    return list(uploads), updates
