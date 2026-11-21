"""Persistent settings and account state for tolino Cloud Sync."""
import contextlib
import copy
import json
import os
import threading

from calibre.utils.config import JSONConfig

from .comparison import COPY_BOUND_FIELDS

# Der bisherige Store bleibt der aktive Datenspeicher: Der neue Namespace
# aus dem 0.1.08-Entwicklungsstand wurde vor der Verteilung zurückgenommen.
PREFERENCE_NAMESPACE = "plugins/Tolino Cloud Sync Lite"
PREFERENCES = JSONConfig(PREFERENCE_NAMESPACE)
DEFAULT_ACCOUNT_NAME = "default"
ACCOUNT_KEYS = ("partner_id", "hardware_id", "refresh_token", "state")
PREFERRED_FORMATS = "preferred_formats"
DEFAULT_ACCOUNT = {
    "name": DEFAULT_ACCOUNT_NAME,
    "partner_id": 1,
    "hardware_id": "",
    "refresh_token": "",
    "state": {},
}
_SETTINGS_LOCK = threading.RLock()
_UNSET = object()
# `_SETTINGS_LOCK` gilt nur in diesem Prozess. Eine zweite Calibre-Instanz
# kann dieselbe Datei schreiben. Wo verfuegbar, schuetzt eine zusaetzliche
# Dateisperre das Laden, Vergleichen und Schreiben.
_LOCK_STATE = threading.local()


def _lock_handle(handle):
    """Advisory lock on the settings lock file, where the OS supports it.

    Die Sperre endet mit dem Schliessen der Datei -- ein eigenes Entsperren
    ist weder noetig noch plattformuebergreifend gleich.
    """
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except ImportError:
        pass


@contextlib.contextmanager
def _settings_file_lock():
    """Sperrdatei neben dem Speicher; im selben Thread wiederverwendbar.

    Ohne Sperrdatei (unschreibbares Verzeichnis, fehlendes ``fcntl``) bleibt
    nur `_SETTINGS_LOCK`: der eigene Prozess ist trotzdem geschuetzt, ein
    fremder Schreiber aber nicht ausgeschlossen. Verschachtelte Aufrufe
    (etwa `_write_account` aus `save_state`) sperren nicht erneut.
    """
    depth = getattr(_LOCK_STATE, "depth", 0)
    if depth:
        _LOCK_STATE.depth = depth + 1
        try:
            yield
        finally:
            _LOCK_STATE.depth = depth
        return
    handle, path = None, getattr(PREFERENCES, "file_path", None)
    if path:
        try:
            handle = open(str(path) + ".lock", "a+b")
            if os.fstat(handle.fileno()).st_size == 0:
                # msvcrt sperrt ein Byte; eine leere Datei liesse sich nicht
                # sperren.
                handle.write(b"\0")
                handle.flush()
        except OSError:
            if handle is not None:
                handle.close()
            handle = None
    try:
        if handle is not None:
            _lock_handle(handle)
        _LOCK_STATE.depth = 1
        yield
    finally:
        _LOCK_STATE.depth = 0
        if handle is not None:
            handle.close()


@contextlib.contextmanager
def settings_write_lock():
    """Keep cooperating settings writers out until a rotation is saved."""
    with _SETTINGS_LOCK, _settings_file_lock():
        yield


