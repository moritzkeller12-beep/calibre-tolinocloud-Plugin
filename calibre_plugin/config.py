"""Fresh settings store for tolino Cloud Sync."""
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
# Nur globale Schluessel: Kontofelder stehen in `accounts` und werden dort
# gelesen -- als Top-Level-Default waeren sie stille Leichen.
DEFAULTS = {PREFERRED_FORMATS: ["EPUB"]}

_SETTINGS_LOCK = threading.RLock()


def load_state(raw):
    """Load the saved state mapping; malformed or absent values are empty."""
    return raw if isinstance(raw, dict) else {}


def _copy(value):
    return value.copy() if isinstance(value, (dict, list)) else value


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
            PREFERRED_FORMATS, DEFAULTS[PREFERRED_FORMATS]))}
        values.update({key: _copy(current[key]) for key in ACCOUNT_KEYS})
        values.update({
            "name": current["name"],
            "accounts": [_copy(item) for item in accounts],
            "active_account": active,
        })
        # Persist only the current schema and its explicit defaults.
        changed = False
        if PREFERRED_FORMATS not in PREFERENCES:
            PREFERENCES[PREFERRED_FORMATS] = _copy(values[PREFERRED_FORMATS])
            changed = True
        if PREFERENCES.get("accounts") != accounts:
            PREFERENCES["accounts"] = accounts
            changed = True
        if PREFERENCES.get("active_account") != active:
            PREFERENCES["active_account"] = active
            changed = True
        if changed:
            PREFERENCES.commit()
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
        self.values["refresh_token"] = str(refresh or "")
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
