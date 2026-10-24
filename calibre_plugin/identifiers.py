"""Book identifier normalization and sync-state key migration."""
import re

from .library import database_uuid_values


_UUID_TEXT = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# Calibre's database path may return 'dummy' when the field is absent.
_UUID_PLACEHOLDERS = frozenset(("none", "null", "undefined", "dummy"))
UUID_SOURCE_ORDER = ("metadata", "field_for", "sql", "db.uuid", "fallback",
                     "ungueltig")


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


def metadata_uuid(item):
    """Read a raw UUID from a metadata object or mapping."""
    if isinstance(item, dict):
        return item.get("uuid")
    try:
        return item.uuid
    except Exception:
        try:
            return item.get("uuid")
        except Exception:
            return None


def describe_uuid(value):
    """Return a short representation of an identifier value for logs."""
    text = repr(value)
    if len(text) > 60:
        text = text[:57] + "..."
    return "%s (%s)" % (text, type(value).__name__)


def display_title(title, number=None):
    """Prefer a human-readable title to an internal identifier in progress."""
    text = str(title or "").strip()
    if text:
        return text
    value = str(number if number is not None else "").strip()
    if value.isdigit():
        return "Buch %s" % value
    return "Buch ohne Titel"


def _count_source(sources, name):
    if isinstance(sources, dict):
        sources[name] = sources.get(name, 0) + 1


def format_uuid_sources(sources):
    """Format UUID source counts consistently for logs."""
    if not isinstance(sources, dict):
        return ""
    names = [name for name in UUID_SOURCE_ORDER if name in sources]
    names.extend(sorted(name for name in sources if name not in UUID_SOURCE_ORDER))
    return ", ".join("%s=%d" % (name, int(sources.get(name) or 0))
                     for name in names)


def book_uuid(database, book_id, item=None, sources=None, rejected=None):
    """Find a real UUID in metadata or supported Calibre database sources."""
    invalid = set()

    def check(name, value):
        text = normalize_uuid_text(value)
        if not text:
            return ""
        if not is_real_uuid(text):
            invalid.add(text)
            return ""
        _count_source(sources, name)
        return text

    if item is not None:
        text = check("metadata", metadata_uuid(item))
        if text:
            return text
    for name, value in database_uuid_values(database, book_id):
        text = check(name, value)
        if text:
            return text
    if invalid:
        _count_source(sources, "ungueltig")
        if isinstance(rejected, dict):
            for text in invalid:
                rejected[text] = rejected.get(text, 0) + 1
    _count_source(sources, "fallback")
    return ""


def migrate_state_keys(metadata_by_id, state):
    """Migrate fallback state keys to real UUIDs when the UUID becomes known."""
    if not isinstance(metadata_by_id, dict) or not isinstance(state, dict):
        return 0
    migrated = 0
    for book_id, metadata in metadata_by_id.items():
        if not isinstance(metadata, dict):
            continue
        book_uuid = valid_book_uuid(metadata.get("uuid"))
        if not book_uuid or book_uuid in state:
            continue
        old_key = state_key_for(state, book_id)
        if not old_key or old_key == book_uuid:
            continue
        entry = state.pop(old_key)
        if isinstance(entry, dict):
            entry["calibre_id"] = book_id
        state[book_uuid] = entry
        migrated += 1
    return migrated


def state_key_for(state, book_id):
    """Find a stored state key by its recorded Calibre book id."""
    if not isinstance(state, dict):
        return ""
    for key, entry in state.items():
        if not isinstance(entry, dict):
            continue
        recorded = entry.get("calibre_id")
        try:
            if recorded is not None and int(recorded) == int(book_id):
                return str(key)
        except (TypeError, ValueError):
            continue
    return ""
