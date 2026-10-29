"""Pure helpers for Tolino sync-data tag patches."""

import time

READ_TAG = "collection_finished_readings_name"


def tag_patch(op, book_id, name, category, error_type, revision=None,
              tag_id=None):
    """Build a sync-data tag patch; removal requires the server tag ID."""
    value = {"modified": int(time.time() * 1000),
             "name": name, "category": category}
    if revision is not None:
        value["revision"] = revision
    path = "/publications/%s/tags" % book_id
    if op == "remove":
        if not str(tag_id or "").strip():
            raise error_type(
                "Marken-ID fehlt (%s / %s)." % (book_id, name),
                code="N06", short="Die Sammlung ist in der Cloud nicht auffindbar.")
        path = "%s/%s" % (path, tag_id)
    return {"op": op, "value": value, "path": path}


def tag_path_parts(path):
    """Return (book ID, tag ID) for valid publication tag paths."""
    parts = str(path or "").split("/")
    if (len(parts) < 4 or parts[1] != "publications"
            or parts[3] != "tags" or not parts[2]):
        return None
    return str(parts[2]), str(parts[4] if len(parts) > 4 else "")


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


def tag_state(patches, book_id, name, category):
    """Return the active patch for one book/tag, or None."""
    return active_tags(patches, category, name).get((str(book_id), name))


def read_ids(patches):
    return {book_id for book_id, _name in active_tags(
        patches, "system", READ_TAG)}


def add_to_collection(client, book_id, collection_name, revision=None):
    patch = client._tag_patch("add", book_id, collection_name, "collection")
    data = client._sync_patch({
        "revision": client._current_revision(revision), "patches": [patch]})
    return data.get("revision")


def mark_read_many(client, book_ids, finished, error_type):
    ids = [str(book_id) for book_id in book_ids or []
           if str(book_id or "").strip()]
    if not ids:
        raise error_type(
            "Gelesen-Markierung ohne Buch.", code="S90",
            short="Es wurde kein Buch ausgewählt.")
    revision, patches = client.sync_snapshot()
    updates = []
    for book_id in ids:
        existing = tag_state(patches, book_id, READ_TAG, "system")
        if finished:
            if existing is None:
                updates.append(client._tag_patch(
                    "add", book_id, READ_TAG, "system"))
        elif existing is not None:
            updates.append(client._tag_patch(
                "remove", book_id, READ_TAG, "system",
                revision=existing["value"].get("revision"),
                tag_id=tag_id_from_patch(existing)))
    if not updates:
        return revision
    data = client._sync_patch({"revision": revision, "patches": updates})
    return data.get("revision")


def collection_pairs(patches):
    return set(active_tags(patches, "collection"))
