"""Normalize Tolino inventory responses for the client and sync planner."""

# Container, in denen die Cloud ihre Bestandslisten liefert.
_INVENTORY_CONTAINERS = ("edata", "ebook")

# Felder, unter denen die Cloud die Kennung einer Kopie fuehrt. Bewusst NUR
# die Kennung des Deliverables: ``identifier`` aus ``epubMetaData`` ist eine
# ISBN/Verlagsnummer und taugt nicht fuer /deletecontent und /downloadinfo.
_INVENTORY_ID_KEYS = ("deliverableId", "deliverable_id", "tolino_id", "id")
# Ersatzkennung, wenn kein Deliverable-Feld da ist (z. B. ``identifier`` in
# ``epubMetaData``). Sie belegt nur, dass IRGENDEINE Kopie existiert -- sie
# taugt weder fuer /deletecontent noch als Beweis, dass eine bestimmte
# Deliverable-ID verschwunden ist.
_INVENTORY_ID_FALLBACK_KEYS = ("identifier",)
# Container, in denen die Kennung stehen kann (Objekt ODER Liste).
_INVENTORY_ID_CONTAINERS = ("epubMetaData", "deliverable", "metadata",
                            "ebook", "edata")
# Felder, an denen ein Objekt als BUCH-Datensatz erkennbar ist. Ein Dict mit
# ``edata``/``ebook`` UND einem dieser Felder ist selbst ein Datensatz; nur
# ein reiner Sammel-Container wird aufgeloest.
_INVENTORY_RECORD_HINTS = ("deliverableId", "deliverable_id", "identifier",
                           "title", "bookTitle", "isbn", "epubMetaData",
                           "authors", "author", "uuid")
_NORMALIZED_RECORD_KEYS = frozenset({
    "tolino_id", "has_deliverable_id", "identifier", "title", "authors",
    "isbn", "uuid",
})


def _nested_inventory_value(value, names, depth=0):
    """Wert in einem Bestands-Datensatz -- auch verschachtelt und in Listen.

    EINE Suche fuer Kennung, Titel, Autor, ISBN und UUID. ``epubMetaData.
    deliverable`` ist in der Cloud eine Liste; ohne diesen Abstieg blieben
    solche Kopien ohne Kennung. Die Tiefe ist begrenzt, damit eine
    ungewoehnlich verschachtelte Antwort nicht teuer wird.
    """
    if isinstance(value, (list, tuple)):
        if depth >= 4:
            return ""
        for entry in value:
            found = _nested_inventory_value(entry, names, depth + 1)
            if found not in (None, "", [], {}):
                return found
        return ""
    if not isinstance(value, dict):
        return ""
    for name in names:
        found = value.get(name)
        if found not in (None, "", [], {}):
            return found
    if depth >= 4:
        return ""
    for container in _INVENTORY_ID_CONTAINERS:
        found = _nested_inventory_value(value.get(container), names, depth + 1)
        if found not in (None, "", [], {}):
            return found
    return ""


def _inventory_id_in(value, keys):
    """Kennung als Text; strukturierte Treffer zaehlen nicht als Kennung."""
    found = _nested_inventory_value(value, keys)
    if isinstance(found, (list, tuple, dict)):
        return ""
    return str(found).strip()


def _inventory_author(value):
    if isinstance(value, dict):
        return str(value.get("name") or value.get("displayName") or "").strip()
    if isinstance(value, (list, tuple)):
        names = [_inventory_author(item) for item in value]
        return ", ".join(name for name in names if name)
    return str(value or "").strip()


def normalize_inventory_record(item):
    """EIN Datensatz, EINE Form fuer Vergleich und Loeschlogik.

    Rueckgabe::

        {"tolino_id": <Deliverable-Kennung oder "">,
         "has_deliverable_id": bool,
         "identifier": <Ersatzkennung/ISBN oder "">,
         "title": str, "authors": str, "isbn": str, "uuid": str}

    ``tolino_id`` bleibt leer, wenn nur ``identifier`` existiert: diese
    Kennung taugt weder fuer ``/downloadinfo`` noch fuer
    ``/deletecontent``. ``has_deliverable_id`` sagt, ob die Kopie fuer
    Loeschungen ueberhaupt ansprechbar ist.
    """
    # The API client already returns this exact shape. Reuse it when the
    # planner sees it again; partial records still go through parsing.
    if (isinstance(item, dict)
            and _NORMALIZED_RECORD_KEYS <= item.keys()):
        return item
    deliverable = _inventory_id_in(item, _INVENTORY_ID_KEYS)
    identifier = _inventory_id_in(item, _INVENTORY_ID_FALLBACK_KEYS)
    title = _nested_inventory_value(item, ("title", "bookTitle", "name"))
    author = _nested_inventory_value(item, ("authors", "author", "creator"))
    isbn = _nested_inventory_value(
        item, ("isbn", "ISBN", "isbn13", "isbn10", "identifier"))
    uuid_value = _nested_inventory_value(
        item, ("uuid", "calibreUuid", "calibre_uuid"))
    return {
        "tolino_id": deliverable,
        "has_deliverable_id": bool(deliverable),
        "identifier": identifier,
        "title": str(title or "").strip(),
        "authors": _inventory_author(author),
        "isbn": str(isbn or "").strip(),
        "uuid": str(uuid_value or "").strip(),
    }


