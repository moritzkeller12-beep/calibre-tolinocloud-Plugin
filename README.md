# Tolino Cloud Sync für Calibre

Plugin-Version: **0.9.8** — synchronisiert Bücher aus Calibre mit der Tolino Cloud.

## Installation

```text
python3 build_plugin.py
```

`tolino_cloud_sync.zip` in Calibre laden: **Einstellungen > Plugins > Plugin aus Datei laden**, dann Calibre neu starten.

## Einrichtung

Im Dialog **Tolino Cloud Sync**: Konto wählen, Partner (z. B. **8 – Books.ch / Orell Füssli**) und Hardware-ID eintragen, Refresh-Token einfügen. Bevorzugte Formate (meist EPUB + PDF) festlegen → **Synchronisierung starten**.

## Refresh-Token beschaffen

**Automatisch (empfohlen):** Im Plugin **„Im Browser anmelden“** klicken — es öffnet sich der Standardbrowser (das eingebaute QtWebEngine-Fenster wurde entfernt, es lief zuverlässig gegen den Bot-Schutz). Bei Keycloak-Partnern wie Orell Füssli öffnet sich die Anmeldeseite; nach dem Anmelden im **Web Reader** (Bibliothek mit Bücherliste) sammelt das Plugin die Refresh-Token-Kandidaten aus den Browser-Storages und validiert jeden live am Token-Endpunkt. Alternativ **„Frischen Token aus laufendem Web Reader übernehmen“**, wenn der Web Reader im Standardbrowser geöffnet und angemeldet ist. Beide Wege **validieren gefundene Tokens live** am Token-Endpunkt und speichern nur den frischen, rotierten Token; Browser-Storages enthalten nach Hintergrund-Rotationen oft mehrere Tokens, von denen ältere bereits verbraucht sind.

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

1. **curl_cffi** (Chrome-TLS, wie die Referenz pytolino) — **erforderlich bei aktivem Bot-Schutz**: `python3 -m pip install --user curl_cffi`
2. **curl** (Kommandozeilen-Client)
3. **urllib** (Python-Standard, letzter Fallback)

Zusätzlich sendet das Plugin beim Token-Request dieselbe **Sec-Fetch-/Client-Hints-Header-Familie** wie der Web Reader selbst (per DevTools verifiziert) — der Bot-Schutz blockt Anfragen ohne diese Header mit der Seite „Zugriff geblockt" (HTTP 403), noch bevor der Token geprüft wird.

**Entscheidend ist der TLS-Fingerprint.** Verifiziert per Direkttest gegen den Endpunkt:

| Transport | Ergebnis |
|---|---|
| System-curl 7.81 (Ubuntu) | ❌ HTTP 403 „Zugriff geblockt" |
| curl_cffi (Chrome-Imitation) | ✅ HTTP 400 `invalid_grant` — WAF passiert, OAuth-Antwort korrekt |

Wenn die Fehlermeldung bei 403 den Hinweis "Install curl_cffi" enthält, ist curl_cffi in Calibres Python-Umgebung nicht installiert und die WAF blockt den Fallback-Transport:

```bash
# In die Python-Umgebung installieren, mit der Calibre läuft:
python3 -m pip install --user curl_cffi
# oder systemweit: pip install curl_cffi
```

Danach **Calibre neu starten** (Plugins laden Python-Module beim Start). Die Diagnose zeigt unter `curl_cffi: true/false`, ob das Modul gefunden wurde, und unter `transport`, welcher Transport den Request ausgeführt hat.

### Ein-Klick-Installation (0.9.7)

Im Plugin-Dialog gibt es den Button **„curl_cffi installieren (Bot-Schutz umgehen)“**. Er:

1. lädt die **offiziellen, versionierten Wheels** (curl_cffi 0.16.3, cffi 2.1.1 — das **passende cffi-Wheel für die jeweilige Python-Version** 3.10–3.14, pycparser 2.23, certifi) direkt von PyPI,
2. **verifiziert jede Datei per SHA-256** (Prüfsummen aus den PyPI-Release-Metadaten; bei Abweichung bricht die Installation ab),
3. entpackt sie in den Calibre-Plugin-Ordner (`<Calibre-Konfig>/plugins/curl_cffi-libs/`) und macht sie sofort importierbar — **ohne pip, ohne Neustart**.

Das funktioniert auch in Calibres eingefrorener Python-Umgebung (Windows/macOS), wo `pip install --user` die Module nicht in Reichweite des Plugins bringt. Wer es bevorzugt, kann weiterhin manuell installieren: `python3 -m pip install --user curl_cffi` + Calibre-Neustart.

Welcher Transport benutzt wurde, steht in der Diagnose (`transport`). Seit 0.9.6 enthält die Fehlermeldung bei 403 zusätzlich die **bereinigte Antwort des Servers** — damit erkennt man, ob Bot-Schutz (z. B. "Access denied") oder ein verbrauchter Refresh-Token (`invalid_grant`) die Ursache ist. Bei `invalid_grant` fordert das Plugin zum Neubeziehen des Tokens im Web Reader auf.

## Weitere Funktionen

- **Hardware-ID automatisch auflösen** aus der Tolino-Geräteliste (nach der Anmeldung)
- **Buch herunterladen** aus der Tolino Cloud (`TolinoClient.download(deliverable_id)`)
- **Sammlungen verwalten** (`add_to_collection` / `remove_from_collection`) und **Gelesen-Markierung** (`mark_read`) über die Sync-Data-API
- **Refresh-Token-Ablaufzeit** wird aus der Token-Antwort gelesen (Diagnose zeigt `refresh_expires_in`)
