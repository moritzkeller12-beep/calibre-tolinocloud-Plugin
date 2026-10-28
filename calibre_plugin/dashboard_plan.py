"""Comparison planning and action summaries for the sync dashboard."""
import os
import tempfile

from .ui_common import (SyncJob, _display_value, _format_signatures,
                        _metadata_value, plural)
from .identifiers import book_uuid, state_key_for, valid_book_uuid
from .library import (cover_bytes, image_suffix, iter_book_ids,
                      metadata_by_id, metadata_ignored, metadata_read_value,
                      metadata_tolino_id, normalize_formats, safe_format_path)
from .plan import (merge_removals, plan_collection_updates,
                   plan_metadata_updates, plan_sync, series_map_from_metadata)
from .config import load_state
from .tolino import log_warning

NOTHING_TO_DO_TEXT = "Es war nichts zu übertragen."


def local_metadata(dialog):
    """Calibre-Metadaten und benötigte Format-Signaturen lesen."""
    metadata = {}
    state = saved_state(dialog)
    for book_id in iter_book_ids(dialog.gui.current_db):
        item = metadata_by_id(dialog.gui.current_db, book_id)
        identifier = book_uuid(item)
        if not identifier:
            identifier = (state_key_for(state, book_id)
                          or "calibre-%s" % book_id)
        identifiers = _metadata_value(item, "identifiers", {}) or {}
        isbn = (_metadata_value(item, "isbn", "")
                or (identifiers.get("isbn", "")
                    if isinstance(identifiers, dict) else ""))
        metadata[book_id] = {
            "uuid": identifier,
            "title": _metadata_value(item, "title"),
            # Vom Abgleich ausgenommen: gekaufte Bücher sollen nicht erneut
            # in die Cloud geladen werden.
            "ignored": metadata_ignored(item),
            "authors": _display_value(_metadata_value(
                item, "authors", _metadata_value(item, "author"))),
            "isbn": _display_value(isbn),
            "formats": _metadata_value(item, "formats"),
            "last_modified": str(_metadata_value(item, "last_modified")),
            "tolino_id": (metadata_tolino_id(
                item, getattr(dialog, "account_name", ""))
                          if dialog.sync_column_enabled else ""),
            "series": str(_metadata_value(item, "series") or "").strip(),
        }
        if getattr(dialog, "read_column_enabled", False):
            metadata[book_id]["calibre_read"] = metadata_read_value(item)
    values = dialog.values()
    preferred_formats = normalize_formats(
        values.get("preferred_formats", ()))
    for book_id, item in metadata.items():
        item["formats"] = normalize_formats(item["formats"])
        preferred = [fmt for fmt in preferred_formats if fmt in item["formats"]]
        item["format_signatures"] = _format_signatures(
            dialog.gui.current_db, book_id, preferred)
    return metadata


def saved_state(dialog):
    try:
        return load_state(dialog.values().get("state"))
    except Exception:
        return {}


def _ignored(item):
    """True, wenn das Buch laut Calibre-Spalte vom Abgleich ausgenommen ist."""
    return bool(isinstance(item, dict) and item.get("ignored"))


def syncable_book_ids(metadata, book_ids):
    """Ausgewählte Calibre-Nummern ohne die ignorierten Bücher."""
    return {book_id for book_id in (book_ids or ())
            if not _ignored((metadata or {}).get(book_id))}


def syncable_metadata(metadata):
    """Metadaten ohne ignorierte Bücher (für Serien und Sammlungen)."""
    return {book_id: item for book_id, item in (metadata or {}).items()
            if not _ignored(item)}


def automatic_action_count(metadata, state):
    """Count automatic series updates waiting in the Cloud.

    Ignorierte Bücher zählen nicht mit: für sie würde der Start nichts tun.
    """
    return len(plan_collection_updates(
        series_map_from_metadata(syncable_metadata(metadata)), state))


