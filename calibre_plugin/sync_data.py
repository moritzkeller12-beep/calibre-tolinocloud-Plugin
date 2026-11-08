"""Pure helpers for Tolino sync-data tag patches."""

import time

from .errors import TolinoApiError

READ_TAG = "collection_finished_readings_name"
# Der Reader legt die Gelesen-Marke je nach Quelle als System- oder
# Sammlungs-Tag ab. Beide Formen bezeichnen denselben Zustand.
READ_TAG_CATEGORIES = ("system", "collection")


def tag_patch(op, book_id, name, category, revision=None, tag_id=None):
    """Build a sync-data tag patch; removal requires the server tag ID."""
    value = {"modified": int(time.time() * 1000),
             "name": name, "category": category}
    if revision is not None:
        value["revision"] = revision
    path = "/publications/%s/tags" % book_id
    if op == "remove":
        if not str(tag_id or "").strip():
            raise TolinoApiError(
                "Marken-ID fehlt (%s / %s)." % (book_id, name),
                code="N02", short="Die Marke ist in der Cloud nicht auffindbar.")
        path = "%s/%s" % (path, tag_id)
    return {"op": op, "value": value, "path": path}


def _publication_path_parts(path, kind):
    parts = str(path or "").split("/")
    if (len(parts) < 4 or parts[1] != "publications"
            or parts[3] != kind or not parts[2]):
        return None
    return str(parts[2]), str(parts[4] if len(parts) > 4 else "")


def tag_path_parts(path):
    """Return (book ID, tag ID) for valid publication tag paths."""
    return _publication_path_parts(path, "tags")


def tag_id_from_patch(item):
    parts = tag_path_parts((item or {}).get("path"))
    return parts[1] if parts else ""


def active_tags(patches, category, name=None):
    """Reduce add/remove history to ``{(book_id, tag_name): active patch}``."""
    active = {}
    for item in patches:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if not isinstance(value, dict) or value.get("category") != category:
            continue
        if name is not None and value.get("name") != name:
            continue
        parts = tag_path_parts(item.get("path"))
        if parts is None:
            continue
        key = (parts[0], str(value.get("name") or ""))
        if str(item.get("op") or "add") == "remove":
            active.pop(key, None)
        else:
            active[key] = item
    return active


def active_read_tags(patches):
    """Aktive Gelesen-Marken in beiden Cloud-Schreibweisen."""
    active = {}
    for category in READ_TAG_CATEGORIES:
        for key, item in active_tags(patches, category, READ_TAG).items():
            active.setdefault(key, item)
    return active


def read_ids(patches):
    return {book_id for book_id, _name in active_read_tags(patches)}


def bookmark_path_parts(path):
    """Return (book ID, bookmark ID) for a publication bookmark path."""
    return _publication_path_parts(path, "bookmark")


def _position_text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _progress_percent(value, current, last):
    """Prozentwert aus ``progress`` oder aus current/last ableiten."""
    percent = None
    try:
        raw = float(value)
    except (TypeError, ValueError):
        raw = None
    if raw is not None:
        if 0 <= raw <= 1:
            percent = round(raw * 100)
        elif 0 < raw <= 100:
            percent = round(raw)
    if percent is None:
        try:
            current_number, last_number = int(current), int(last)
        except (TypeError, ValueError):
            current_number = last_number = 0
        if last_number > 0:
            percent = round(current_number / last_number * 100)
    if percent is None:
        return None
    return max(0, min(100, int(percent)))


def _bookmark_entry(value):
    if not isinstance(value, dict):
        return None
    current = _position_text(value.get("currentPosition"))
    last = _position_text(value.get("lastPosition"))
    percent = _progress_percent(value.get("progress"), current, last)
    if percent is None and not current and not last:
        return None
    try:
        modified = int(value.get("modified") or 0)
    except (TypeError, ValueError):
        modified = 0
    return {
        "percent": percent,
        "current": current,
        "last": last,
        "position": str(value.get("position") or "").strip(),
        "modified": modified,
    }


def reading_progress(patches):
    """Reduce bookmark patches to the newest progress entry per book.

    Jedes Lesezeichen bleibt unter seiner ``bookmark/<id>`` getrennt; ein
    ``remove`` löscht nur dieses eine Lesezeichen. Angezeigt wird danach das
    zuletzt geänderte noch vorhandene Lesezeichen des Buchs.
    """
    active, order, newest = {}, 0, {}
    for item in patches:
        if not isinstance(item, dict):
            continue
        parts = bookmark_path_parts(item.get("path"))
        if parts is None:
            continue
        key = (parts[0], parts[1])
        if str(item.get("op") or "add") == "remove":
            active.pop(key, None)
            continue
        entry = _bookmark_entry(item.get("value"))
        if entry is None:
            continue
        order += 1
        previous = active.get(key)
        if previous is None or entry["modified"] >= previous[0]["modified"]:
            active[key] = (entry, order)
    for (book_id, _bookmark_id), (entry, sequence) in active.items():
        previous = newest.get(book_id)
        if (previous is None or entry["modified"] > previous[0]["modified"]
                or (entry["modified"] == previous[0]["modified"]
                    and sequence > previous[1])):
            newest[book_id] = (entry, sequence)
    return {book_id: entry for book_id, (entry, _sequence) in newest.items()}


def collection_pairs(patches):
    return set(active_tags(patches, "collection"))
