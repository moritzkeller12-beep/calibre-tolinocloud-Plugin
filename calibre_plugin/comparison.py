"""Conservative Calibre-to-tolino inventory comparison and sync signatures."""
import hashlib

from .identifiers import valid_book_uuid
from .library import metadata_tolino_id, normalize_formats
from .matching import (author_tokens, authors_match, normalize_isbn,
                       normalize_match_text, normalize_title,
                       normalized_authors)

# Hinweis einer Zeile, deren Buch in Calibre vom Abgleich ausgenommen ist.
IGNORED_EXPLANATION = (
    "In Calibre als «ignoriert» markiert (#tolino_ignore) – wird weder "
    "hochgeladen noch abgeglichen.")


def is_valid_isbn(value):
    """True for an ISBN-10/ISBN-13 with a correct check digit.

    Only a valid ISBN may identify a book automatically; arbitrary digits
    in the ISBN field are not a unique key.
    """
    digits = normalize_isbn(value).upper()
    if len(digits) == 10:
        total = 0
        for index, character in enumerate(digits):
            if character == "X":
                if index != 9:
                    return False
                digit = 10
            elif character.isdigit():
                digit = int(character)
            else:
                return False
            total += digit * (10 - index)
        return total % 11 == 0
    if len(digits) == 13 and digits.isdigit():
        total = sum((1 if index % 2 == 0 else 3) * int(character)
                    for index, character in enumerate(digits))
        return total % 10 == 0
    return False


def _metadata_text(metadata, name):
    return str(metadata.get(name) or "")


def _file_changed(metadata, state_entry, format_name):
    """True, wenn sich die Buchdatei seit dem letzten Sync geändert hat.

    Nur aussagekräftig, wenn beide Seiten einen Content-Fingerabdruck
    haben; sonst False – die Oberfläche schlägt dann Metadaten statt Upload vor.
    """
    if not format_name:
        return False
    current = content_fingerprint(metadata, format_name)
    previous = str(state_entry.get("content_fp") or "")
    return bool(current and previous and current != previous)


def comparison_group(status):
    """UI-Gruppe einer Vergleichszeile (stabil und ohne Qt)."""
    if status in ("new_in_calibre", "changed"):
        return "action"
    if status in ("review", "duplicate_tolino", "not_matchable"):
        return "review"
    if status == "identical":
        return "unchanged"
    return "tolino_only"


def _row_explanation(status, match_reason, file_changed):
    """Klartext-Hinweis für eine Bestandszeile."""
    if match_reason == "cloud_id_missing":
        return ("In tolino vorhanden, aber nicht ansprechbar (kein Download, "
                "kein Löschen). Ein Upload würde ein Duplikat erzeugen.")
    if match_reason == "duplicate_title":
        return "Titel mehrfach in tolino."
    if match_reason == "title_only":
        return ("Nur der Titel stimmt überein. Ein Upload würde vermutlich "
                "ein Duplikat erzeugen.")
    if match_reason == "ambiguous":
        return "Mehrere mögliche Treffer in tolino."
    if match_reason == "conflict":
        return "Autor oder ISBN weichen ab."
    if file_changed:
        return "Buchdatei unterscheidet sich – Hochladen lädt die neue Datei."
    if status == "identical":
        if match_reason == "title_author":
            return "Titel und Autor stimmen mit tolino überein."
        return ("In beiden vorhanden, keine Unterschiede in den "
                "geprüften Angaben gefunden.")
    if status == "new_in_calibre":
        return "Noch nicht in tolino – kann hochgeladen werden."
    return "Nur Metadaten unterscheiden sich – kein erneuter Upload nötig."


