"""Book identifier normalization and sync-state key migration."""
import re

_UUID_TEXT = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# Calibre's database path may return 'dummy' when the field is absent.
_UUID_PLACEHOLDERS = frozenset(("none", "null", "undefined", "dummy"))
def normalize_uuid_text(value):
    """Normalize UUIDs represented as text, bytes, UUID objects, or reprs."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    elif not isinstance(value, str):
        value = str(value)
    text = value.strip().strip("'\" \t\r\n")
    match = _UUID_TEXT.search(text)
    if match:
        return match.group(0).casefold()
    return text


def valid_book_uuid(value):
    """Return a usable Calibre book key, or empty for junk/incomplete data."""
    text = normalize_uuid_text(value)
    if not text or text.casefold() in _UUID_PLACEHOLDERS:
        return ""
    if len(text) > 128 or any(character.isspace() for character in text):
        return ""
    return text


def is_real_uuid(value):
    """Check the canonical UUID shape for identifier values from sources."""
    return bool(_UUID_TEXT.fullmatch(normalize_uuid_text(value)))


def display_title(title, number=None):
    """Prefer a human-readable title to an internal identifier in progress."""
    text = str(title or "").strip()
    if text:
        return text
    value = str(number if number is not None else "").strip()
    if value.isdigit():
        return "Buch %s" % value
    return "Buch ohne Titel"


def book_uuid(item):
    """Return a canonical Calibre UUID from Metadata, if present."""
    raw = (item.get("uuid") if isinstance(item, dict)
           else getattr(item, "uuid", None))
    value = normalize_uuid_text(raw)
    return value if value and is_real_uuid(value) else ""


def migrate_state_keys(metadata_by_id, state):
    """Migrate fallback state keys to real UUIDs when the UUID becomes known."""
    if not isinstance(metadata_by_id, dict) or not isinstance(state, dict):
        return 0
    migrated = 0
    for book_id, metadata in metadata_by_id.items():
        if not isinstance(metadata, dict):
            continue
        key = valid_book_uuid(metadata.get("uuid"))
        if not key or key in state:
            continue
        old_key = state_key_for(state, book_id)
        if not old_key or old_key == key:
            continue
        entry = state.pop(old_key)
        if isinstance(entry, dict):
            entry["calibre_id"] = book_id
        state[key] = entry
        migrated += 1
    return migrated


def state_keys_by_book_id(state):
    """Calibre-Nummer → gespeicherter Zustandsschlüssel in EINEM Durchlauf.

    ``state_key_for`` suchte für jedes Buch erneut linear über den Zustand;
    wer mehrere Bücher prüft, baut den Index einmal.
    """
    keys = {}
    if not isinstance(state, dict):
        return keys
    for key, entry in state.items():
        if not isinstance(entry, dict):
            continue
        recorded = entry.get("calibre_id")
        try:
            keys[int(recorded)] = str(key)
        except (TypeError, ValueError):
            continue
    return keys


def state_key_for(state, book_id):
    """Find a stored state key by its recorded Calibre book id."""
    try:
        return state_keys_by_book_id(state).get(int(book_id), "")
    except (TypeError, ValueError):
        return ""
