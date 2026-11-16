"""Qt-freie Sync-Planung: Metadaten, Jobs, Aktionen und ihre Texte.

Dieses Modul importiert bewusst kein Qt: `ui_dashboard` und die
Planungstests kommen damit ohne UI-Stubs aus.
"""
import os
from typing import NamedTuple

from .identifiers import (book_uuid, calibre_key, display_title, state_entry,
                          valid_book_uuid)
from .library import (TOLINO_READ_COLUMN, bool_column_value, cover_bytes,
                      image_suffix, metadata_ignored, metadata_tolino_id,
                      normalize_formats, safe_format_path)
from .tolino import log_warning

NOTHING_TO_DO_TEXT = "Es war nichts zu übertragen."


def metadata_value(item, name, default=""):
    """Feld aus einem Calibre-Metadatenobjekt oder -Dict lesen."""
    if hasattr(item, name):
        return getattr(item, name)
    return item.get(name, default) if isinstance(item, dict) else default


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


class UploadRecord(NamedTuple):
    """One planned upload: Calibre book, chosen format, replaced cloud copy."""

    book_id: object
    book_uuid: object
    format_name: object
    old_id: object


class MetaUpdateRecord(NamedTuple):
    """One planned in-place metadata patch for an existing cloud copy."""

    book_id: object
    book_uuid: object
    tolino_id: object
    fields: object


def plan_sync(metadata_by_id, state, preferred_formats, upload_book_ids):
    """Build upload jobs for selected Calibre book IDs with a supported format."""
    selected_ids = set(upload_book_ids or ())
    preferred_formats = normalize_formats(preferred_formats)
    uploads = []
    for book_id, metadata in metadata_by_id.items():
        if book_id not in selected_ids or not isinstance(metadata, dict):
            continue
        identifier = valid_book_uuid(metadata.get("uuid"))
        if not identifier:
            continue
        available = set(normalize_formats(metadata.get("formats")))
        selected_format = next(
            (fmt for fmt in preferred_formats if fmt in available), None)
        if not selected_format:
            continue
        old = state.get(identifier)
        old_id = old.get("tolino_id") if isinstance(old, dict) else None
        uploads.append(UploadRecord(book_id, identifier, selected_format, old_id))
    return uploads


def normalize_removals(explicit=()):
    """Normalize user-picked cloud deletions.

    `explicit` holds (book_uuid, tolino_id, title) triples marked in the
    comparison dialog. A cloud copy is deleted once. Returns the selected
    pairs and a title map keyed by tolino id.
    """
    merged = []
    seen = set()
    titles = {}
    for entry in tuple(explicit or ()):
        if not isinstance(entry, (tuple, list)) or len(entry) < 2:
            continue
        book_uuid, tolino_id = entry[0], entry[1]
        title = str(entry[2] or "") if len(entry) > 2 else ""
        tolino_id = str(tolino_id or "").strip()
        if not tolino_id:
            continue
        if title:
            titles[tolino_id] = title
        if tolino_id not in seen:
            seen.add(tolino_id)
            merged.append((str(book_uuid or ""), tolino_id))
    return merged, titles


def series_map_from_metadata(metadata_by_id):
    """uuid -> Serienname fuer alle Buecher mit Serie.

    Reine Zuordnung ohne Cloud-Zugriff.
    """
    if not isinstance(metadata_by_id, dict):
        return {}
    return {
        book_uuid: series
        for metadata in metadata_by_id.values()
        if isinstance(metadata, dict)
        and (book_uuid := str(metadata.get("uuid") or ""))
        and (series := str(metadata.get("series") or "").strip())
    }


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
        entry = state.get(book_uuid)
        if not book_uuid or not series or not isinstance(entry, dict):
            continue
        tolino_id = str(entry.get("tolino_id") or "").strip()
        recorded = entry.get("collections")
        recorded = {
            str(name).strip() for name in recorded
            if str(name or "").strip()
        } if isinstance(recorded, list) else set()
        if tolino_id and series not in recorded:
            pending.add((str(book_uuid), tolino_id, series))
    return sorted(pending, key=lambda item: (item[1], item[2], item[0]))


def metadata_update_fields(metadata):
    """Tolino /meta payload fields for an in-place metadata update."""
    return {
        target: value
        for source, target in (("title", "title"), ("authors", "author"),
                               ("isbn", "isbn"))
        if (value := str(metadata.get(source) or "").strip())
    }