def _reload():
    """Datei bei jedem Zugriff lesen; nur ein vollstaendiger Stand zaehlt.

    Eine zweite Calibre-Instanz, der Keep-alive oder ein Kindprozess schreibt
    in dieselbe Datei. Ohne Abgleich zeigt das offene Fenster bis zum Neustart
    einen veralteten Kontostand. Gelesen wird der Rohinhalt und nicht der
    Zeitstempel: dessen Aufloesung ist je nach Dateisystem grob, schnelle
    Schreibvorgaenge gleicher Groesse blieben unsichtbar. Eine halb
    geschriebene Datei zaehlt nicht als Stand -- Calibres `refresh()` wuerde
    sie uebernehmen und dabei alle Konten verlieren.

    ``False`` heisst: die Datei ist gerade unbrauchbar. Sie kann existieren
    und nur gesperrt sein (Rechte, Verzeichnis, E/A-Fehler) -- das ist NICHT
    dasselbe wie «es gibt sie nicht». Lesen bleibt dann beim letzten guten
    Stand, ein Schreibvorgang bricht ab, statt ihn zu ueberschreiben.
    """
    with _SETTINGS_LOCK:
        path = getattr(PREFERENCES, "file_path", None)
        if not path:
            return True
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except FileNotFoundError:
            # Keine Datei: der Cache ist der Stand (frische Installation oder
            # gerade geloescht). Ein spaeter angelegter Stand wird erkannt.
            return True
        except OSError:
            # Datei existiert, ist aber nicht lesbar: Cache behalten und
            # Schreibvorgaenge abbrechen, statt den alten Stand ueber einen
            # fremden oder neueren zu schreiben.
            return False
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            data = None
        if not isinstance(data, dict):
            # Halb geschrieben: nicht uebernehmen und jeden Schreibvorgang
            # abbrechen, statt die halbe Datei zu ersetzen. Jeder weitere
            # Zugriff versucht es erneut.
            return False
        # `clear`/`update` umgehen Calibres committendes `__setitem__`:
        # ein Lesevorgang darf die Datei nicht schreiben.
        dict.clear(PREFERENCES)
        dict.update(PREFERENCES, data)
        return True


def _commit():
    """Schreiben abschliessen."""
    PREFERENCES.commit()


def load_state(raw):
    """Load the saved state mapping; malformed or absent values are empty."""
    return raw if isinstance(raw, dict) else {}


# Zustandseinträge müssen tief kopiert werden: sonst teilen Worker und
# Einstellungen dieselben Dicts und Listen.
def _account(value, fallback_name=DEFAULT_ACCOUNT_NAME, copy_state=True):
    """Return one clean account record with an independent state mapping."""
    source = value if isinstance(value, dict) else {}
    result = dict(DEFAULT_ACCOUNT)
    result.update({key: source[key] for key in ACCOUNT_KEYS if key in source})
    result["name"] = str(source.get("name") or fallback_name).strip() or fallback_name
    state = source.get("state")
    state = state if isinstance(state, dict) else {}
    result["state"] = copy.deepcopy(state) if copy_state else state
    return result


def _named_accounts(raw):
    """Namen und Rohdatensatz je Konto, ohne andere Zustände zu kopieren."""
    if not isinstance(raw, list) or not raw:
        # Auf einer frischen Installation ist das Default-Konto nur virtuell.
        yield DEFAULT_ACCOUNT_NAME, {}
        return
    names = set()
    for index, value in enumerate(raw):
        source = value if isinstance(value, dict) else {}
        fallback = "account-%d" % (index + 1)
        base = str(source.get("name") or fallback).strip() or fallback
        name, number = base, index + 1
        while name in names:
            name, number = "%s-%d" % (base, number), number + 1
        names.add(name)
        yield name, source


def _account_summaries(raw):
    """Kontometadaten für UI und Keep-alive -- ohne die grossen States."""
    return [{"name": name,
             "partner_id": source.get("partner_id", 1),
             "hardware_id": source.get("hardware_id", ""),
             "refresh_token": source.get("refresh_token", "")}
            for name, source in _named_accounts(raw)]


def account_summary(name):
    """Nur Name, Zugang und Geräte-ID eines Kontos (ohne Buchzustand)."""
    with _SETTINGS_LOCK:
        _reload()
        return next((item for item in _account_summaries(
            PREFERENCES.get("accounts", None))
            if item["name"] == name), {})


def _active_name(accounts):
    """Aktives Konto oder das erste vorhandene -- nie ein leerer Name."""
    names = {item["name"] for item in accounts}
    active = str(PREFERENCES.get("active_account", "") or "")
    return active if active in names else accounts[0]["name"]


def settings_readable():
    """True nur, wenn der Einstellungsspeicher gerade lesbar ist.

    Der Keep-alive darf einen einmal nutzbaren Token nicht einloesen, wenn
    die Rotation anschliessend nicht gespeichert werden kann.
    """
    with _SETTINGS_LOCK:
        return _reload()


