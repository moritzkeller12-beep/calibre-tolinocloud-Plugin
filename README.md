# Tolino Cloud Sync für Calibre

Plugin-Version: **0.9.2** — synchronisiert Bücher aus Calibre mit der Tolino Cloud.

## Installation

```text
python3 build_plugin.py
```

`tolino_cloud_sync.zip` in Calibre laden: **Einstellungen > Plugins > Plugin aus Datei laden**, dann Calibre neu starten.

## Einrichtung

Im Dialog **Tolino Cloud Sync**: Konto wählen, Partner (z. B. **8 – Books.ch / Orell Füssli**) und Hardware-ID eintragen, Refresh-Token einfügen. Bevorzugte Formate (meist EPUB + PDF) festlegen → **Synchronisierung starten**.

## Refresh-Token beschaffen

**Automatisch:** Im Plugin **„Im Browser anmelden"** oder **„Token aus Browser extrahieren"** klicken (im Tolino **Web Reader** angemeldet sein, nicht nur im Shop).

**Manuell (Fallback):**

1. **F12** → Netzwerk-Tab → Aufnahme an + „Preserve log"
2. Im Web Reader des Partners anmelden (nicht abmelden!)
3. Filter `registerhw` → Request-Header `hardware_id` kopieren
4. Filter `token` → Request `/auth/oauth2/token` → Antwort → `refresh_token` kopieren
5. Beide Werte ins Plugin eintragen und speichern

**Wichtig:** Token nach dem Eintragen direkt verwenden, nicht wiederholt testen — Tolino-Tokens werden nach einmaliger Nutzung ungültig (`invalid_grant`).

## Lokale Validierung

```text
python3 -m unittest calibre_plugin.test_sync
python3 build_plugin.py
```
