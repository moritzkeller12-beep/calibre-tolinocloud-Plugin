"""Build a deterministic Calibre plugin archive without Calibre installed."""
import io
import sys
import tokenize
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT, SOURCE = Path(__file__).parent, Path(__file__).parent / "calibre_plugin"
MARKER = "plugin-import-name-tolino_cloud_sync.txt"
ICON = "images/tolino_cloud_sync.png"
ROOT_FILES = ("__init__.py", "README.md", "CHANGELOG.md")
FILES = (*ROOT_FILES, *sorted(p.name for p in SOURCE.glob("*.py")
                              if p.name != "test_sync.py"), ICON, MARKER)
DATE, MODE = (2026, 1, 1, 0, 0, 0), 0o644 << 16
# Diese Token verschieben keine Anweisungsgrenze.
_SKIPPED = frozenset((tokenize.NL, tokenize.COMMENT, tokenize.INDENT,
                      tokenize.DEDENT))
_BROKEN = (LookupError, SyntaxError, UnicodeDecodeError, tokenize.TokenError)


def _docstring_spans(tokens):
    """Spans aller Docstrings: ein String, der allein eine Anweisung bildet.

    Die Tokenfolge ist in jeder Python-Version gleich; ``ast`` fuehrt
    Docstrings seit 3.14 nicht mehr als eigene Knoten.
    """
    spans, previous = [], tokenize.ENCODING
    for index, token in enumerate(tokens):
        following = tokens[index + 1].type if index + 1 < len(tokens) else None
        if (token.type == tokenize.STRING
                and previous in (tokenize.NEWLINE, tokenize.ENCODING)
                and following == tokenize.NEWLINE):
            spans.append((token.start, token.end))
        if token.type not in _SKIPPED:
            previous = token.type
    return spans


def _strip_comments(text, tokens):
    """Kommentare abschneiden; Shebang und Kodierzeile bleiben erhalten."""
    lines = text.splitlines(keepends=True)
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        row, column = token.start
        line = lines[row - 1]
        if row <= 2 and (line.startswith("#!") or "coding" in line):
            continue
        ending = "\r\n" if line.endswith("\r\n") else (
            "\n" if line.endswith("\n") else "")
        lines[row - 1] = line[:column].rstrip() + ending
    return "".join(lines)


def _blank_docstrings(text, spans):
    """Docstrings durch ``0`` ersetzen; die Zeilenzahl bleibt gleich.

    Das ``0`` haelt eine Klasse am Leben, deren Rumpf nur ein Docstring war.
    """
    starts, offset = [], 0
    for line in text.splitlines(keepends=True):
        starts.append(offset)
        offset += len(line)
    for (start_row, start_col), (end_row, end_col) in sorted(spans,
                                                             reverse=True):
        start = starts[start_row - 1] + start_col
        end = starts[end_row - 1] + end_col
        kept = "".join(char for char in text[start:end][1:] if char in "\r\n")
        text = text[:start] + "0" + kept + text[end:]
    return text


def strip_source(data):
    """Kommentare und Docstrings entfernen; die Zeilenzahl bleibt gleich."""
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        text = data.decode(encoding)
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
        # Erst Kommentare (ihre Spalten gelten fuer den Originaltext), dann
        # Docstrings (deren Spans aus denselben Tokens stammen).
        text = _blank_docstrings(_strip_comments(text, tokens),
                                 _docstring_spans(tokens))
    except _BROKEN:
        return data
    return text.encode(encoding)


def build_plugin(archive_path=ROOT / "tolino_cloud_sync.zip"):
    archive_path = Path(archive_path)
    with ZipFile(archive_path, "w", ZIP_DEFLATED, compresslevel=9) as archive:
        for name in FILES:
            path = ROOT / name if name in ROOT_FILES else SOURCE / name
            data = b"" if name == MARKER else path.read_bytes()
            # Nur die Module kürzen; __init__.py trägt den Lizenzhinweis.
            if path.parent == SOURCE and path.suffix == ".py":
                data = strip_source(data)
            info = ZipInfo(name, date_time=DATE)
            info.compress_type, info.external_attr = ZIP_DEFLATED, MODE
            archive.writestr(info, data, compress_type=ZIP_DEFLATED, compresslevel=9)
    print(f"Wrote {archive_path.name} ({len(FILES)} entries, "
          f"{archive_path.stat().st_size} bytes)")
    return archive_path


if __name__ == "__main__":
    build_plugin(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tolino_cloud_sync.zip")
