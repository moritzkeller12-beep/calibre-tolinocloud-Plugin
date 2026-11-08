"""Book identifier normalization."""
import re

_UUID_TEXT = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# State keys for books without a real UUID always carry a library scope.
_SCOPED_CALIBRE_KEY = re.compile(r"calibre-[0-9a-f]{12}-\d+")
# Calibre's database path may return 'dummy' when the field is absent.
_UUID_PLACEHOLDERS = frozenset(("none", "null", "undefined", "dummy"))
def normalize_uuid_text(value):
    """Normalize a UUID-shaped value to trimmed text; no byte/repr parsing."""
    return str(value or "").strip().casefold()


def valid_book_uuid(value):
    """Return a supported Calibre key, or empty for junk/incomplete data."""
    text = normalize_uuid_text(value)
    if not text or text in _UUID_PLACEHOLDERS:
        return ""
    if len(text) > 128 or any(character.isspace() for character in text):
        return ""
    if _UUID_TEXT.fullmatch(text) or _SCOPED_CALIBRE_KEY.fullmatch(text):
        return text
    return ""


def _scope_text(scope):
    return str(scope or "").strip().casefold()


def _book_number(book_id):
    return str(book_id if book_id is not None else "").strip()


def calibre_key(book_id, library_scope=""):
    """Stable scoped state key; empty without a book number or scope."""
    number = _book_number(book_id)
    scope = _scope_text(library_scope)
    if not number or not scope:
        return ""
    return "calibre-%s-%s" % (scope, number)


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


def adopt_column_tolino_ids(metadata_by_id, state):
    """Konto-IDs aus `#tolino_id` einmalig in den State übernehmen.

    Die Spalte ist nur eine Migrationsquelle für ältere Installationen. Ein
    bereits vorhandener State-Eintrag bleibt unangetastet -- auch wenn er
    keine `tolino_id` mehr enthält. So kann eine bewusst entfernte, veraltete
    Cloud-ID nicht aus der Spalte zurückkehren.
    """
    if not isinstance(metadata_by_id, dict) or not isinstance(state, dict):
        return False
    changed = False
    for book_id, item in metadata_by_id.items():
        if not isinstance(item, dict):
            continue
        key = valid_book_uuid(item.get("uuid"))
        column_id = str(item.get("tolino_id") or "").strip()
        if not key or not column_id or key in state:
            continue
        state[key] = {"tolino_id": column_id, "calibre_id": book_id}
        changed = True
    return changed
