"""Book identifier normalization."""
import re

_UUID_TEXT = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# Calibre's database path may return 'dummy' when the field is absent.
_UUID_PLACEHOLDERS = frozenset(("none", "null", "undefined", "dummy"))
def normalize_uuid_text(value):
    """Normalize a UUID-shaped value to trimmed text; no byte/repr parsing."""
    return str(value or "").strip().casefold()


def valid_book_uuid(value):
    """Return a usable Calibre book key, or empty for junk/incomplete data."""
    text = normalize_uuid_text(value)
    if not text or text in _UUID_PLACEHOLDERS:
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


def state_entry(state, key):
    """Zustandseintrag eines Buchs -- fremde Formen zählen als leer."""
    entry = state.get(key) if key and isinstance(state, dict) else None
    return entry if isinstance(entry, dict) else {}
