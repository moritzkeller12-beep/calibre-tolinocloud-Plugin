"""Conservative Calibre-to-tolino inventory comparison and sync signatures."""
import hashlib

from .identifiers import state_entry, valid_book_uuid
from .library import normalize_formats
from .matching import (author_tokens, authors_match, is_valid_isbn,
                       normalize_isbn, normalize_match_text, normalize_title,
                       normalized_authors)

IGNORED_EXPLANATION = (
    "In Calibre als «ignoriert» markiert (#tolino_ignore) – wird weder "
    "hochgeladen noch abgeglichen.")


def _metadata_text(metadata, name):
    return str(metadata.get(name) or "")


def _file_changed(metadata, previous_entry, format_name):
    """True, wenn sich die Buchdatei seit dem letzten Sync geändert hat.

    Nur aussagekräftig, wenn beide Seiten einen Content-Fingerabdruck
    haben; sonst False – die Oberfläche schlägt dann Metadaten statt Upload vor.
    """
    if not format_name:
        return False
    current = content_fingerprint(metadata, format_name)
    previous = str(previous_entry.get("content_fp") or "")
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


_REASON_EXPLANATIONS = {
    "cloud_id_missing": ("In tolino vorhanden, aber nicht ansprechbar "
                         "(kein Download, kein Löschen). Ein Upload würde "
                         "ein Duplikat erzeugen."),
    "title_only": ("Nur der Titel stimmt überein. Ein Upload würde "
                   "vermutlich ein Duplikat erzeugen."),
    "ambiguous": "Mehrere mögliche Treffer in tolino.",
    "conflict": "Autor oder ISBN weichen ab.",
}


def _row_explanation(status, match_reason, file_changed):
    """Klartext-Hinweis für eine Bestandszeile."""
    reason = _REASON_EXPLANATIONS.get(match_reason)
    if reason:
        return reason
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


def _read_flags(tolino_id, read_known, read_ids):
    """Lesestatus einer Zeile: Cloud-Stand und Auswahl in einem Objekt."""
    is_read = read_known and bool(tolino_id) and str(tolino_id) in read_ids
    return ReadState(known=read_known, cloud=is_read)


class ProgressState:
    """Lesefortschritt einer Vergleichszeile – nur Anzeige, nie Auswahl.

    ``known=False`` bedeutet, dass sync-data nicht gelesen werden konnte.
    Ein bekannter Stand ohne Bookmark hat ``percent=None`` und bleibt leer;
    das ist nicht dasselbe wie 0 Prozent.
    """

    __slots__ = ("known", "percent", "current", "last", "position")

    def __init__(self, known=True, percent=None, current="", last="",
                 position=""):
        self.known = bool(known)
        self.current = str(current or "").strip()
        self.last = str(last or "").strip()
        self.position = str(position or "").strip()
        try:
            value = int(round(float(percent)))
        except (TypeError, ValueError, OverflowError):
            value = None
        self.percent = value if value is not None and 0 <= value <= 100 else None

    @property
    def text(self):
        if not self.known:
            return "unbekannt"
        if self.percent is None:
            return ""
        text = "%d %%" % self.percent
        if self.current and self.last:
            text += " · %s/%s" % (self.current, self.last)
        return text

    def __bool__(self):
        return self.known and self.percent is not None


def _progress_state(tolino_id, progress_known, progress_by_id):
    """Fortschritt einer Zeile aus der sync-data-Antwort herauslösen."""
    if not progress_known:
        return ProgressState(known=False)
    item = (progress_by_id.get(str(tolino_id))
            if tolino_id and isinstance(progress_by_id, dict) else None)
    if not isinstance(item, dict):
        return ProgressState()
    return ProgressState(
        percent=item.get("percent"), current=item.get("current", ""),
        last=item.get("last", ""), position=item.get("position", ""))


_READING_STATUSES = frozenset(("identical", "changed"))


def reading_state(entry):
    """Den veränderbaren Lesebereich eines State-Eintrags sicher anlegen."""
    reading = entry.get("reading")
    if not isinstance(reading, dict):
        reading = {}
        entry["reading"] = reading
    return reading


def _matched_reading_rows(comparison):
    """Yield confirmed rows with both a Calibre and a tolino mapping."""
    for row in comparison or ():
        if (not isinstance(row, dict)
                or row.get("status") not in _READING_STATUSES):
            continue
        book_id = row.get("book_id")
        tolino_id = str(row.get("tolino_id") or "").strip()
        if book_id is None or not tolino_id:
            continue
        yield row, book_id, tolino_id


def comparison_read_updates(comparison):
    """Return confirmed (Calibre id, read?) updates for mapped rows."""
    updates = []
    for row, book_id, _tolino_id in _matched_reading_rows(comparison):
        read = row.get("read")
        if read is None or not getattr(read, "known", False):
            continue
        updates.append((book_id, bool(read.cloud)))
    return updates