def _looks_like_inventory_record(value):
    """True, wenn ein Dict selbst ein Datensatz ist (nicht nur Container)."""
    if not isinstance(value, dict):
        return False
    return any(key in value for key in _INVENTORY_RECORD_HINTS)


def _field_names(value):
    """Feldnamen eines Objekts -- nie Werte (fuer Fehlermeldungen)."""
    if not isinstance(value, dict):
        return type(value).__name__
    return ", ".join(sorted(str(key) for key in value)[:10]) or "keine"


def _collect_inventory(value, records, problems, container=""):
    """Datensaetze sammeln; unerwartete Teile als Problem notieren.

    Ein Datensatz, der selbst ``ebook``/``edata`` heisst, ist ein Datensatz:
    frueher wurde er als Container gelesen und seine uebrigen Felder gingen
    verloren.
    """
    if isinstance(value, dict):
        nested = [key for key in _INVENTORY_CONTAINERS if key in value]
        if nested and not _looks_like_inventory_record(value):
            for key in nested:
                _collect_inventory(value[key], records, problems, key)
            return
        records.append(value)
        return
    if isinstance(value, (list, tuple)):
        for entry in value:
            _collect_inventory(entry, records, problems, container)
        return
    if value is None:
        problems.append("Container %s ist leer" % (container or "?"))
        return
    problems.append("Eintrag vom Typ %s" % type(value).__name__)


def parse_inventory(data):
    """Inventar-Antwort lesen: ``(records, problems, envelope_ok)``.

    ``envelope_ok`` False heisst: die Antwort hat gar nicht die Form eines
    Bestands (fehlender Schluessel, falscher Typ, leere Huelle). Das ist
    NIE ein leerer Bestand und laesst den Vergleich scheitern -- sonst
    saehe die Cloud leer aus und jede lokale Datei gaelte als neu.

    ``problems`` bei ``envelope_ok`` True heissen: einzelne Datensaetze
    liessen sich nicht deuten. Die Anzeige nutzt dann die lesbaren; jede
    Loeschung verweigert ``inventory_ids()`` (N02), weil eine unbekannte
    Kopie sonst faelschlich als «nicht mehr in der Cloud» gelten koennte.
    Der Sync-Lauf macht ohne Loeschungen weiter und meldet das.

    Ein Datensatz mit NUR ``identifier`` ist deutbar (kein Problem), traegt
    aber keine Deliverable-Kennung. Das zaehlt ``inventory_ids_complete``
    als unvollstaendig: eine nicht gefundene ID darf dann nicht als
    geloescht gelten.
    """
    if not isinstance(data, dict):
        return [], ["Antwort ist kein Objekt"], False
    if "PublicationInventory" not in data:
        return ([], ["PublicationInventory fehlt (Felder: %s)"
                     % _field_names(data)], False)
    container = data["PublicationInventory"]
    if isinstance(container, dict) and not any(
            key in container for key in _INVENTORY_CONTAINERS):
        return ([], ["PublicationInventory ohne edata/ebook (Felder: %s)"
                     % _field_names(container)], False)
    if isinstance(container, (str, bytes, int, float, bool)) or container is None:
        return ([], ["PublicationInventory hat den Typ %s"
                     % type(container).__name__], False)
    raw, problems = [], []
    _collect_inventory(container, raw, problems)
    records = [normalize_inventory_record(item) for item in raw]
    unknown = sum(1 for record in records
                  if not record["tolino_id"] and not record["identifier"])
    if unknown:
        problems.append("%d Datensatz/Datensaetze ohne Kennung" % unknown)
    return records, problems, True


def inventory_records(value):
    """Alle Datensaetze einer Bestandsform (Huelle, Liste oder Datensatz).

    Eine Implementierung fuer Client und Vergleich: ``compare_inventory``
    bekommt damit dieselbe Deutung wie ``parse_inventory``.
    """
    records, problems = [], []
    _collect_inventory(value, records, problems)
    return records


