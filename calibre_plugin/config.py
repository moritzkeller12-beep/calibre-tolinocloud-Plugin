try:
    from calibre.utils.config import JSONConfig
except ImportError:
    class JSONConfig(dict):
        """Small in-memory stand-in so settings tests do not need Calibre."""

        defaults = {}

        def __init__(self, *_args, **_kwargs):
            super().__init__()

        def commit(self):
            return None


PARTNER_SCHEMA_VERSION = 2


def _force_legacy_partner_id(value):
    """Unconditionally map a historic partner ID (lazy import)."""
    try:
        from .tolino import force_legacy_partner_id
    except ImportError:
        try:
            from tolino import force_legacy_partner_id
        except ImportError:
            return value
    return force_legacy_partner_id(value)


def _migrate_partner_ids(accounts):
    """One-time rewrite of historic plugin partner IDs to consecutive ones.

    Guarded by a schema marker so it only ever runs once: the legacy ID
    space (3, 4, 6, 8, 13, 23, 30) overlaps the new consecutive one, so a
    stored ID can only be interpreted as legacy before the marker exists.
    """
    try:
        version = int(PREFERENCES.get("partner_schema_version", 1) or 1)
    except (TypeError, ValueError):
        version = 1
    if version >= PARTNER_SCHEMA_VERSION:
        return accounts
    migrated = []
    for item in accounts:
        if isinstance(item, dict):
            item = dict(item)
            item["partner_id"] = _force_legacy_partner_id(item.get("partner_id"))
        migrated.append(item)
    PREFERENCES["partner_schema_version"] = PARTNER_SCHEMA_VERSION
    PREFERENCES["accounts"] = migrated
    if migrated:
        first = migrated[0]
        if isinstance(first, dict):
            PREFERENCES["partner_id"] = first.get("partner_id")
    PREFERENCES.commit()
    return migrated


PREFERENCES = JSONConfig("plugins/Tolino Cloud Sync")
DEFAULT_ACCOUNT_NAME = "default"
ACCOUNT_KEYS = ("partner_id", "hardware_id", "refresh_token", "username",
                "password", "state")
DEFAULT_ACCOUNT = {
    "name": DEFAULT_ACCOUNT_NAME,
    "partner_id": 1,
    "hardware_id": "",
    "refresh_token": "",
    "username": "",
    "password": "",
    "state": {},
}
DEFAULTS = {
    **DEFAULT_ACCOUNT,
    "partner_schema_version": PARTNER_SCHEMA_VERSION,
    "preferred_formats": ["EPUB", "PDF"],
    "upload_covers": True,
    "enable_deletions": False,
    "use_tolino_column": True,
    "tolino_column_notice_shown": False,
    "accounts": [],
    "active_account": DEFAULT_ACCOUNT_NAME,
}
PREFERENCES.defaults = DEFAULTS


def _copy(value):
    return value.copy() if isinstance(value, (dict, list)) else value


def _account(value, fallback_name=DEFAULT_ACCOUNT_NAME):
    source = value if isinstance(value, dict) else {}
    result = dict(DEFAULT_ACCOUNT)
    result.update({key: source[key] for key in ACCOUNT_KEYS if key in source})
    result["name"] = str(source.get("name") or fallback_name).strip() or fallback_name
    result["state"] = result["state"] if isinstance(result["state"], dict) else {}
    return result


def _migrate_accounts():
    """Migrate the original single-account keys into one named account."""
    raw_accounts = PREFERENCES.get("accounts", None)
    if isinstance(raw_accounts, list) and raw_accounts:
        accounts = [_account(item, "account-%d" % (index + 1))
                    for index, item in enumerate(raw_accounts)]
        accounts = _migrate_partner_ids(accounts)
    else:
        legacy = {key: PREFERENCES.get(key, DEFAULTS[key]) for key in ACCOUNT_KEYS}
        accounts = _migrate_partner_ids([_account(legacy)])
    names = set()
    for index, item in enumerate(accounts):
        name = item["name"]
        if name in names:
            item["name"] = "%s-%d" % (name, index + 1)
        names.add(item["name"])
    active = str(PREFERENCES.get("active_account", "") or "")
    if active not in names:
        active = accounts[0]["name"]
    return accounts, active


def settings():
    """Return a complete snapshot, migrating old single-account preferences."""
    accounts, active = _migrate_accounts()
    values = {}
    changed = False
    for key, default in DEFAULTS.items():
        if key in ("accounts", "active_account") or key in ACCOUNT_KEYS:
            continue
        value = PREFERENCES.get(key, None)
        if value is None:
            value = _copy(default)
            changed = True
        values[key] = _copy(value)
    current = next(item for item in accounts if item["name"] == active)
    values.update({key: _copy(current[key]) for key in ACCOUNT_KEYS})
    values["name"] = current["name"]
    values["accounts"] = [_copy(item) for item in accounts]
    values["active_account"] = active
    # Keep legacy top-level fields in sync for older plugin versions.
    persisted = PREFERENCES.get("accounts", None)
    if persisted != accounts or PREFERENCES.get("active_account") != active:
        changed = True
    if changed:
        PREFERENCES["accounts"] = accounts
        PREFERENCES["active_account"] = active
        for key, value in current.items():
            PREFERENCES[key] = value
        for key in ("preferred_formats", "upload_covers", "enable_deletions",
                    "use_tolino_column",
                    "tolino_column_notice_shown"):
            PREFERENCES[key] = values[key]
        PREFERENCES.commit()
    return values


def save_account(name, values, active=None):
    """Persist one account without changing any other account's state."""
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
    if chosen == normalized["name"]:
        for key in ACCOUNT_KEYS:
            PREFERENCES[key] = normalized[key]
    PREFERENCES.commit()


def save_settings(values):
    """Persist global options and the selected account (legacy-compatible)."""
    current = settings()
    for key in ("preferred_formats", "upload_covers", "enable_deletions",
                "use_tolino_column",
                "tolino_column_notice_shown"):
        if key in values:
            PREFERENCES[key] = values[key]
    accounts = values.get("accounts", current["accounts"])
    active = values.get("active_account", current["active_account"])
    if not isinstance(accounts, list):
        accounts = current["accounts"]
    active_values = {key: values[key] for key in ACCOUNT_KEYS if key in values}
    if active_values:
        save_account(active, dict(active_values, name=active), active=active)
    else:
        PREFERENCES["accounts"] = [_account(item, item.get("name", DEFAULT_ACCOUNT_NAME))
                                    for item in accounts]
        PREFERENCES["active_account"] = active
        PREFERENCES.commit()
