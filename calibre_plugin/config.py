from calibre.utils.config import JSONConfig


PREFERENCES = JSONConfig("plugins/Tolino Cloud Sync")
PREFERENCES.defaults = {
    "partner_id": 3,
    "hardware_id": "",
    "refresh_token": "",
    "username": "",
    "password": "",
    "preferred_formats": ["EPUB", "PDF"],
    "upload_covers": True,
    "enable_deletions": False,
    "state": {},
}


def settings():
    """Return a copy so callers cannot accidentally mutate persisted values."""
    return dict(PREFERENCES)


def save_settings(values):
    for key, value in values.items():
        PREFERENCES[key] = value
    PREFERENCES.commit()

