"""Explicit account and option context for one comparison or sync run.

Der Kontext traegt nur, was den Lauf ueber den Kontowechsel hinweg
identifiziert: Konto, Buchhaendler, Geraet, Formate und Spalten. Die
Laufdaten (Metadaten, Sync-Zustand) sind eigene Parameter und werden nicht
in diesen Kontext geschrieben.
"""
from dataclasses import dataclass, field

DEFAULT_PREFERRED_FORMATS = ("EPUB",)


@dataclass
class RunContext:
    """Konto und Optionen eines Laufs."""

    account_name: str = ""
    preferred_formats: tuple = DEFAULT_PREFERRED_FORMATS
    columns: dict = field(default_factory=dict)
    # Der CredentialStore schreibt Rotationen in genau dieses Mapping.
    credentials: dict = field(default_factory=dict)

    def __post_init__(self):
        self.account_name = str(self.account_name or "")
        self.preferred_formats = tuple(
            self.preferred_formats or DEFAULT_PREFERRED_FORMATS)
        self.columns = dict(self.columns or {})
        self.credentials = dict(self.credentials or {})
        self.credentials.setdefault("partner_id", 1)
        self.credentials.setdefault("hardware_id", "")

    @classmethod
    def from_values(cls, values, columns=None):
        """Aus dem GUI-Snapshot des Dashboards bauen."""
        values = values if isinstance(values, dict) else {}
        credentials = {
            "partner_id": values.get("partner_id", 1),
            "hardware_id": str(values.get("hardware_id") or ""),
        }
        # Ein von Hand eingetippter Token ist Teil des Laufs; der
        # gespeicherte liegt im Konto und wird vom Vault gelesen.
        typed = str(values.get("refresh_token") or "").strip()
        if typed:
            credentials["refresh_token"] = typed
        return cls(
            account_name=values.get("account_name") or "",
            preferred_formats=(values.get("preferred_formats")
                               or DEFAULT_PREFERRED_FORMATS),
            columns=(columns if columns is not None
                     else values.get("columns") or {}),
            credentials=credentials,
        )

    @property
    def partner_id(self):
        return self.credentials.get("partner_id", 1)

    @property
    def hardware_id(self):
        return str(self.credentials.get("hardware_id") or "")

    @hardware_id.setter
    def hardware_id(self, value):
        self.credentials["hardware_id"] = str(value or "")

    @property
    def refresh_token(self):
        return str(self.credentials.get("refresh_token") or "")

    def clone(self):
        """Unabhaengige Kopie fuer einen Worker."""
        return RunContext(
            account_name=self.account_name,
            preferred_formats=self.preferred_formats,
            columns=self.columns,
            credentials=self.credentials,
        )

    def secrets(self):
        """Zugangsdaten fuer die Redaktion von Fehlertexten."""
        return (self.refresh_token,)
