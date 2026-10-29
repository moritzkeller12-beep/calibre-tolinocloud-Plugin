"""Fresh settings store for tolino Cloud Sync."""
import copy
import hashlib
import json
import os
import threading

try:
    from calibre.utils.config import JSONConfig
except ImportError:
    class JSONConfig(dict):
        """In-memory stand-in for settings tests outside Calibre."""

        defaults = {}

        def __init__(self, *_args, **_kwargs):
            super().__init__()

        def commit(self):
            return None


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


# Zustandsjournal: Ein Upload ist nicht rueckgaengig zu machen. Statt nach
# jedem Schritt die ganze Konfiguration zu schreiben, haelt eine kleine
# JSON-Lines-Datei je Konto den letzten Eintrag fest; der volle
# Konto-Schnappschuss laeuft nur noch einmal je Phase. Beim naechsten Start
# wird das Journal zuerst angewandt -- so bleibt die Tolino-ID eines fertigen
# Uploads bekannt und wird nicht ein zweites Mal hochgeladen.
STATE_JOURNAL_DIR = ""


def _state_journal_dir():
    """Ort fuer das Journal; leer heisst «kein Journal verfuegbar».

    Calibres JSONConfig kennt seinen Dateipfad. Tests und eingebettete
    Nutzung ohne Datei fallen auf den vollen Konto-Schnappschuss zurueck.
    """
    if STATE_JOURNAL_DIR:
        return STATE_JOURNAL_DIR
    path = str(getattr(PREFERENCES, "file_path", "") or "")
    if not path:
        return ""
    return os.path.join(os.path.dirname(path) or ".", "tolino-state-journal")


def state_journal_path(account_name):
    """Datei des Kontos; der Name wird gehasht (keine Pfadteile darin)."""
    directory = _state_journal_dir()
    if not directory or not account_name:
        return ""
    digest = hashlib.sha256(str(account_name).encode("utf-8")).hexdigest()
    return os.path.join(directory, digest[:16] + ".jsonl")


def journal_state(account_name, entries):
    """Eintraege sofort anhaengen und fsyncen; True, wenn das gelang."""
    if not _state_journal_dir() or not account_name or not entries:
        return False
    lines = [json.dumps({"key": str(key), "entry": value},
                        ensure_ascii=False)
             for key, value in entries.items()]
    path = state_journal_path(account_name)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except (OSError, TypeError, ValueError):
        return False
    return True


def read_state_journal(account_name):
    """Letzter Eintrag je Schluessel; None bedeutet «entfernt»."""
    path = state_journal_path(account_name)
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return {}
    entries = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            # Abgeschnittene letzte Zeile nach einem Absturz: der Rest gilt.
            continue
        if not isinstance(item, dict) or "key" not in item:
            continue
        entry = item.get("entry")
        entries[str(item["key"])] = entry if isinstance(entry, dict) else None
    return entries


def merge_state_journal(state, account_name):
    """Journal auf einen Zustand anwenden; Rueckgabe: Zahl der Eintraege."""
    if not isinstance(state, dict):
        return 0
    entries = read_state_journal(account_name)
    for key, entry in entries.items():
        if entry is None:
            state.pop(key, None)
        else:
            state[key] = _copy(entry)
    return len(entries)