def plan_actions(dialog, settings, metadata, state, selection):
    """Turn the confirmed selection into concrete jobs and next state."""
    preferred = normalize_formats(settings["preferred_formats"])
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
    removals, removal_titles = merge_removals(
        (selection or {}).get("cloud_deletions"))
    titles = titles_for(metadata, removal_titles)
    meta_updates = plan_metadata_updates(
        metadata, state, uploads,
        only_book_ids=syncable_book_ids(metadata, selection["metadata"]))
    series_map = series_map_from_metadata(syncable_metadata(metadata))
    jobs, skipped_uploads = upload_jobs(dialog, settings, metadata, uploads)
    skipped_uploads = skipped_formats + list(skipped_uploads)
    settings["state"] = state
    gaps = selection_gaps(selection, uploads, meta_updates)
    return {
        "jobs": jobs,
        "metadata": metadata,
        "removals": removals,
        "removal_titles": removal_titles,
        "meta_updates": meta_updates,
        "series_map": series_map,
        "skipped_uploads": skipped_uploads,
        "selected_read": selection["read"],
        "selected_unread": selection["unread"],
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


def selection_gaps(selection, uploads, meta_updates):
    """Count selected actions that could not become work items."""
    planned_uploads = {item.book_id for item in uploads}
    planned_meta = {record[0] for record in meta_updates}
    open_uploads = set(selection["uploads"]) - planned_uploads
    open_meta = set(selection["metadata"]) - planned_meta
    return {"uploads": len(open_uploads), "metadata": len(open_meta)}


def upload_jobs(dialog, settings, metadata, uploads):
    """Build upload jobs, recording unavailable local formats."""
    jobs = []
    skipped_uploads = []
    for upload in uploads:
        book_id = upload.book_id
        book_uuid = upload.book_uuid
        fmt = upload.format_name
        old_id = upload.old_id
        try:
            path = safe_format_path(dialog.gui.current_db, book_id, fmt)
            if not os.path.isfile(path):
                raise ValueError("Datei nicht gefunden: %s" % path)
        except ValueError as exc:
            title = metadata.get(book_id, {}).get("title") or "ohne Titel"
            skipped_uploads.append(
                "Buch %s (%s), Format %s: %s" % (book_id, title, fmt, exc))
            continue
        cover_path = None
        try:
            cover = cover_bytes(dialog.gui.current_db, book_id)
        except Exception as exc:
            log_warning("Cover für Buch %s nicht verfügbar: %s" % (book_id, exc))
            cover = None
        suffix = image_suffix(cover)
        if cover and suffix:
            temp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            temp.write(cover)
            temp.close()
            cover_path = temp.name
            dialog.temp_files.append(cover_path)
        elif cover:
            log_warning("Coverformat unbekannt – Cover wird nicht übertragen.")
        title = (metadata.get(book_id) or {}).get("title") or ""
        jobs.append(SyncJob(book_id, book_uuid, fmt, old_id, path,
                            cover_path, title))
    return jobs, skipped_uploads


def open_collections(preparation, state):
    """Count only collection additions that are still needed."""
    return len(plan_collection_updates(
        preparation.get("series_map") or {}, state or {}))


def planned_actions(preparation, uploads, state):
    return (int(uploads) + len(preparation["removals"])
            + len(preparation.get("meta_updates") or ())
            + open_collections(preparation, state)
            + (1 if preparation.get("selected_read") else 0)
            + (1 if preparation.get("selected_unread") else 0))


def plan_text(preparation, uploads, state):
    """Summarize concrete actions before starting the worker."""
    parts = []
    if uploads:
        parts.append("%d Upload%s" % (uploads, "s" if uploads != 1 else ""))
    if preparation["removals"]:
        count = len(preparation["removals"])
        parts.append("%d %s" % (count, plural(count, "Löschung", "Löschungen")))
    if preparation.get("meta_updates"):
        count = len(preparation["meta_updates"])
        parts.append("%d Metadaten" % count)
    collections = open_collections(preparation, state)
    if collections:
        parts.append("%d Sammlungen" % collections)
    if preparation.get("selected_read"):
        parts.append("%d gelesen" % len(preparation["selected_read"]))
    if preparation.get("selected_unread"):
        parts.append("%d ungelesen" % len(preparation["selected_unread"]))
    return ("Läuft: %s." % ", ".join(parts)
            if parts else "Läuft …")


def nothing_to_do_text(preparation):
    """Explain why selected rows could not become actions."""
    gaps = preparation.get("selected_without_action") or {}
    uploads = int(gaps.get("uploads") or 0)
    metadata = int(gaps.get("metadata") or 0)
    if uploads or metadata:
        note = NOTHING_TO_DO_TEXT + (
            "\n\n%d ausgewählte Aktionen konnten nicht eingeplant werden."
            % (uploads + metadata))
        return note
    return NOTHING_TO_DO_TEXT
