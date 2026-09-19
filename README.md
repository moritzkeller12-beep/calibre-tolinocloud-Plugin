# Tolino Cloud Sync für Calibre

Plugin-Version: **0.9.6** — synchronisiert Bücher aus Calibre mit der Tolino Cloud.

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

## 403-Fehler verstehen (0.9.6)

Der Token-Endpunkt sitzt hinter einem Bot-Schutz, der TLS-Fingerprints prüft. Das Plugin versucht deshalb der Reihe nach:

1. **curl_cffi** (Chrome-TLS, wie die Referenz pytolino) — optional installierbar: `pip install curl_cffi`
2. **curl** (Kommandozeilen-Client)
3. **urllib** (Python-Standard, letzter Fallback)

Welcher Transport benutzt wurde, steht in der Diagnose (`transport`). Seit 0.9.6 enthält die Fehlermeldung bei 403 zusätzlich die **bereinigte Antwort des Servers** — damit erkennt man, ob Bot-Schutz (z. B. "Access denied") oder ein verbrauchter Refresh-Token (`invalid_grant`) die Ursache ist. Bei `invalid_grant` fordert das Plugin zum Neubeziehen des Tokens im Web Reader auf.

## Weitere Funktionen

- **Hardware-ID automatisch auflösen** aus der Tolino-Geräteliste (nach der Anmeldung)
- **Buch herunterladen** aus der Tolino Cloud (`TolinoClient.download(deliverable_id)`)
- **Sammlungen verwalten** (`add_to_collection` / `remove_from_collection`) und **Gelesen-Markierung** (`mark_read`) über die Sync-Data-API
- **Refresh-Token-Ablaufzeit** wird aus der Token-Antwort gelesen (Diagnose zeigt `refresh_expires_in`)
