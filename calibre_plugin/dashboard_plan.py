"""Comparison planning and action summaries for the sync dashboard."""
import os

from .planning_values import (SyncJob, display_value, format_signatures,
                              metadata_value, plural)
from .identifiers import (book_uuid, state_keys_by_book_id,
                          valid_book_uuid)
from .library import (TOLINO_READ_COLUMN, bool_column_value, cover_bytes,
                      image_suffix, metadata_ignored, metadata_tolino_id,
                      normalize_formats, safe_format_path)
from .plan import (normalize_removals, plan_collection_updates,
                   plan_metadata_updates, plan_sync, series_map_from_metadata)
from .config import load_state
from .tolino import log_warning

NOTHING_TO_DO_TEXT = "Es war nichts zu übertragen."


def local_metadata(dialog, context, state=None):
    """Calibre-Metadaten und benötigte Format-Signaturen lesen."""
    metadata = {}
    state = load_state(state)
    state_keys = state_keys_by_book_id(state)
    database = dialog.gui.current_db
    api = database.new_api
    for book_id in api.all_book_ids():
        item = api.get_metadata(book_id)
        identifier = book_uuid(item)
        if not identifier:
            identifier = (state_keys.get(book_id)
                          or "calibre-%s" % book_id)
        identifiers = metadata_value(item, "identifiers", {}) or {}
        isbn = (metadata_value(item, "isbn", "")
                or (identifiers.get("isbn", "")
                    if isinstance(identifiers, dict) else ""))
        metadata[book_id] = {
            "uuid": identifier,
            "title": metadata_value(item, "title"),
            # Vom Abgleich ausgenommen: gekaufte Bücher sollen nicht erneut
            # in die Cloud geladen werden.
            "ignored": metadata_ignored(item),
            "authors": display_value(metadata_value(
                item, "authors", metadata_value(item, "author"))),
            "isbn": display_value(isbn),
            "formats": metadata_value(item, "formats"),
            "last_modified": str(metadata_value(item, "last_modified")),
            "tolino_id": (metadata_tolino_id(item, context.account_name)
                          if context.columns.get("id") else ""),
            "series": str(metadata_value(item, "series") or "").strip(),
        }
        if context.columns.get("read"):
            metadata[book_id]["calibre_read"] = bool_column_value(
                item, TOLINO_READ_COLUMN)
    preferred_formats = normalize_formats(context.preferred_formats)
    for book_id, item in metadata.items():
        item["formats"] = normalize_formats(item["formats"])
        preferred = [fmt for fmt in preferred_formats if fmt in item["formats"]]
        item["format_signatures"] = format_signatures(
            database, book_id, preferred)
    return metadata


def syncable_metadata(metadata):
    """Metadaten ohne die in Calibre als «ignoriert» markierten Bücher."""
    return {book_id: item for book_id, item in (metadata or {}).items()
            if not (isinstance(item, dict) and item.get("ignored"))}


def syncable_book_ids(metadata, book_ids):
    """Ausgewählte Calibre-Nummern ohne die ignorierten Bücher."""
    return set(book_ids or ()) & set(syncable_metadata(metadata))


def plan_actions(dialog, context, metadata, state, selection):
    """Turn the confirmed selection into concrete jobs and next state."""
    preferred = normalize_formats(context.preferred_formats)
    # Ignorierte Bücher fallen hier endgültig heraus -- unabhängig davon,
    # was die Auswahl enthält.
    requested_uploads = syncable_book_ids(metadata, selection["uploads"])
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
    titles = titles_for(metadata, removal_titles)
    meta_updates = plan_metadata_updates(
        metadata, state, uploads,
        only_book_ids=syncable_book_ids(metadata, selection["metadata"]))
    series_map = series_map_from_metadata(syncable_metadata(metadata))
    jobs, skipped_uploads = upload_jobs(dialog, metadata, uploads)
    skipped_uploads = skipped_formats + list(skipped_uploads)
    planned_uploads = {item.book_id for item in uploads}
    planned_meta = {record[0] for record in meta_updates}
    gaps = {
        "uploads": len(set(selection["uploads"]) - planned_uploads),
        "metadata": len(set(selection["metadata"]) - planned_meta),
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


def titles_for(metadata, removal_titles=None):
    titles = {}
    for item in (metadata or {}).values():
        if not isinstance(item, dict):
            continue
        key = valid_book_uuid(item.get("uuid"))
        if key:
            titles[key] = str(item.get("title") or "")
    for tolino_id, title in (removal_titles or {}).items():
        titles[str(tolino_id)] = str(title or "")
    return titles


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
    marked = sum(1 for _tolino, _book, finished in changes if finished)
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
