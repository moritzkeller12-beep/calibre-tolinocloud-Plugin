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
                code="N02", short="Die Sammlung ist in der Cloud nicht auffindbar.")
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


def read_ids(patches):
    return {book_id for book_id, _name in active_tags(
        patches, "system", READ_TAG)}


def collection_pairs(patches):
    return set(active_tags(patches, "collection"))