def comparison_progress_updates(comparison):
    """Return confirmed (Calibre id, percent) updates for mapped rows."""
    updates = []
    for row, book_id, _tolino_id in _matched_reading_rows(comparison):
        progress = row.get("progress")
        if progress is None or not getattr(progress, "known", False):
            continue
        updates.append((book_id, progress.percent))
    return updates


def store_reading_state(state, comparison):
    """Store confirmed cloud reading values in the queried account state."""
    if not isinstance(state, dict):
        return
    for row, book_id, tolino_id in _matched_reading_rows(comparison):
        key = valid_book_uuid(row.get("uuid"))
        if not key:
            continue
        read = row.get("read")
        progress = row.get("progress")
        read_known = bool(read is not None and getattr(read, "known", False))
        progress_known = bool(progress is not None
                              and getattr(progress, "known", False))
        if not (read_known or progress_known):
            continue
        entry = state.get(key)
        if not isinstance(entry, dict):
            entry = {}
            state[key] = entry
        recorded = str(entry.get("tolino_id") or "").strip()
        if recorded and recorded != tolino_id:
            continue
        entry["tolino_id"] = tolino_id
        entry["calibre_id"] = book_id
        reading = reading_state(entry)
        if read_known:
            reading["read"] = bool(read.cloud)
        if progress_known:
            # Beim Kontowechsel wird nur der Prozentwert gebraucht;
            # Position und current/last liefert der frische Cloud-Stand.
            reading["progress"] = {"percent": progress.percent}


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


def _comparison_row(book_id, title, authors, isbn, tolino_id, status, read,
                    progress, explanation, group=None, **extra):
    """Common shape of a comparison row; callers add source-specific fields."""
    row = {
        "book_id": book_id,
        "title": title,
        "authors": authors,
        "isbn": isbn,
        "tolino_id": tolino_id,
        "status": status,
        "group": comparison_group(status) if group is None else group,
        "read": read,
        "progress": progress,
        "explanation": explanation,
    }
    row.update(extra)
    return row


def _remote_rows(inventory, read_known, read_ids, progress_known,
                 progress_by_id):
    """Normalisierte Cloud-Datensätze in Vergleichszeilen umwandeln."""
    rows = []
    for record in inventory:
        # Auch Datensätze ohne eindeutige Cloud-ID bleiben sichtbar;
        # andernfalls erschiene das Buch als neu und könnte ein Duplikat erzeugen.
        tolino_id = record["tolino_id"]
        rows.append(_comparison_row(
            None, record["title"], record["authors"], record["isbn"],
            tolino_id, "only_tolino",
            _read_flags(tolino_id, read_known, read_ids),
            _progress_state(tolino_id, progress_known, progress_by_id),
            "Nur in tolino – kann heruntergeladen werden.",
            cloud_id_missing=not bool(tolino_id), file_changed=False,
            normalized_title=normalize_title(record["title"])))
    return rows


def _find_remote_match(index, stored_id, local_isbn, title_key,
                       author_key, authors, matched):
    """Ersten eindeutigen Treffer in der dokumentierten Reihenfolge finden.

    Rueckgabe ``(row_index, reason, ambiguous, candidates)``. Ein Treffer
    verbraucht die Zeile erst beim Aufrufer; mehrere freie Treffer auf
    denselben Schluessel sind ein Prueffall und werden nicht nach
    Listenposition ausgewaehlt. Der Titel+Autor-Schluessel transliteriert
    ae/oe/ue; deshalb muss bei diesem Grund zusaetzlich `authors_match`
    zustimmen («Michael» und «Michal» sind nicht derselbe Name).
    """
    attempts = []
    if stored_id:
        attempts.append((index.by_tolino_id.get(stored_id, ()), "stored_id"))
    if is_valid_isbn(local_isbn):
        attempts.append((index.by_isbn.get(local_isbn, ()), "isbn"))
    if title_key and author_key:
        title_author = [
            item for item in index.by_title_author.get((title_key, author_key), ())
            if authors_match(authors, index.rows[item]["authors"])]
        attempts.append((title_author, "title_author"))
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
    # Gleiche Metadaten dürfen eine geänderte Datei nicht verdecken.
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
    # ueberstimmen. Bewusster Kompromiss: aendert die Cloud den Titel spaeter,
    # gilt die Zeile weiter als gleich -- sonst wuerde jede externe Aenderung
    # beim naechsten Sync sofort wieder ueberschrieben.
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


def _remember_tolino_match(state, book_uuid, book_id, tolino_id):
    """Persist a confirmed assignment under the Calibre UUID."""
    entry = state.get(book_uuid)
    if not isinstance(entry, dict):
        entry = {}
        state[book_uuid] = entry
    entry["tolino_id"] = tolino_id
    entry.setdefault("calibre_id", book_id)