def settings():
    """Aktuelles Konto und Metadaten -- ohne Buchzustände zu kopieren."""
    with _SETTINGS_LOCK:
        _reload()
        accounts = _account_summaries(PREFERENCES.get("accounts", None))
        active = _active_name(accounts)
        current = next(item for item in accounts if item["name"] == active)
        return {
            PREFERRED_FORMATS: list(PREFERENCES.get(PREFERRED_FORMATS,
                                                    ["EPUB"])),
            "partner_id": current["partner_id"],
            "hardware_id": current["hardware_id"],
            "accounts": accounts,
            "active_account": active,
        }


def _write_account(name, values, *, active=_UNSET, preferred_formats=None,
                   create=True, allow_virtual_default=False, reload=True):
    """Persist one account and optionally the active/format settings.

    ``active`` ist ein Sentinel: Wird es übergeben (auch als ``None``), wird
    das aktive Konto neu bestimmt. Fehlt es, bleibt die Auswahl unberührt.
    ``reload=False`` nur für Aufrufer, die den Stand gerade selbst gelesen und
    zusammengefuehrt haben: sie schreiben genau diesen Stand.

    Laden und Commit laufen unter beiden Sperren: ein fremder Prozess kann
    die Datei zwischen `_reload()` und `_commit()` nicht mehr aendern.
    """
    with _SETTINGS_LOCK, _settings_file_lock():
        name = str(name or "").strip()
        if not name or not isinstance(values, dict):
            return False
        # Eine zweite Calibre-Instanz oder der Keep-alive kann inzwischen
        # geschrieben haben. `JSONConfig` haelt den Stand im Speicher und
        # schreibt beim naechsten Commit ALLES zurueck -- ohne diesen Abgleich
        # loescht der eigene, gecachte Stand fremde Token.
        if reload and not _reload():
            return False
        raw = PREFERENCES.get("accounts", None)
        records = list(raw) if isinstance(raw, list) else []
        index = None
        if records:
            for position, (stored, _source) in enumerate(
                    _named_accounts(records)):
                if stored == name:
                    index = position
                    break
        previous_active = ""
        if active is not _UNSET:
            previous_active = _active_name(
                [{"name": stored}
                 for stored, _source in _named_accounts(records)])
        if index is None:
            if not (create or (allow_virtual_default and not records
                               and name == DEFAULT_ACCOUNT_NAME)):
                return False
            records.append(_account(dict(values, name=name), name))
        else:
            record = dict(records[index])
            record.update({key: values[key] for key in ACCOUNT_KEYS
                           if key in values})
            records[index] = _account(record, name,
                                      copy_state="state" in values)
        changes = {"accounts": records}
        if active is not _UNSET:
            names = {stored for stored, _source in _named_accounts(records)}
            chosen = str(active or previous_active or "").strip()
            changes["active_account"] = chosen if chosen in names else name
        elif not PREFERENCES.get("active_account"):
            changes["active_account"] = name
        if preferred_formats is not None:
            changes[PREFERRED_FORMATS] = list(preferred_formats)
        # `update` umgeht Calibres committendes `__setitem__`: alle Werte
        # landen in EINEM Schreibvorgang, statt halb geschrieben auf der
        # Platte zu stehen.
        PREFERENCES.update(changes)
        _commit()
        return True


def save_account(name, values, active=None, preferred_formats=None,
                 create=True):
    """Persist one account without changing any other account's state.

    `preferred_formats` schreibt die globalen Formate im selben Commit mit;
    der Dialog braucht dafür keinen zweiten Schreibaufruf. Mit
    ``create=False`` werden nur bestehende Konten aktualisiert: ein
    verspäteter Hintergrund-Schreibvorgang darf kein gelöschtes Konto
    wieder anlegen.
    """
    with _SETTINGS_LOCK:
        return _write_account(
            name, values, active=active,
            preferred_formats=preferred_formats, create=create,
            allow_virtual_default=True)