def _same_comparison_fields(metadata, remote):
    """True, wenn kein vergleichbares Feld der Cloud-Kopie widerspricht.

    Verglichen wird nur, was beide Seiten liefern. Fehlt auf einer Seite
    Titel, Autor oder ISBN, blockiert das Feld den Abgleich nicht -- ein
    fehlender Wert ist kein Unterschied.
    """
    values = {
        "title": _metadata_text(metadata, "title"),
        "authors": _metadata_text(metadata, "authors"),
        "isbn": _metadata_text(metadata, "isbn"),
    }
    for field, local in values.items():
        remote_value = remote.get(field)
        if not str(local or "").strip() or not str(remote_value or "").strip():
            continue
        if field == "isbn":
            if normalize_isbn(local) != normalize_isbn(remote_value):
                return False
        elif normalize_match_text(local) != normalize_match_text(remote_value):
            return False
    return True


class ReadState:
    """Lesestatus einer Vergleichszeile.

    Vier Fakten an einem Ort: ``known`` (Cloud-Antwort lesbar?), ``cloud``
    (Stand in tolino), ``chosen`` (Auswahl im Fenster) und ``local``
    (Calibre-Spalte, reine Anzeige). ``changed`` ist die einzige Regel,
    nach der übertragen wird.
    """

    __slots__ = ("known", "cloud", "chosen", "local")

    def __init__(self, known=True, cloud=False, chosen=None, local=None):
        self.known = bool(known)
        self.cloud = bool(cloud)
        self.chosen = self.cloud if chosen is None else bool(chosen)
        self.local = local

    @property
    def changed(self):
        """Übertragen wird nur eine Änderung -- und nur bei bekanntem Stand."""
        return self.known and self.chosen != self.cloud

    def __bool__(self):
        return self.chosen

    def __repr__(self):
        return ("ReadState(known=%s, cloud=%s, chosen=%s, local=%s)"
                % (self.known, self.cloud, self.chosen, self.local))


def _read_flags(tolino_id, read_known, read_ids):
    """Lesestatus einer Zeile: Cloud-Stand und Auswahl in einem Objekt."""
    is_read = read_known and bool(tolino_id) and str(tolino_id) in read_ids
    return ReadState(known=read_known, cloud=is_read)


class _RemoteIndex:
    """Cloud-Zeilen plus Lookup-Indizes -- in derselben Reihenfolge.

    Die Zuordnung suchte frueher fuer JEDES Buch die ganze Cloud-Liste
    erneut ab (quadratisch). Die Listen in den Indizes enthalten dieselben
    Zeilen in derselben Reihenfolge; `matched` entscheidet weiterhin, ob
    eine Zeile noch frei ist.
    """

    __slots__ = ("rows", "by_tolino_id", "by_isbn", "by_title",
                 "by_title_author", "title_counts")

    def __init__(self, rows):
        self.rows = rows
        self.by_tolino_id = {}
        self.by_isbn = {}
        self.by_title = {}
        self.by_title_author = {}
        self.title_counts = {}
        for index, row in enumerate(rows):
            if row["tolino_id"]:
                self.by_tolino_id.setdefault(row["tolino_id"], []).append(index)
            row_isbn = normalize_isbn(row["isbn"])
            if row_isbn and is_valid_isbn(row_isbn):
                self.by_isbn.setdefault(row_isbn, []).append(index)
            if not row["normalized_title"]:
                continue
            title = row["normalized_title"]
            self.title_counts[title] = self.title_counts.get(title, 0) + 1
            self.by_title.setdefault(title, []).append(index)
            author_key = normalized_authors(row["authors"])
            if author_key:
                self.by_title_author.setdefault((title, author_key), []).append(index)


def _remote_rows(inventory, read_known, read_ids):
    """Normalisierte Cloud-Datensätze in Vergleichszeilen umwandeln."""
    rows = []
    for record in inventory:
        # Auch Datensätze ohne eindeutige Cloud-ID bleiben sichtbar;
        # andernfalls erschiene das Buch als neu und könnte ein Duplikat erzeugen.
        tolino_id = (record["tolino_id"]
                     if record["has_deliverable_id"] else "")
        rows.append({
            "book_id": None,
            "title": record["title"],
            "authors": record["authors"],
            "isbn": record["isbn"],
            "tolino_id": tolino_id,
            "cloud_id_missing": not record["has_deliverable_id"],
            "status": "only_tolino",
            "file_changed": False,
            "read": _read_flags(tolino_id, read_known, read_ids),
            "explanation": "Nur in tolino – kann heruntergeladen werden.",
            "normalized_title": normalize_title(record["title"]),
        })
    return rows