def _local_comparison_row(book_id, metadata, remote, state, preferred_formats,
                          matched, disputed, read_known, read_ids,
                          progress_known, progress_by_id, read_column=False):
    """Eine lokale Vergleichszeile -- inklusive einer bestaetigten Zuordnung."""
    # Ignorierte Bücher bleiben zugeordnet, erhalten aber keine Aktion.
    ignored = bool(metadata.get("ignored"))
    book_uuid = valid_book_uuid(metadata.get("uuid"))
    title = _metadata_text(metadata, "title")
    authors = _metadata_text(metadata, "authors")
    isbn = _metadata_text(metadata, "isbn")
    old = state_entry(state, book_uuid)
    stored_id = str(old.get("tolino_id") or "").strip()
    title_key = normalize_title(title)
    author_key = normalized_authors(authors)
    local_formats = normalize_formats(metadata.get("formats"))
    selected_format = next((fmt for fmt in preferred_formats
                            if fmt in local_formats), None)
    local_isbn = normalize_isbn(isbn)
    remote_index, match_reason, ambiguous, candidates = _find_remote_match(
        remote, stored_id, local_isbn, title_key, author_key, authors, matched)
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
        if book_uuid and tolino_id and status != "review":
            _remember_tolino_match(state, book_uuid, book_id, tolino_id)
    elif ambiguous:
        status = "review"
        disputed.update(candidates)
    else:
        status, match_reason = _title_only_match(
            remote, title_key, matched, disputed)

    row = _comparison_row(
        book_id, title, authors, isbn, tolino_id, status,
        _read_flags(tolino_id, read_known, read_ids),
        _progress_state(tolino_id, progress_known, progress_by_id),
        (IGNORED_EXPLANATION if ignored
         else _row_explanation(status, match_reason, file_changed)),
        group="ignored" if ignored else comparison_group(status),
        uuid=book_uuid or str(metadata.get("uuid") or ""),
        stale_cloud_id=(stored_id if status == "new_in_calibre"
                        and not tolino_id and stored_id else ""),
        ignored=ignored, match_reason=match_reason,
        file_changed=file_changed,
        has_preferred_format=bool(selected_format))
    if read_column:
        row["read_column"] = True
        local = metadata.get("calibre_read")
        row["read"].local = None if local is None else bool(local)
    return row


def _review_row(row, status, explanation):
    return dict(row, status=status, group="review", explanation=explanation)


def _leftover_remote_row(row, disputed, title_counts, row_index):
    """Cloud-Zeile, die keine lokale Zeile eindeutig beansprucht hat."""
    if row_index in disputed:
        return _review_row(row, "review",
                           "Möglicher Treffer, nicht eindeutig.")
    if row.get("cloud_id_missing"):
        return _review_row(row, "review",
                           "In tolino vorhanden, aber nicht ansprechbar "
                           "(kein Download, kein Löschen).")
    if not row["title"]:
        return _review_row(row, "not_matchable",
                           "tolino-Datensatz ohne Titel – nicht zuordenbar.")
    if title_counts.get(row["normalized_title"], 0) > 1:
        return _review_row(row, "duplicate_tolino",
                           "Titel mehrfach in tolino – nicht automatisch hochladen.")
    return row


def compare_inventory(metadata_by_id, state, inventory, preferred_formats=(),
                      read_ids=(), read_column=False, progress_by_id=None):
    """Return deterministic local/remote rows for the pre-sync confirmation view.

    Matching is deliberately conservative and ordered:

    1. the stored tolino ID (sync state or account-specific ``#tolino_id``),
    2. one unique *valid* ISBN,
    3. one unique normalized title+author pair.

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

    ``progress_by_id`` maps tolino IDs to the reading progress from
    sync-data. ``None`` means «not loaded»; an empty mapping means «loaded,
    but no bookmarks». Progress is display-only and is never written back.
    """
    preferred_formats = normalize_formats(preferred_formats)
    read_known = read_ids is not None
    read_ids = ({str(item) for item in read_ids if str(item or "").strip()}
                if read_known else set())
    progress_known = progress_by_id is not None
    progress_by_id = ({str(key): value for key, value in progress_by_id.items()}
                      if progress_known and isinstance(progress_by_id, dict)
                      else {})
    rows = _remote_rows(
        inventory, read_known, read_ids, progress_known, progress_by_id)
    remote = _RemoteIndex(rows)
    matched = set()
    disputed = set()
    local_rows = []
    for book_id, metadata in metadata_by_id.items():
        local_rows.append(_local_comparison_row(
            book_id, metadata, remote, state, preferred_formats,
            matched, disputed, read_known, read_ids,
            progress_known, progress_by_id, read_column))
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
    """Hash uuid, format and size/mtime; return '' if no signature was read.

    This is not a content hash: equal size and mtime can hide file changes.
    """
    signatures = metadata.get("format_signatures")
    signature = (str(signatures.get(format_name) or "")
                 if isinstance(signatures, dict) else "")
    if not signature:
        return ""
    value = "%s|%s|%s" % (metadata.get("uuid", ""), format_name, signature)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


