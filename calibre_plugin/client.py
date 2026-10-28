"""Create authenticated Tolino clients through the account token vault."""
from .token_vault import TOKEN_VAULT
from .tolino import TolinoClient


def make_client(store, lock_timeout=5):
    """Log in with the freshest stored token for an account."""
    return TOKEN_VAULT.login(store, lock_timeout, client_factory=TolinoClient)