def clear_state_journal(account_name):
    """Journal nach erfolgreichem Vollschreiben loeschen."""
    path = state_journal_path(account_name)
    if not path:
        return True
    try:
        os.remove(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _copy(value):
    """Tiefe Kopie: Zustandseinträge teilen sich keine Objekte mehr.

    Eine flache Kopie liess die Einträge im `state` dieselben Dicts und
    Listen benutzen wie PREFERENCES; ein Worker hätte damit unbestätigte
    Änderungen in die Einstellungen geschrieben.
    """
    return copy.deepcopy(value)


def _account(value, fallback_name=DEFAULT_ACCOUNT_NAME):
    """Return one clean account record with an independent state mapping."""
    source = value if isinstance(value, dict) else {}
    result = dict(DEFAULT_ACCOUNT)
    result.update({key: source[key] for key in ACCOUNT_KEYS if key in source})
    result["name"] = str(source.get("name") or fallback_name).strip() or fallback_name
    state = source.get("state")
    result["state"] = _copy(state) if isinstance(state, dict) else {}
    return result


def _account_list(raw):
    """Normalize fresh-store account records; never inspect legacy top-level keys."""
    if not isinstance(raw, list) or not raw:
        return [_account({}, DEFAULT_ACCOUNT_NAME)]
    accounts = [_account(item, "account-%d" % (index + 1))
                for index, item in enumerate(raw)]
    names = set()
    for index, item in enumerate(accounts):
        name = item["name"]
        if name in names:
            item["name"] = "%s-%d" % (name, index + 1)
        names.add(item["name"])
    return accounts


def settings():
    """Return a complete current-account snapshot from the fresh store."""
    with _SETTINGS_LOCK:
        accounts = _account_list(PREFERENCES.get("accounts", None))
        names = {item["name"] for item in accounts}
        active = str(PREFERENCES.get("active_account", "") or "")
        if active not in names:
            active = accounts[0]["name"]
        current = next(item for item in accounts if item["name"] == active)
        values = {PREFERRED_FORMATS: _copy(PREFERENCES.get(
            PREFERRED_FORMATS, ["EPUB"]))}
        values.update({key: _copy(current[key]) for key in ACCOUNT_KEYS})
        values.update({
            "name": current["name"],
            "accounts": [_copy(item) for item in accounts],
            "active_account": active,
        })
        # Reines Lesen: geschrieben wird nur in save_account/save_settings.
        return values


def save_account(name, values, active=None):
    """Persist one account without changing any other account's state."""
    with _SETTINGS_LOCK:
        snapshot = settings()
        accounts = snapshot["accounts"]
        existing = next((item for item in accounts if item["name"] == name), {})
        merged = dict(existing)
        merged.update(values)
        normalized = _account(dict(merged, name=name), name)
        for index, item in enumerate(accounts):
            if item["name"] == name:
                accounts[index] = normalized
                break
        else:
            accounts.append(normalized)
        chosen = active or snapshot["active_account"]
        if chosen not in {item["name"] for item in accounts}:
            chosen = normalized["name"]
        PREFERENCES["accounts"] = accounts
        PREFERENCES["active_account"] = chosen
        PREFERENCES.commit()


def account_by_name(name, accounts=None):
    """Return the stored account dict for ``name`` -- never a different one."""
    items = settings()["accounts"] if accounts is None else accounts
    for item in items:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return {}


def set_active_account(name):
    """Switch the active account without touching any stored values."""
    if name not in {item.get("name") for item in settings()["accounts"]}:
        return
    save_account(name, {}, active=name)


def save_credentials(account_name, refresh_token, hardware):
    """Persist a rotated refresh token and hardware ID for one account."""
    if not account_name:
        return
    save_account(account_name, {
        "refresh_token": refresh_token,
        "hardware_id": hardware,
    }, active=account_name)


class CredentialStore:
    """Snapshot and persistence adapter for one account's rotating credentials."""

    def __init__(self, values, account_name=None):
        self.values = values if isinstance(values, dict) else {}
        self.account_name = str(
            account_name or self.values.get("account_name") or "")

    @property
    def refresh_token(self):
        return str(self.values.get("refresh_token") or "")

    @property
    def hardware_id(self):
        return str(self.values.get("hardware_id") or "")

    def snapshot(self):
        return {"refresh_token": self.refresh_token,
                "hardware_id": self.hardware_id}

    def _save(self):
        if self.account_name:
            save_credentials(self.account_name, self.refresh_token,
                             self.hardware_id)

    def save_refresh(self, refresh):
        """Rotierten Token speichern; ein leerer Wert löscht nichts.

        Der Callback des Clients läuft bei jeder Token-Antwort. Eine Antwort
        ohne `refresh_token` darf den gespeicherten Zugang nicht ausleeren.
        """
        token = str(refresh or "").strip()
        if not token:
            return
        self.values["refresh_token"] = token
        self._save()

    def save_hardware(self, hardware):
        self.values["hardware_id"] = str(hardware or "")
        self._save()

    def freshest(self):
        """Adopt the freshest persisted credentials before using a token."""
        if not self.account_name:
            return self.snapshot()
        try:
            stored = account_by_name(self.account_name) or {}
        except Exception:
            return self.snapshot()
        token = str(stored.get("refresh_token") or "").strip()
        if token and token != self.refresh_token:
            self.values["refresh_token"] = token
        hardware = str(stored.get("hardware_id") or "").strip()
        if hardware and hardware != self.hardware_id:
            self.values["hardware_id"] = hardware
        return self.snapshot()


def save_settings(values):
    """Persist global options and the selected account."""
    with _SETTINGS_LOCK:
        current = settings()
        if PREFERRED_FORMATS in values:
            PREFERENCES[PREFERRED_FORMATS] = values[PREFERRED_FORMATS]
        accounts = values.get("accounts", current["accounts"])
        active = values.get("active_account", current["active_account"])
        if not isinstance(accounts, list):
            accounts = current["accounts"]
        active_values = {key: values[key] for key in ACCOUNT_KEYS if key in values}
        if active_values:
            save_account(active, dict(active_values, name=active), active=active)
        else:
            PREFERENCES["accounts"] = [
                _account(item, item.get("name", DEFAULT_ACCOUNT_NAME))
                for item in accounts]
            PREFERENCES["active_account"] = active
            PREFERENCES.commit()
