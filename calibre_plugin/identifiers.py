"""Book identifier normalization."""
import re

_UUID_TEXT = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# State keys for books without a real UUID always carry a library scope.
_SCOPED_CALIBRE_KEY = re.compile(r"calibre-[0-9a-f]{12}-\d+")
# Calibre's database path may return 'dummy' when the field is absent.
_UUID_PLACEHOLDERS = frozenset(("none", "null", "undefined", "dummy"))


def _text(value):
    """Getrimmter Kleinbuchstaben-Text eines Werts; keine Byte-/Repr-Deutung."""
    return str(value or "").strip().casefold()


def valid_book_uuid(value):
    """Return a supported Calibre key, or empty for junk/incomplete data."""
    text = _text(value)
    if not text or text in _UUID_PLACEHOLDERS:
        return ""
    if len(text) > 128 or any(character.isspace() for character in text):
        return ""
    if _UUID_TEXT.fullmatch(text) or _SCOPED_CALIBRE_KEY.fullmatch(text):
        return text
    return ""


def book_uuid(item):
    """Canonical Calibre UUID from a metadata item or a plain value.

    Nur die echte UUID-Form zählt: ein Bibliotheksschlüssel (`calibre-…`)
    ist kein Ersatz, deshalb prüft diese Funktion `valid_book_uuid` nicht.
    """
    if isinstance(item, dict):
        raw = item.get("uuid")
    elif isinstance(item, str):
        raw = item
    else:
        raw = getattr(item, "uuid", None)
    text = _text(raw)
    return text if _UUID_TEXT.fullmatch(text) else ""


def _book_number(book_id):
    return str(book_id if book_id is not None else "").strip()


def calibre_key(book_id, library_scope=""):
    """Stable scoped state key; empty without a book number or scope."""
    number = _book_number(book_id)
    scope = _text(library_scope)
    if not number or not scope:
        return ""
    return "calibre-%s-%s" % (scope, number)


def display_title(title, number=None):
    """Prefer a human-readable title to an internal identifier in progress."""
    text = str(title or "").strip()
    if text:
        return text
    value = str(number if number is not None else "").strip()
    if value.isdigit():
        return "Buch %s" % value
    return "Buch ohne Titel"


def state_entry(state, key):
    """Zustandseintrag eines Buchs -- fremde Formen zählen als leer."""
    entry = state.get(key) if key and isinstance(state, dict) else None
    return entry if isinstance(entry, dict) else {}