def _copy_changed(before, entry):
    """True, wenn die beiden Eintraege zu verschiedenen Cloud-Kopien gehoeren."""
    old = str(before.get("tolino_id") or "").strip()
    new = str(entry.get("tolino_id") or "").strip()
    return old != new


def _merged_entry(before, entry, current):
    """Ein Buch feldweise zusammenfuehren: nur eigene Aenderungen setzen.

    Kopiegebundene Felder duerfen nur von der resultierenden ID stammen.
    """
    merged = dict(current) if isinstance(current, dict) else {}
    changes = (set(entry) if not isinstance(before, dict) else
               {field for field in set(before) | set(entry)
                if before.get(field) != entry.get(field)})
    if "tolino_id" in changes:
        result_copy = entry
    else:
        result_copy = merged
    if _copy_changed(merged, result_copy):
        for field in COPY_BOUND_FIELDS:
            merged.pop(field, None)
    local_matches = not _copy_changed(entry, result_copy)
    local_replaced = isinstance(before, dict) and _copy_changed(before, entry)
    for field in changes:
        if field in COPY_BOUND_FIELDS and (
                not local_matches or (local_replaced and field not in entry)):
            # Eine Entfernung beim Kopiewechsel betrifft nur die alte Kopie.
            continue
        if field in entry:
            merged[field] = entry[field]
        else:
            merged.pop(field, None)
    return merged


def _merge_state(stored, base, state):
    """Drei-Wege-Merge des Zustands gegen den gespeicherten Stand.

    Nur was sich gegenueber `base` geaendert hat, setzt sich durch; alles
    andere -- auch Felder desselben Buchs -- bleibt, wie es inzwischen
    gespeichert wurde.
    """
    merged = dict(stored)
    for key in set(base) - set(state):
        merged.pop(key, None)  # in diesem Lauf entfernt
    for key, entry in state.items():
        current = merged.get(key)
        if isinstance(entry, dict):
            entry = _merged_entry(base.get(key), entry, current)
            if current is None and not entry:
                continue  # fremd geloescht und lokal unveraendert
        merged[key] = entry
    return merged


def save_state(account_name, state, base):
    """Zustand eines Kontos sichern: nur Aenderungen gegenueber `base`.

    `base` ist der Ausgangsstand des Aufrufers. Ein anderer Lauf kann
    inzwischen andere Felder desselben Buchs geschrieben haben; gespeichert
    werden deshalb nur geaenderte Felder. Ein hier entfernter Eintrag oder
    ein entferntes Feld verschwindet auch dann, wenn `base` es noch nennt.
    """
    with _SETTINGS_LOCK, _settings_file_lock():
        if not _reload():
            return False
        source = _account_record(account_name)
        if source is None:
            return False
        stored = source.get("state")
        stored = stored if isinstance(stored, dict) else {}
        merged = _merge_state(stored, base, state)
        if merged == stored:
            # Nichts geaendert: kein vollstaendiger Schreibvorgang.
            return True
        # Der Stand ist gelesen und zusammengefuehrt: nicht noch einmal
        # einlesen, sonst schriebe der Merge auf einem anderen Stand.
        return _write_account(account_name, {"state": merged}, create=False,
                              allow_virtual_default=True, reload=False)


def _account_record(name):
    """Rohdatensatz eines Kontos aus dem geladenen Stand (ohne Kopie)."""
    for stored, source in _named_accounts(PREFERENCES.get("accounts", None)):
        if stored == name:
            return source
    return None


def account_by_name(name):
    """Return one account without copying every other account's state."""
    with _SETTINGS_LOCK:
        _reload()
        source = _account_record(name)
    return _account(dict(source, name=name), name) if source is not None else {}


def _update_account(name, values, allow_virtual_default=True):
    """Update one existing account, by default the virtual default account.

    ``allow_virtual_default=False`` fuer Aufrufer, die auch das virtuelle
    Default-Konto nicht anlegen duerfen (etwa das Loeschen eines Tokens).
    """
    with _SETTINGS_LOCK:
        return _write_account(
            name, values, create=False,
            allow_virtual_default=allow_virtual_default)