def plan_metadata_updates(metadata_by_id, state, uploads, only_book_ids):
    """In-place Metadaten-Updates (/meta) für die ausgewählten Bücher.

    Ein Buch kommt in Frage, wenn es bereits eine Cloud-Kopie hat und
    `metadata_update_fields` etwas zu schreiben hat. Ob die Cloud schon
    denselben Stand kennt, entscheidet der Vergleich (`meta_verified_fp`),
    nicht diese Planung. `only_book_ids` enthält die vom Nutzer ausgewählten
    Bücher. Bücher, die ohnehin hochgeladen werden, werden NIE zusätzlich
    gepatcht – der Upload trägt die Metadaten mit.

    Returns the planned metadata updates.
    """
    upload_uuids = {str(record.book_uuid) for record in uploads}
    # In Bibliotheksreihenfolge laufen, aber Schluessel als Text vergleichen:
    # Calibre liefert IDs als int, Aufrufer koennen sie als str uebergeben.
    selected_ids = {str(value) for value in only_book_ids or ()}
    updates = []
    for book_id, metadata in metadata_by_id.items():
        if str(book_id) not in selected_ids:
            continue
        if not isinstance(metadata, dict):
            raise ValueError("Preparation returned invalid metadata for book %r." % (book_id,))
        identifier = valid_book_uuid(metadata.get("uuid"))
        if not identifier or identifier in upload_uuids:
            continue
        old = state_entry(state, identifier)
        tolino_id = str(old.get("tolino_id") or "")
        if not tolino_id:
            continue
        fields = metadata_update_fields(metadata)
        if not fields:
            continue
        updates.append(MetaUpdateRecord(book_id, identifier, tolino_id, fields))
    return updates


def local_metadata(database, context):
    """Calibre-Metadaten und benötigte Format-Signaturen lesen.

    Der Aufrufer übergibt die Datenbank direkt. Die Funktion läuft im
    `LocalMetadataWorker`; Calibres thread-sichere `new_api` wird nur lesend
    genutzt, Widgets werden nie übergeben.
    """
    metadata = {}
    api = database.new_api
    preferred_formats = normalize_formats(context.preferred_formats)
    for book_id in api.all_book_ids():
        item = api.get_metadata(book_id)
        identifier = (book_uuid(item)
                      or calibre_key(book_id, context.library_scope))
        identifiers = metadata_value(item, "identifiers", {}) or {}
        isbn = (metadata_value(item, "isbn", "")
                or (identifiers.get("isbn", "")
                    if isinstance(identifiers, dict) else ""))
        formats = normalize_formats(metadata_value(item, "formats"))
        preferred = [fmt for fmt in preferred_formats if fmt in formats]
        metadata[book_id] = {
            "uuid": identifier,
            "title": metadata_value(item, "title"),
            # Vom Abgleich ausgenommen: gekaufte Bücher sollen nicht erneut
            # in die Cloud geladen werden.
            "ignored": metadata_ignored(item),
            "authors": display_value(metadata_value(
                item, "authors", metadata_value(item, "author"))),
            "isbn": display_value(isbn),
            "formats": formats,
            "tolino_id": (metadata_tolino_id(item, context.account_name)
                          if context.columns.get("id") else ""),
            "series": str(metadata_value(item, "series") or "").strip(),
            "format_signatures": format_signatures(
                database, book_id, preferred),
        }
        if context.columns.get("read"):
            metadata[book_id]["calibre_read"] = bool_column_value(
                item, TOLINO_READ_COLUMN)
    return metadata


def syncable_metadata(metadata):
    """Metadaten ohne die in Calibre als «ignoriert» markierten Bücher."""
    return {book_id: item for book_id, item in (metadata or {}).items()
            if not (isinstance(item, dict) and item.get("ignored"))}


