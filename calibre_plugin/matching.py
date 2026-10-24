"""Pure text normalization and author matching for sync planning."""
import re
import unicodedata
from functools import lru_cache


@lru_cache(maxsize=4096)
def _normalize_match_text_text(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    parts = []
    for character in value:
        category = unicodedata.category(character)
        if character.isspace() or category[0] in ("P", "S"):
            parts.append(" ")
        else:
            parts.append(character)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def normalize_match_text(value):
    """Normalize human-readable matching text without dropping Unicode letters."""
    return _normalize_match_text_text(str(value or ""))


@lru_cache(maxsize=4096)
def _normalize_title_text(value):
    title = unicodedata.normalize("NFKC", value).strip()
    title = re.sub(r"\.(?:epub|pdf|mobi|azw3|azw|fb2|txt|html?)\s*$",
                   "", title, flags=re.IGNORECASE)
    return normalize_match_text(title)


def normalize_title(value):
    """Normalize a title, ignoring only a clearly technical file extension."""
    return _normalize_title_text(str(value or ""))


@lru_cache(maxsize=4096)
def _normalize_isbn_text(value):
    return re.sub(r"[^0-9x]+", "", value)


def normalize_isbn(value):
    """Remove ISBN separators while preserving its digits and check digit."""
    return _normalize_isbn_text(str(value or "").casefold())


_AUTHOR_SEPARATORS = re.compile(r"[\W_]+")
_AUTHOR_FILLER = {"and", "und", "et", "y", "with", "mit"}


@lru_cache(maxsize=4096)
def _author_tokens_text(value):
    """Tokenize names ignoring order, accents and punctuation."""
    text = value.casefold()
    for umlaut, plain in (("\u00df", "ss"), ("\u00e4", "a"), ("\u00f6", "o"),
                          ("\u00fc", "u"), ("\u0142", "l"), ("\u00f8", "o")):
        text = text.replace(umlaut, plain)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(character for character in text
                   if not unicodedata.combining(character))
    text = text.replace("ae", "a").replace("oe", "o").replace("ue", "u")
    tokens = [token for token in _AUTHOR_SEPARATORS.split(text) if token]
    return tuple(sorted({token for token in tokens
                         if token not in _AUTHOR_FILLER}))


def author_tokens(value):
    """Return stable, sorted name tokens; accepts list-like metadata too."""
    return _author_tokens_text(str(value or ""))


def authors_match(left, right):
    """Match names across order, accent and initial/full-name differences."""
    left_tokens, right_tokens = author_tokens(left), author_tokens(right)
    if not left_tokens or not right_tokens:
        return False
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


def normalized_authors(value):
    """Return a stable author lookup key."""
    return " ".join(author_tokens(value))
