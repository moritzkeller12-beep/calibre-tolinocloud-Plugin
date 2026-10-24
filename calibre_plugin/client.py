"""Create authenticated Tolino clients while coordinating token use."""
from .sessions import lock_keepalive, token_session, unlock_keepalive
from .tolino import (BUSY_MESSAGE, RELOGIN, TolinoAuthError, TolinoClient)


def make_client(store, lock_timeout=5):
    """Log in with the freshest saved credentials for one account."""
    if not store.refresh_token:
        raise TolinoAuthError("Noch nicht angemeldet. " + RELOGIN, code="A03")
    if not lock_keepalive(lock_timeout):
        raise TolinoAuthError(BUSY_MESSAGE, code="S91")
    try:
        with token_session():
            store.freshest()
            client = TolinoClient(
                store.values.get("partner_id"), store.hardware_id,
                store.refresh_token,
                token_callback=store.save_refresh,
                hardware_callback=store.save_hardware,
                refresh_provider=store.freshest)
            client.login()
    finally:
        unlock_keepalive()
    return client