def plan_actions(dialog, context, metadata, state, selection, series_map=None):
    """Turn the confirmed selection into concrete jobs and next state."""
    preferred = normalize_formats(context.preferred_formats)
    # Ignorierte Bücher fallen hier endgültig heraus -- unabhängig davon,
    # was die Auswahl enthält.
    syncable = syncable_metadata(metadata)
    syncable_ids = set(syncable)
    requested_uploads = set(selection["uploads"]) & syncable_ids
    uploads = plan_sync(
        metadata, state, preferred, upload_book_ids=requested_uploads)
    planned_book_ids = {record[0] for record in uploads}
    skipped_formats = []
    display = ", ".join(preferred) or "EPUB"
    for book_id in requested_uploads - planned_book_ids:
        item = metadata.get(book_id)
        if not isinstance(item, dict) or not valid_book_uuid(item.get("uuid")):
            continue
        title = item.get("title") or "ohne Titel"
        skipped_formats.append(
            "Buch %s (%s): kein Format %s." %
            (book_id, title, display))
    removals, removal_titles = normalize_removals(
        (selection or {}).get("cloud_deletions"))
    titles = {valid_book_uuid(item.get("uuid")): str(item.get("title") or "")
              for item in (metadata or {}).values() if isinstance(item, dict)}
    titles.pop(None, None)
    titles.update({str(key): str(value or "")
                   for key, value in removal_titles.items()})
    requested_metadata = set(selection["metadata"]) & syncable_ids
    meta_updates = plan_metadata_updates(
        metadata, state, uploads, only_book_ids=requested_metadata)
    if series_map is None:
        series_map = series_map_from_metadata(syncable)
    jobs, skipped_uploads = upload_jobs(dialog, metadata, uploads)
    skipped_uploads = skipped_formats + list(skipped_uploads)
    planned_meta = {record[0] for record in meta_updates}
    gaps = {
        "uploads": len(requested_uploads - planned_book_ids),
        "metadata": len(requested_metadata - planned_meta),
    }
    return {
        "jobs": jobs,
        "metadata": metadata,
        "removals": removals,
        "removal_titles": removal_titles,
        "meta_updates": meta_updates,
        "series_map": series_map,
        "collections": len(plan_collection_updates(series_map, state)),
        "skipped_uploads": skipped_uploads,
        "read_changes": selection["read_changes"],
        "titles": titles,
        "selected_without_action": gaps,
    }


def upload_jobs(dialog, metadata, uploads):
    """Build upload jobs, recording unavailable local formats."""
    jobs = []
    skipped_uploads = []
    for upload in uploads:
        book_id = upload.book_id
        identifier = upload.book_uuid
        fmt = upload.format_name
        old_id = upload.old_id
        title = str((metadata.get(book_id) or {}).get("title") or "")
        try:
            path = safe_format_path(dialog.gui.current_db, book_id, fmt)
            if not os.path.isfile(path):
                raise ValueError("Datei nicht gefunden: %s" % path)
        except ValueError as exc:
            skipped_uploads.append(
                "Buch %s (%s), Format %s: %s"
                % (book_id, title or "ohne Titel", fmt, exc))
            continue
        try:
            cover = cover_bytes(dialog.gui.current_db, book_id)
        except Exception as exc:
            log_warning("Cover für Buch %s nicht verfügbar: %s" % (book_id, exc))
            cover = None
        if cover and not image_suffix(cover):
            log_warning("Coverformat unbekannt – Cover wird nicht übertragen.")
            cover = None
        jobs.append(SyncJob(book_id, identifier, fmt, old_id, path, cover,
                            title))
    return jobs, skipped_uploads


def plan_counts(preparation, uploads):
    """Zahlen des Laufs -- eine Regel für Plan, Text und Fortschritt."""
    changes = preparation.get("read_changes") or ()
    marked = sum(1 for change in changes
                 if len(change) > 2 and change[2])
    unmarked = len(changes) - marked
    counts = {
        "uploads": int(uploads),
        "removals": len(preparation.get("removals") or ()),
        "metadata": len(preparation.get("meta_updates") or ()),
        "collections": int(preparation.get("collections") or 0),
        "read": marked,
        "unread": unmarked,
    }
    # Lesestatus kostet je Richtung einen Schritt, nicht je Buch.
    counts["steps"] = (counts["uploads"] + counts["removals"]
                       + counts["metadata"] + counts["collections"]
                       + (1 if marked else 0) + (1 if unmarked else 0))
    return counts


def plan_text(preparation, uploads):
    """Summarize concrete actions before starting the worker."""
    counts = plan_counts(preparation, uploads)
    parts = []
    if counts["uploads"]:
        parts.append("%d %s" % (counts["uploads"],
                                plural(counts["uploads"], "Upload", "Uploads")))
    if counts["removals"]:
        parts.append("%d %s" % (counts["removals"],
                                plural(counts["removals"], "Löschung",
                                       "Löschungen")))
    if counts["metadata"]:
        parts.append("%d Metadaten" % counts["metadata"])
    if counts["collections"]:
        parts.append("%d Sammlungen" % counts["collections"])
    if counts["read"]:
        parts.append("%d gelesen" % counts["read"])
    if counts["unread"]:
        parts.append("%d ungelesen" % counts["unread"])
    return ("Läuft: %s." % ", ".join(parts)
            if parts else "Läuft …")


def nothing_to_do_text(preparation):
    """Explain why selected rows could not become actions."""
    gaps = preparation.get("selected_without_action") or {}
    uploads = int(gaps.get("uploads") or 0)
    metadata = int(gaps.get("metadata") or 0)
    if uploads or metadata:
        return NOTHING_TO_DO_TEXT + (
            "\n\n%d ausgewählte Aktionen konnten nicht eingeplant werden."
            % (uploads + metadata))
    return NOTHING_TO_DO_TEXT
