"""Fresh settings store for tolino Cloud Sync."""
import copy
import threading

from calibre.utils.config import JSONConfig

# Keep the historic Lite namespace: changing it would hide existing user
# settings and credentials in Calibre's preferences store.
PREFERENCES = JSONConfig("plugins/Tolino Cloud Sync Lite")
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


def load_state(raw):
    """Load the saved state mapping; malformed or absent values are empty."""
    return raw if isinstance(raw, dict) else {}


# Zustandseinträge müssen tief kopiert werden: sonst teilen Worker und
# Einstellungen dieselben Dicts und Listen.
def _account(value, fallback_name=DEFAULT_ACCOUNT_NAME):
    """Return one clean account record with an independent state mapping."""
    source = value if isinstance(value, dict) else {}
    result = dict(DEFAULT_ACCOUNT)
    result.update({key: source[key] for key in ACCOUNT_KEYS if key in source})
    result["name"] = str(source.get("name") or fallback_name).strip() or fallback_name
    state = source.get("state")
    result["state"] = copy.deepcopy(state) if isinstance(state, dict) else {}
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


def _account_list(raw):
    """Normalize fresh-store account records; never inspect legacy top-level keys."""
    return [_account(dict(value, name=name), name)
            for name, value in _named_accounts(raw)]


def _accounts_and_active():
    """Normalisierte Konten und aktives Konto -- EINE Kopie je Aufruf.

    `_account_list` liefert bereits unabhängige Konten (inklusive tiefer
    Zustandskopie). Ein zweiter `deepcopy` über alle Konten wäre reine
    Verschwendung; die aktuellen Felder werden nur einmal gezielt kopiert.
    """
    accounts = _account_list(PREFERENCES.get("accounts", None))
    names = {item["name"] for item in accounts}
    active = str(PREFERENCES.get("active_account", "") or "")
    if active not in names:
        active = accounts[0]["name"]
    return accounts, active


def settings():
    """Return a complete current-account snapshot from the fresh store."""
    with _SETTINGS_LOCK:
        accounts, active = _accounts_and_active()
        current = next(item for item in accounts if item["name"] == active)
        # Reines Lesen: geschrieben wird nur in save_account/remove_account.
        return {
            PREFERRED_FORMATS: copy.deepcopy(
                PREFERENCES.get(PREFERRED_FORMATS, ["EPUB"])),
            "partner_id": copy.deepcopy(current["partner_id"]),
            "hardware_id": copy.deepcopy(current["hardware_id"]),
            "accounts": accounts,
            "active_account": active,
        }


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
        accounts, current_active = _accounts_and_active()
        existing = next((item for item in accounts if item["name"] == name), {})
        merged = dict(existing)
        merged.update(values)
        normalized = _account(dict(merged, name=name), name)
        for index, item in enumerate(accounts):
            if item["name"] == name:
                accounts[index] = normalized
                break
        else:
            if not create:
                return False
            accounts.append(normalized)
        chosen = active or current_active
        if chosen not in {item["name"] for item in accounts}:
            chosen = normalized["name"]
        PREFERENCES["accounts"] = accounts
        PREFERENCES["active_account"] = chosen
        if preferred_formats is not None:
            PREFERENCES[PREFERRED_FORMATS] = copy.deepcopy(
                list(preferred_formats))
        PREFERENCES.commit()
        return True


def account_by_name(name):
    """Return one account without copying every other account's state."""
    with _SETTINGS_LOCK:
        accounts = PREFERENCES.get("accounts", None)
        for stored_name, value in _named_accounts(accounts):
            if stored_name == name:
                return _account(dict(value, name=stored_name), stored_name)
    return {}


def _update_account(name, values, allow_default=False):
    """Update one existing account; callbacks never recreate deleted accounts."""
    if not name:
        return False
    with _SETTINGS_LOCK:
        raw = PREFERENCES.get("accounts", None)
        records = raw if isinstance(raw, list) else []
        if records:
            for index, (stored_name, source) in enumerate(
                    _named_accounts(records)):
                if stored_name != name:
                    continue
                accounts = list(records)
                accounts[index] = _account(
                    dict(source, **values, name=stored_name), stored_name)
                PREFERENCES["accounts"] = accounts
                PREFERENCES.commit()
                return True
        # Auf einer frischen Installation ist das Default-Konto nur virtuell.
        if not (allow_default and not records and name == DEFAULT_ACCOUNT_NAME):
            return False
        PREFERENCES["accounts"] = [_account(dict(values, name=name), name)]
        if not PREFERENCES.get("active_account"):
            PREFERENCES["active_account"] = name
        PREFERENCES.commit()
        return True


def set_active_account(name):
    """Switch the active account without touching any stored values."""
    if not account_by_name(name):
        return
    save_account(name, {}, active=name)


def save_credentials(account_name, refresh_token, hardware):
    """Persist a rotated refresh token and hardware ID for one account.

    Das aktive Konto bleibt unberührt: der Keep-alive rotiert alle Konten,
    ein Token-Rotationsschritt darf die Auswahl nicht umschalten.
    """
    return _update_account(account_name, {
        "refresh_token": refresh_token,
        "hardware_id": hardware,
    }, allow_default=True)


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
                               allow_default=True)
    return False


def clear_refresh_token(account_name):
    """Einen eindeutig abgelehnten Refresh-Token aus dem Konto loeschen."""
    return _update_account(account_name, {"refresh_token": ""})


def save_hardware_id(account_name, hardware):
    """Die Geraete-ID im Konto sichern -- die Config ist ihre einzige Quelle."""
    return _update_account(account_name,
                           {"hardware_id": str(hardware or "")},
                           allow_default=True)


def account_secrets(account_name):
    """Bekannte Geheimnisse eines Kontos fuer die Log-Redaktion."""
    account = account_by_name(account_name) or {}
    token = str(account.get("refresh_token") or "").strip()
    return (token,) if token else ()


def remove_account(name):
    """Remove one saved account and select a remaining account."""
    with _SETTINGS_LOCK:
        accounts, active = _accounts_and_active()
        remaining = [item for item in accounts if item["name"] != name]
        if not remaining or len(remaining) == len(accounts):
            return False
        PREFERENCES["accounts"] = remaining
        PREFERENCES["active_account"] = (
            active if active != name else remaining[0]["name"])
        PREFERENCES.commit()
        return True