def _find_remote_match(index, stored_id, local_isbn, title_key,
                       author_key, matched):
    """Ersten eindeutigen Treffer in der dokumentierten Reihenfolge finden.

    Rueckgabe ``(row_index, reason, ambiguous, candidates)``. Ein Treffer
    verbraucht die Zeile erst beim Aufrufer; mehrere freie Treffer auf
    denselben Schluessel sind ein Prueffall und werden nicht nach
    Listenposition ausgewaehlt.
    """
    attempts = []
    if stored_id:
        attempts.append((index.by_tolino_id.get(stored_id, ()), "stored_id"))
    if is_valid_isbn(local_isbn):
        attempts.append((index.by_isbn.get(local_isbn, ()), "isbn"))
    if title_key and author_key:
        attempts.append((index.by_title_author.get((title_key, author_key), ()),
                         "title_author"))
    for indexes, reason in attempts:
        free = [item for item in indexes if item not in matched]
        if len(free) == 1:
            return free[0], reason, False, []
        if len(free) > 1:
            return None, "ambiguous", True, free
    return None, "", False, []


def _metadata_conflict(remote, authors, author_key, local_isbn):
    """Autor oder ISBN widersprechen sich; die Zuordnung bleibt ungeklärt."""
    remote_authors = remote["authors"]
    if (author_key and author_tokens(remote_authors)
            and not authors_match(authors, remote_authors)):
        return True
    remote_isbn = normalize_isbn(remote["isbn"])
    return (is_valid_isbn(local_isbn) and is_valid_isbn(remote_isbn)
            and local_isbn != remote_isbn)


def _matched_status(metadata, remote, old, selected_format,
                    authors, author_key, local_isbn):
    """Status einer eindeutig zugeordneten Zeile (Dateisignatur zuerst)."""
    # Die Dateisignatur entscheidet VOR dem Status: gleiche Metadaten
    # duerfen eine geänderte Buchdatei nicht als «keine Unterschiede»
    # verdecken.
    file_changed = _file_changed(metadata, old, selected_format)
    if not remote["tolino_id"]:
        # Die Cloud-Kopie ist nicht ansprechbar. Ein Upload könnte ein Duplikat
        # erzeugen; daher bleibt die Zeile zur Prüfung.
        return "review", "cloud_id_missing", file_changed
    if _metadata_conflict(remote, authors, author_key, local_isbn):
        return "review", "conflict", file_changed
    same_fields = _same_comparison_fields(metadata, remote)
    current_meta_fp = metadata_fingerprint(metadata)
    # Nur ein nach PUT per GET bestaetigter Patch darf explizite Inventarwerte
    # ueberstimmen.
    verified_metadata_matches = (
        bool(old.get("meta_verified_fp"))
        and old.get("meta_verified_fp") == current_meta_fp)
    unchanged = (verified_metadata_matches or same_fields) and not file_changed
    return ("identical" if unchanged else "changed"), "", file_changed


def _title_only_match(remote, title_key, matched, disputed):
    """Title-only rows are hints; they never consume a cloud row."""
    title_candidates = remote.by_title.get(title_key, ()) if title_key else ()
    free_titles = [item for item in title_candidates if item not in matched]
    if not free_titles:
        return "new_in_calibre", ""
    disputed.update(free_titles)
    return ("review", "ambiguous" if len(free_titles) > 1 else "title_only")


