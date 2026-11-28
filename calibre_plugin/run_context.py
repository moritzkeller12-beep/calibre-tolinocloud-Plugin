"""Explicit account and option context for one comparison or sync run.

Der Kontext traegt nur, was den Lauf ueber den Kontowechsel hinweg
identifiziert: Konto, Buchhaendler, Formate und Spalten. Zugangsdaten
stehen ausschliesslich in der Config; der Vault liest sie unter seinem
Lock. Laufdaten (Metadaten, Sync-Zustand) sind eigene Parameter.
"""
from dataclasses import dataclass, field

from .config import account_secrets

DEFAULT_PREFERRED_FORMATS = ("EPUB",)


@dataclass(frozen=True)
class RunContext:
    """Konto und Optionen eines Laufs."""

    account_name: str = ""
    preferred_formats: tuple = DEFAULT_PREFERRED_FORMATS
    columns: dict = field(default_factory=dict)
    library_scope: str = ""

    def __post_init__(self):
        object.__setattr__(self, "account_name", str(self.account_name or ""))
        object.__setattr__(self, "preferred_formats", tuple(
            self.preferred_formats or DEFAULT_PREFERRED_FORMATS))
        object.__setattr__(self, "columns", dict(self.columns or {}))
        object.__setattr__(self, "library_scope",
                           str(self.library_scope or ""))

    @classmethod
    def from_values(cls, values, columns=None, library_scope=""):
        """Aus dem GUI-Snapshot des Dashboards bauen."""
        values = values if isinstance(values, dict) else {}
        return cls(
            account_name=values.get("account_name") or "",
            preferred_formats=values.get("preferred_formats"),
            columns=columns or {},
            library_scope=library_scope,
        )

    def secrets(self):
        """Geheimnisse des Kontos fuer die Redaktion von Fehlertexten."""
        return account_secrets(self.account_name)
