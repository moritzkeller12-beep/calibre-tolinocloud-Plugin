"""Persistent settings and account state for tolino Cloud Sync."""
import copy
import threading

from calibre.utils.config import JSONConfig

SCHEMA_VERSION = 2
PREFERENCE_NAMESPACE = "plugins/Tolino Cloud Sync"
LEGACY_NAMESPACE = "plugins/Tolino Cloud Sync Lite"
BACKUP_NAMESPACE = "plugins/Tolino Cloud Sync Migration Backup 0.1.08"
PREFERENCES = JSONConfig(PREFERENCE_NAMESPACE)
LEGACY_PREFERENCES = JSONConfig(LEGACY_NAMESPACE)
LEGACY_BACKUP = JSONConfig(BACKUP_NAMESPACE)
LEGACY_PREFERENCE_KEYS = (
    "refresh_token", "partner_id", "hardware_id", "state",
    "enable_deletions", "compare_authors", "compare_title",
    "compare_isbn", "sync_collections", "upload_covers",
)
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
_USING_LEGACY_PREFERENCES = False


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


def _snapshot(store):
    """Copy a preferences store without assuming a concrete Calibre type."""
    keys = getattr(store, "keys", None)
    keys = list(keys()) if callable(keys) else [
        key for key in LEGACY_PREFERENCE_KEYS if key in store]
    return {key: copy.deepcopy(store.get(key)) for key in keys}


def _commit(store):
    commit = getattr(store, "commit", None)
    if callable(commit):
        commit()


def _legacy_accounts(legacy):
    """Return account records from an old list or single-account store."""
    raw = legacy.get("accounts")
    if isinstance(raw, list) and raw:
        return list(raw)
    if not any(key in legacy for key in ACCOUNT_KEYS):
        return []
    return [dict(
        {"name": DEFAULT_ACCOUNT_NAME},
        **{key: copy.deepcopy(legacy[key]) for key in ACCOUNT_KEYS
           if key in legacy})]


def migrate_preferences(current=None, legacy=None, backup=None):
    """Move known settings from the old namespace once, with a backup.

    Only current keys are copied; obsolete top-level options stay in the
    backup. Existing data in the new namespace always wins. The function is
    idempotent and returns True only when it wrote something.
    """
    current = PREFERENCES if current is None else current
    legacy = LEGACY_PREFERENCES if legacy is None else legacy
    backup = LEGACY_BACKUP if backup is None else backup
    with _SETTINGS_LOCK:
        try:
            version = int(current.get("schema_version", 0) or 0)
        except (TypeError, ValueError):
            version = 0
        if version >= SCHEMA_VERSION:
            return False
        old = _snapshot(legacy)
        if not old and not current:
            return False
        if old and not _snapshot(backup):
            backup.update(old)
            _commit(backup)
        changes = {}
        if not current.get("accounts"):
            accounts = _legacy_accounts(legacy)
            if accounts:
                changes["accounts"] = copy.deepcopy(accounts)
                names = [name for name, _source in _named_accounts(accounts)]
                active = str(legacy.get("active_account") or "").strip()
                changes["active_account"] = (
                    active if active in names else names[0])
        formats = legacy.get(PREFERRED_FORMATS)
        if (PREFERRED_FORMATS not in current
                and isinstance(formats, (list, tuple))):
            changes[PREFERRED_FORMATS] = list(formats)
        changes["schema_version"] = SCHEMA_VERSION
        current.update(changes)
        _commit(current)
        return True


def ensure_preferences_migrated():
    """Run the one-time migration, falling back to the old store on failure."""
    global PREFERENCES, _USING_LEGACY_PREFERENCES
    try:
        return migrate_preferences()
    except Exception as exc:
        # Never hide existing accounts behind a failed migration. The next
        # plugin start retries against the old store.
        PREFERENCES = LEGACY_PREFERENCES
        _USING_LEGACY_PREFERENCES = True
        try:
            from .tolino import log_warning
            log_warning("Preferences-Migration fehlgeschlagen: %s"
                        % type(exc).__name__)
        except Exception:
            pass
        return False


def _mark_schema():
    if not _USING_LEGACY_PREFERENCES:
        PREFERENCES.update({"schema_version": SCHEMA_VERSION})


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
        return next((item for item in _account_summaries(
            PREFERENCES.get("accounts", None))
            if item["name"] == name), {})


def _active_name(accounts):
    """Aktives Konto oder das erste vorhandene -- nie ein leerer Name."""
    names = {item["name"] for item in accounts}
    active = str(PREFERENCES.get("active_account", "") or "")
    return active if active in names else accounts[0]["name"]


def settings():
    """Aktuelles Konto und Metadaten -- ohne Buchzustände zu kopieren."""
    with _SETTINGS_LOCK:
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