def _local_comparison_row(book_id, metadata, remote, state, preferred_formats,
                          use_metadata_ids, matched,
                          disputed, read_known, read_ids, read_column=False):
    """Eine lokale Vergleichszeile -- inklusive einer bestaetigten Zuordnung."""
    # Ausschluss aus der Calibre-Spalte: das Buch wird weiter zugeordnet
    # (sonst erschiene seine Cloud-Kopie als «nur in tolino»), aber es
    # bekommt keine Aktion.
    ignored = bool(metadata.get("ignored"))
    book_uuid = valid_book_uuid(metadata.get("uuid"))
    title = _metadata_text(metadata, "title")
    authors = _metadata_text(metadata, "authors")
    isbn = _metadata_text(metadata, "isbn")
    old = state.get(book_uuid, {}) if book_uuid else {}
    stored_id = str(old.get("tolino_id") or
                    (metadata_tolino_id(metadata) if use_metadata_ids else "")
                    or "").strip()
    title_key = normalize_title(title)
    author_key = normalized_authors(authors)
    local_formats = normalize_formats(metadata.get("formats"))
    selected_format = next((fmt for fmt in preferred_formats
                            if fmt in local_formats), None)
    local_isbn = normalize_isbn(isbn)
    remote_index, match_reason, ambiguous, candidates = _find_remote_match(
        remote, stored_id, local_isbn, title_key, author_key, matched)
    file_changed = False
    tolino_id = ""
    status = "new_in_calibre"
    if remote_index is not None:
        matched.add(remote_index)
        remote_row = remote.rows[remote_index]
        tolino_id = remote_row["tolino_id"]
        status, override, file_changed = _matched_status(
            metadata, remote_row, old, selected_format,
            authors, author_key, local_isbn)
        if override:
            match_reason = override
        if book_uuid and tolino_id:
            # Die Calibre-Nummer mitschreiben: sie findet den Eintrag
            # wieder, falls Calibre spaeter keine lesbare Kennung mehr
            # liefert. Ohne Kennung wird KEIN Zustand angelegt -- eine
            # unbestätigte Prueffzeile darf keine Zuordnung vortäuschen.
            entry = state.setdefault(book_uuid, {})
            entry["tolino_id"] = tolino_id
            entry.setdefault("calibre_id", book_id)
    elif ambiguous:
        status = "review"
        disputed.update(candidates)
    else:
        status, match_reason = _title_only_match(
            remote, title_key, matched, disputed)

    row = {
        "book_id": book_id,
        "uuid": book_uuid or str(metadata.get("uuid") or ""),
        "title": title,
        "authors": authors,
        "isbn": isbn,
        "tolino_id": tolino_id,
        "stale_cloud_id": (stored_id if status == "new_in_calibre"
                            and not tolino_id and stored_id else ""),
        "ignored": ignored,
        "status": status,
        "group": "ignored" if ignored else comparison_group(status),
        "match_reason": match_reason,
        "file_changed": file_changed,
        "has_preferred_format": bool(selected_format),
        "read": _read_flags(tolino_id, read_known, read_ids),
        "explanation": (IGNORED_EXPLANATION if ignored
                        else _row_explanation(status, match_reason,
                                              file_changed)),
    }
    if read_column:
        row["read_column"] = True
        local = metadata.get("calibre_read")
        row["read"].local = None if local is None else bool(local)
    return row


def _leftover_remote_row(row, disputed, title_counts, row_index):
    """Cloud-Zeile, die keine lokale Zeile eindeutig beansprucht hat."""
    if row_index in disputed:
        return dict(row, status="review", group="review",
                    explanation="Möglicher Treffer, nicht eindeutig.")
    if row.get("cloud_id_missing"):
        return dict(row, status="review", group="review",
                    explanation=("In tolino vorhanden, aber nicht ansprechbar "
                                 "(kein Download, kein Löschen)."))
    if not row["title"]:
        return dict(row, status="not_matchable", group="review",
                    explanation=(
                        "tolino-Datensatz ohne Titel – nicht zuordenbar."
                    ))
    if title_counts.get(row["normalized_title"], 0) > 1:
        return dict(row, status="duplicate_tolino", group="review",
                    explanation="Titel mehrfach in tolino – nicht automatisch hochladen.")
    return row