def _stored_token(account_name):
    """Der gespeicherte Refresh-Token eines Kontos, leer heisst «keiner»."""
    return str(
        account_summary(account_name).get("refresh_token") or "").strip()


def set_active_account(name):
    """Switch the active account without touching any stored values."""
    with _SETTINGS_LOCK, _settings_file_lock():
        # Wie bei jedem Schreiben: erst den fremden Stand laden, sonst
        # schreibt der eigene Cache fremde Token weg.
        if not _reload():
            return False
        names = [stored for stored, _ in _named_accounts(
            PREFERENCES.get("accounts", None))]
        if name not in names:
            return False
        PREFERENCES["active_account"] = name
        _commit()
        return True


def save_credentials(account_name, refresh_token, hardware, partner_id=None):
    """Persist a rotated refresh token, hardware ID and detected shop.

    Das aktive Konto bleibt unberührt: der Keep-alive rotiert alle Konten,
    ein Token-Rotationsschritt darf die Auswahl nicht umschalten.
    """
    values = {"refresh_token": refresh_token, "hardware_id": hardware}
    if partner_id:
        values["partner_id"] = partner_id
    return _update_account(account_name, values)


def save_refresh_token(account_name, token):
    """Einen rotierten Token im Konto sichern; leer loescht nichts.

    Der Client meldet jede Token-Antwort hier; eine Antwort ohne
    `refresh_token` darf den gespeicherten Zugang nicht ausleeren. Ein
    unbekanntes Konto wird nicht angelegt: ein verspaeteter Callback darf
    ein gerade entferntes Konto nicht wieder erzeugen.
    """
    token = str(token or "").strip()
    if token:
        return _update_account(account_name, {"refresh_token": token})
    return False


def clear_refresh_token_if_unchanged(account_name, rejected_token):
    """Nur den wirklich abgelehnten Token loeschen.

    Keep-alive, GUI und eine zweite Calibre-Instanz koennen denselben
    Einmal-Token gleichzeitig einloesen. Verliert diese Anfrage das Rennen,
    antwortet der Server mit ``invalid_grant`` -- ein blindes Loeschen wuerde
    dann den frisch gespeicherten, gueltigen Token des Gewinners mitnehmen
    und eine neue Anmeldung erzwingen. Steht inzwischen ein anderer Token im
    Konto, bleibt er unangetastet.
    """
    rejected = str(rejected_token or "").strip()
    # Vergleich und Loeschen unter beiden Sperren: sonst kann zwischen beiden
    # Schritten genau die Rotation stattfinden, die geschuetzt werden soll --
    # auch aus einer zweiten Calibre-Instanz.
    with _SETTINGS_LOCK, _settings_file_lock():
        current = _stored_token(account_name)
        if rejected and current and current != rejected:
            return False
        # Ein entferntes Konto darf ein verspaeteter Callback nicht anlegen.
        return _update_account(account_name, {"refresh_token": ""},
                               allow_virtual_default=False)


def save_hardware_id(account_name, hardware):
    """Die Geraete-ID im Konto sichern -- die Config ist ihre einzige Quelle."""
    return _update_account(account_name, {"hardware_id": str(hardware or "")})


def account_secrets(account_name):
    """Bekannte Geheimnisse eines Kontos fuer die Log-Redaktion."""
    token = _stored_token(account_name)
    return (token,) if token else ()


def remove_account(name):
    """Remove one saved account and select a remaining account."""
    with _SETTINGS_LOCK, _settings_file_lock():
        if not _reload():
            return False
        raw = PREFERENCES.get("accounts", None)
        named = list(_named_accounts(raw))
        remaining = [(stored, source) for stored, source in named
                     if stored != name]
        if not remaining or len(remaining) == len(named):
            return False
        active = _active_name([{"name": stored} for stored, _ in named])
        PREFERENCES.update({
            "accounts": [source for _stored, source in remaining],
            "active_account": (active if active != name else remaining[0][0]),
        })
        _commit()
        return True
