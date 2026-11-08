"""Book identifier normalization."""
import copy
import re

_UUID_TEXT = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# New state keys always carry a library scope. The unscoped
# `calibre-<id>` form is accepted only by the one-time legacy migration.
_SCOPED_CALIBRE_KEY = re.compile(r"calibre-[0-9a-f]{12}-\d+")
_LEGACY_CALIBRE_KEY = re.compile(r"calibre-\d+")
# One account-wide decision for state keys that predate library scopes.
LEGACY_MIGRATION_KEY = "__legacy_calibre_migration__"
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


def legacy_calibre_key(book_id):
    """Old unscoped key, used only to read pre-scope state."""
    number = _book_number(book_id)
    return "calibre-%s" % number if number else ""


def _legacy_marker(state):
    marker = state.get(LEGACY_MIGRATION_KEY) if isinstance(state, dict) else None
    return marker if isinstance(marker, dict) else {}


def is_legacy_calibre_key(value):
    """True for the old unscoped ``calibre-<id>`` fallback only."""
    return bool(_LEGACY_CALIBRE_KEY.fullmatch(normalize_uuid_text(value)))


def legacy_calibre_keys(state):
    """Old fallback keys that carry no library identity."""
    if not isinstance(state, dict):
        return []
    return [key for key, value in state.items()
            if isinstance(value, dict) and is_legacy_calibre_key(key)]


def needs_legacy_migration(state, library_scope):
    """Does this library still need a one-time legacy-state decision?"""
    scope = _scope_text(library_scope)
    if not scope or not legacy_calibre_keys(state):
        return False
    marker = _legacy_marker(state)
    marked_scope = _scope_text(marker.get("scope"))
    if marked_scope == scope:
        return False
    if marked_scope and not marker.get("declined"):
        return False
    return True


def decline_legacy_migration(state, library_scope):
    """Remember that this library does not claim the unscoped old state."""
    scope = _scope_text(library_scope)
    if (not scope or not isinstance(state, dict)
            or not legacy_calibre_keys(state)):
        return False
    marker = _legacy_marker(state)
    marked_scope = _scope_text(marker.get("scope"))
    if marked_scope and marked_scope != scope and not marker.get("declined"):
        return False
    state[LEGACY_MIGRATION_KEY] = {
        "scope": scope, "declined": True, "ids": [],
    }
    return True


def migrate_legacy_calibre_keys(state, metadata, library_scope="",
                                allow_legacy=True):
    """Copy old ``calibre-<id>`` entries to this library once, after consent.

    The old key carries no library identity. It is adopted only when the
    recorded Calibre number matches the current book, and only after the
    library claimed it. A marker makes the decision account-wide: another
    library cannot copy the same entry, and deleting the scoped entry later
    does not resurrect it from the old key. The marker is recorded even when
    no book matches, so a confirmed claim is not asked again; the old key
    itself stays for older plugin versions and is never claimed twice.
    """
    scope = _scope_text(library_scope)
    if (not allow_legacy or not scope or not isinstance(state, dict)
            or not isinstance(metadata, dict)
            or not legacy_calibre_keys(state)):
        return False
    marker = _legacy_marker(state)
    marked_scope = _scope_text(marker.get("scope"))
    if marked_scope and marked_scope != scope and not marker.get("declined"):
        return False
    if marked_scope == scope and marker.get("declined"):
        return False
    claimed = ({str(value) for value in (marker.get("ids") or ())}
               if marked_scope == scope else set())
    changed = False
    for book_id, item in metadata.items():
        if not isinstance(item, dict):
            continue
        key = valid_book_uuid(item.get("uuid"))
        legacy = legacy_calibre_key(book_id)
        if not key or not key.startswith("calibre-") or key == legacy:
            continue
        if str(book_id) in claimed:
            continue
        old = state.get(legacy)
        if not isinstance(old, dict):
            continue
        recorded = old.get("calibre_id")
        if recorded is not None and str(recorded) != str(book_id):
            continue
        if key not in state:
            state[key] = copy.deepcopy(old)
        claimed.add(str(book_id))
        changed = True
    state[LEGACY_MIGRATION_KEY] = {
        "scope": scope, "declined": False,
        "ids": sorted(claimed),
    }
    return changed


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