def compare_inventory(metadata_by_id, state, inventory, preferred_formats=(),
                      use_metadata_ids=True, read_ids=(), read_column=False):
    """Return deterministic local/remote rows for the pre-sync confirmation view.

    Matching is deliberately conservative and ordered:

    1. the stored tolino ID (state or ``#tolino_id`` column),
    2. the Calibre UUID,
    3. one unique *valid* ISBN,
    4. one unique normalized title+author pair.

    A title on its own never matches: it produces a ``review`` row for a
    human decision and leaves the cloud record untouched, so no automatic
    upload and no ``identical`` can be derived from a title alone. Multiple
    hits on the same lookup key are marked ``review`` as well; no hit is
    picked by list position. Conflicting authors or ISBNs also stay
    ``review`` so that the user confirms the assignment.

    `read_ids` are tolino IDs already marked as read in the selected account.
    They prefill the dialog's Cloud-status selection; Calibre's read column is
    attached to each row as display-only information.
    ``None`` bedeutet «Status unbekannt» (Abruf fehlgeschlagen): die Zeilen
    tragen dann ``read_known=False``, damit die Oberfläche «unbekannt» nicht
    als «ungelesen» darstellt und nichts zurückschreibt.

    ``inventory`` enthält die von `TolinoClient.inventory()` gelieferten,
    bereits normalisierten Cloud-Datensätze.

    ``read_column`` adds the informational Calibre ``#tolino_read`` value;
    it never changes the selected account's Cloud read status.
    """
    preferred_formats = normalize_formats(preferred_formats)
    read_known = read_ids is not None
    read_ids = ({str(item) for item in read_ids if str(item or "").strip()}
                if read_known else set())
    rows = _remote_rows(inventory, read_known, read_ids)
    remote = _RemoteIndex(rows)
    matched = set()
    disputed = set()
    local_rows = []
    for book_id, metadata in metadata_by_id.items():
        local_rows.append(_local_comparison_row(
            book_id, metadata, remote, state, preferred_formats,
            use_metadata_ids, matched, disputed,
            read_known, read_ids, read_column))
    for index, row in enumerate(remote.rows):
        if index not in matched:
            local_rows.append(_leftover_remote_row(
                row, disputed, remote.title_counts, index))
    return local_rows

def comparison_sort_key(row):
    """Sortierschlüssel des Bestandsvergleichs: Aktionen zuerst, dann Titel."""
    return (
        0 if row.get("upload") else 1,
        0 if row.get("metadata") else 1,
        str(row.get("title") or "").casefold(),
        str(row.get("authors") or "").casefold(),
        str(row.get("isbn") or ""),
    )


def sort_comparison_rows(comparison_rows):
    """Bestandsvergleich sortieren: erst ausgewählte Aktionen, dann Titel, Autoren.

    Zeilen mit einer Aktion ("upload"/"metadata") stehen oben.
    """
    return sorted(comparison_rows, key=comparison_sort_key)


def metadata_fingerprint(metadata):
    """Hash of the fields pushed in place to tolino instead of uploaded."""
    value = "%s|%s|%s|%s" % (
        metadata.get("title", ""),
        metadata.get("authors", ""),
        metadata.get("isbn", ""),
        metadata.get("series", ""),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def content_fingerprint(metadata, format_name):
    """Hash of the format FILE itself (uuid|format|size:mtime); '' when the
    signature was never collected – an unknown file must never be mistaken
    for an unchanged one, so '' blocks in-place metadata updates."""
    signatures = metadata.get("format_signatures")
    signature = (str(signatures.get(format_name) or "")
                 if isinstance(signatures, dict) else "")
    if not signature:
        return ""
    value = "%s|%s|%s" % (metadata.get("uuid", ""), format_name, signature)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


