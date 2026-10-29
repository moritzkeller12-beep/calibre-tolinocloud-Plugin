"""Pure text normalization and author matching for sync planning."""
import re
import unicodedata


def normalize_match_text(value):
    """Normalize human-readable matching text without dropping Unicode letters."""
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    parts = []
    for character in value:
        category = unicodedata.category(character)
        if character.isspace() or category[0] in ("P", "S"):
            parts.append(" ")
        else:
            parts.append(character)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def normalize_title(value):
    """Normalize a title, ignoring only a clearly technical file extension."""
    title = unicodedata.normalize("NFKC", str(value or "")).strip()
    title = re.sub(r"\.(?:epub|pdf|mobi|azw3|azw|fb2|txt|html?)\s*$",
                   "", title, flags=re.IGNORECASE)
    return normalize_match_text(title)


def normalize_isbn(value):
    """Remove ISBN separators while preserving its digits and check digit."""
    return re.sub(r"[^0-9x]+", "", str(value or "").casefold())


_AUTHOR_SEPARATORS = re.compile(r"[\W_]+")
_AUTHOR_FILLER = {"and", "und", "et", "y", "with", "mit"}


def _has_umlaut(value):
    """True, wenn der Text einen echten Umlaut enthält (nicht «ue»)."""
    text = str(value or "").casefold()
    return any(character in text for character in ("ä", "ö", "ü"))


def author_tokens(value, transliterate=True):
    """Return stable, sorted name tokens; accepts list-like metadata too.

    ``transliterate`` schaltet zusätzlich ae→a, oe→o, ue→u ein. Der Ersatz
    ist mehrdeutig («Joe» und «Jo» ergäben beide «jo»), deshalb wird er nur
    verglichen, wenn eine Schreibweise tatsächlich einen Umlaut enthält.
    """
    text = str(value or "").casefold()
    for umlaut, plain in (("\u00df", "ss"), ("\u00e4", "a"), ("\u00f6", "o"),
                          ("\u00fc", "u"), ("\u0142", "l"), ("\u00f8", "o")):
        text = text.replace(umlaut, plain)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(character for character in text
                   if not unicodedata.combining(character))
    if transliterate:
        text = text.replace("ae", "a").replace("oe", "o").replace("ue", "u")
    tokens = [token for token in _AUTHOR_SEPARATORS.split(text) if token]
    return tuple(sorted({token for token in tokens
                         if token not in _AUTHOR_FILLER}))


def _tokens_match(left_tokens, right_tokens):
    """Token-Vergleich: Reihenfolge, Initialen und Teilnamen."""
    if left_tokens == right_tokens:
        return True

    def consistent(extra, other):
        for token in extra:
            if token in other:
                continue
            if len(token) == 1:
                if any(item.startswith(token) for item in other):
                    continue
                return False
            if any(len(item) == 1 and token.startswith(item) for item in other):
                continue
            return False
        return True

    return (consistent(set(left_tokens) - set(right_tokens), set(right_tokens))
            and consistent(set(right_tokens) - set(left_tokens),
                           set(left_tokens)))


def authors_match(left, right):
    """Match names across order, accent and initial/full-name differences.

    Zuerst ohne ae/oe/ue-Ersatz («Joe» ≠ «Jo»); nur wenn eine Seite einen
    echten Umlaut hat, wird zusätzlich die ersetzte Schreibweise verglichen
    («Müller» = «Mueller»).
    """
    left_tokens = author_tokens(left, transliterate=False)
    right_tokens = author_tokens(right, transliterate=False)
    if not left_tokens or not right_tokens:
        return False
    if _tokens_match(left_tokens, right_tokens):
        return True
    if _has_umlaut(left) or _has_umlaut(right):
        return _tokens_match(author_tokens(left), author_tokens(right))
    return False


def normalized_authors(value):
    """Return a stable author lookup key."""
    return " ".join(author_tokens(value))
