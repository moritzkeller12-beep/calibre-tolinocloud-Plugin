try:
    from calibre.utils.config import JSONConfig
except ImportError:
    class JSONConfig(dict):
        """Small in-memory stand-in so settings tests do not need Calibre."""

        defaults = {}

        def __init__(self, *_args, **_kwargs):
            super().__init__()

        def get(self, key, default=None):
            return dict.get(self, key, default)

        def commit(self):
            return None


PREFERENCES = JSONConfig("plugins/Tolino Cloud Sync")
DEFAULTS = {
    "partner_id": 3,
    "hardware_id": "",
    "refresh_token": "",
    "username": "",
    "password": "",
    "preferred_formats": ["EPUB", "PDF"],
    "upload_covers": True,
    "enable_deletions": False,
    "tolino_column_notice_shown": False,
    "state": {},
}
PREFERENCES.defaults = DEFAULTS


def settings():
    """Return and persist a complete settings snapshot for old configurations."""
    values = {}
    changed = False
    for key, default in DEFAULTS.items():
        value = PREFERENCES.get(key, None)
        if value is None:
            value = default.copy() if isinstance(default, (dict, list)) else default
            PREFERENCES[key] = value
            changed = True
        values[key] = value.copy() if isinstance(value, (dict, list)) else value
    if not isinstance(values["preferred_formats"], list):
        values["preferred_formats"] = list(DEFAULTS["preferred_formats"])
        PREFERENCES["preferred_formats"] = values["preferred_formats"]
        changed = True
    if not isinstance(values["state"], dict):
        values["state"] = {}
        PREFERENCES["state"] = {}
        changed = True
    if changed:
        PREFERENCES.commit()
    return values


def save_settings(values):
    for key, value in values.items():
        PREFERENCES[key] = value
    PREFERENCES.commit()
