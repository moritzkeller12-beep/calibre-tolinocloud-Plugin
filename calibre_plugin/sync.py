import hashlib
import json


def fingerprint(metadata, format_name):
    value = "%s|%s|%s|%s" % (
        metadata.get("uuid", ""),
        metadata.get("last_modified", ""),
        format_name,
        metadata.get("title", ""),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def plan_sync(metadata_by_id, state, preferred_formats, deletions=False):
    current = {}
    uploads = []
    for book_id, metadata in metadata_by_id.items():
        book_uuid = metadata.get("uuid")
        if not book_uuid:
            continue
        available = {str(x).upper() for x in metadata.get("formats", [])}
        selected = next((f for f in preferred_formats if f.upper() in available), None)
        if not selected:
            continue
        fp = fingerprint(metadata, selected)
        current[book_uuid] = {"tolino_id": state.get(book_uuid, {}).get("tolino_id"),
                              "fingerprint": fp, "calibre_id": book_id}
        old = state.get(book_uuid, {})
        if not old.get("tolino_id") or old.get("fingerprint") != fp:
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