def legacy_report(current=None, backup=None):
    """Return counts of legacy settings and state; no values or tokens."""
    current = PREFERENCES if current is None else current
    backup = LEGACY_BACKUP if backup is None else backup
    with _SETTINGS_LOCK:
        backup_snapshot = _snapshot(backup)
        source = backup_snapshot or _snapshot(LEGACY_PREFERENCES)
        raw = current.get("accounts")
        named = list(_named_accounts(raw)) if isinstance(raw, list) else []
        state_entries = 0
        fallback_keys = 0
        for _name, account in named:
            state = account.get("state") if isinstance(account, dict) else None
            if not isinstance(state, dict):
                continue
            state_entries += len(state)
            fallback_keys += sum(
                1 for key in state if str(key).startswith("calibre-"))
        try:
            version = int(current.get("schema_version", 0) or 0)
        except (TypeError, ValueError):
            version = 0
        return {
            "schema_version": version,
            "account_count": len(named),
            "state_entries": state_entries,
            "fallback_state_keys": fallback_keys,
            "backup_available": bool(backup_snapshot),
            "legacy_preference_keys": tuple(sorted(
                key for key in LEGACY_PREFERENCE_KEYS if key in source)),
        }


def _write_account(name, values, *, active=_UNSET, preferred_formats=None,
                   create=True, allow_virtual_default=False):
    """Persist one account and optionally the active/format settings.

    ``active`` ist ein Sentinel: Wird es übergeben (auch als ``None``), wird
    das aktive Konto neu bestimmt. Fehlt es, bleibt die Auswahl unberührt.
    """
    name = str(name or "").strip()
    if not name or not isinstance(values, dict):
        return False
    raw = PREFERENCES.get("accounts", None)
    records = list(raw) if isinstance(raw, list) else []
    index = None
    if records:
        for position, (stored, _source) in enumerate(_named_accounts(records)):
            if stored == name:
                index = position
                break
    previous_active = ""
    if active is not _UNSET:
        previous_active = _active_name(
            [{"name": stored} for stored, _source in _named_accounts(records)])
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
    PREFERENCES["accounts"] = records
    if active is not _UNSET:
        names = {stored for stored, _source in _named_accounts(records)}
        chosen = str(active or previous_active or "").strip()
        PREFERENCES["active_account"] = chosen if chosen in names else name
    elif not PREFERENCES.get("active_account"):
        PREFERENCES["active_account"] = name
    if preferred_formats is not None:
        PREFERENCES[PREFERRED_FORMATS] = list(preferred_formats)
    _mark_schema()
    PREFERENCES.commit()
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


def account_by_name(name):
    """Return one account without copying every other account's state."""
    with _SETTINGS_LOCK:
        for stored, value in _named_accounts(
                PREFERENCES.get("accounts", None)):
            if stored == name:
                return _account(dict(value, name=stored), stored)
    return {}


def _update_account(name, values, allow_virtual_default=False):
    """Update one existing account; callbacks never recreate deleted accounts."""
    with _SETTINGS_LOCK:
        return _write_account(
            name, values, create=False,
            allow_virtual_default=allow_virtual_default)


def set_active_account(name):
    """Switch the active account without touching any stored values."""
    with _SETTINGS_LOCK:
        names = [stored for stored, _ in _named_accounts(
            PREFERENCES.get("accounts", None))]
        if name not in names:
            return False
        PREFERENCES["active_account"] = name
        _mark_schema()
        PREFERENCES.commit()
        return True


def save_credentials(account_name, refresh_token, hardware):
    """Persist a rotated refresh token and hardware ID for one account.

    Das aktive Konto bleibt unberührt: der Keep-alive rotiert alle Konten,
    ein Token-Rotationsschritt darf die Auswahl nicht umschalten.
    """
    return _update_account(account_name, {
        "refresh_token": refresh_token,
        "hardware_id": hardware,
    }, allow_virtual_default=True)


def save_refresh_token(account_name, token):
    """Einen rotierten Token im Konto sichern; leer loescht nichts.

    Der Client meldet jede Token-Antwort hier; eine Antwort ohne
    `refresh_token` darf den gespeicherten Zugang nicht ausleeren. Ein
    unbekanntes Konto wird nicht angelegt: ein verspaeteter Callback darf
    ein gerade entferntes Konto nicht wieder erzeugen.
    """
    token = str(token or "").strip()
    if token:
        return _update_account(account_name, {"refresh_token": token},
                               allow_virtual_default=True)
    return False


def clear_refresh_token(account_name):
    """Einen eindeutig abgelehnten Refresh-Token aus dem Konto loeschen."""
    return _update_account(account_name, {"refresh_token": ""})


def save_hardware_id(account_name, hardware):
    """Die Geraete-ID im Konto sichern -- die Config ist ihre einzige Quelle."""
    return _update_account(account_name,
                           {"hardware_id": str(hardware or "")},
                           allow_virtual_default=True)


def account_secrets(account_name):
    """Bekannte Geheimnisse eines Kontos fuer die Log-Redaktion."""
    token = str(account_summary(account_name).get("refresh_token") or "").strip()
    return (token,) if token else ()


def remove_account(name):
    """Remove one saved account and select a remaining account."""
    with _SETTINGS_LOCK:
        raw = PREFERENCES.get("accounts", None)
        named = list(_named_accounts(raw))
        remaining = [(stored, source) for stored, source in named
                     if stored != name]
        if not remaining or len(remaining) == len(named):
            return False
        active = _active_name([{"name": stored} for stored, _ in named])
        PREFERENCES["accounts"] = [source for _stored, source in remaining]
        PREFERENCES["active_account"] = (
            active if active != name else remaining[0][0])
        _mark_schema()
        PREFERENCES.commit()
        return True
