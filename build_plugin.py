"""Build a deterministic Calibre plugin archive without Calibre installed."""
import ast
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


def strip_comments(data):
    """Kommentare entfernen, Zeilennummern und Kodierung aber behalten.

    Docstrings bleiben in dieser Stufe stehen; strip_source entfernt sie
    zusätzlich. Shebang und Kodierzeile bleiben erhalten. Lässt sich die
    Datei nicht zerlegen, wird sie unverändert ausgeliefert.
    """
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        text = data.decode(encoding)
        columns = {}
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                columns.setdefault(token.start[0], token.start[1])
    except (LookupError, SyntaxError, UnicodeDecodeError, tokenize.TokenError):
        return data
    lines = text.splitlines(keepends=True)
    for row, column in columns.items():
        line = lines[row - 1]
        if column == 0 and row <= 2 and (
                line.startswith("#!") or "coding" in line):
            continue
        ending = "\r\n" if line.endswith("\r\n") else (
            "\n" if line.endswith("\n") else "")
        lines[row - 1] = line[:column].rstrip() + ending
    return "".join(lines).encode(encoding)


def strip_docstrings(data):
    """Docstrings entfernen, Zeilen und Spalten aber unverändert lassen."""
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        text = data.decode(encoding)
        tree = ast.parse(text)
    except (LookupError, SyntaxError, UnicodeDecodeError, tokenize.TokenError):
        return data
    lines = text.splitlines(keepends=True)
    starts, offset = [], 0
    for line in lines:
        starts.append(offset)
        offset += len(line)
    spans = []
    docstring_nodes = (ast.Module, ast.ClassDef, ast.FunctionDef,
                       ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, docstring_nodes) or not node.body:
            continue
        first = node.body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            value = first.value
            spans.append((value.lineno, value.col_offset,
                          value.end_lineno, value.end_col_offset))
    for start_line, start_col, end_line, end_col in sorted(spans, reverse=True):
        start = starts[start_line - 1] + len(
            lines[start_line - 1].encode("utf-8")[:start_col].decode("utf-8"))
        end = starts[end_line - 1] + len(
            lines[end_line - 1].encode("utf-8")[:end_col].decode("utf-8"))
        segment = text[start:end]
        text = (text[:start]
                + "0" + "".join("\n" if char == "\n" else
                                  "\r" if char == "\r" else " "
                                  for char in segment[1:])
                + text[end:])
    return text.encode(encoding)


def strip_source(data):
    """Kommentare und Docstrings für das Release entfernen."""
    return strip_comments(strip_docstrings(data))


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
