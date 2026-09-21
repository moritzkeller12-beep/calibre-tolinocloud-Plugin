# Tolino Cloud Sync für Calibre

Plugin-Version: **0.9.19** — synchronisiert Bücher aus Calibre mit der Tolino Cloud.

## Installation

```text
python3 build_plugin.py
```

`tolino_cloud_sync.zip` in Calibre laden: **Einstellungen > Plugins > Plugin aus Datei laden**, dann Calibre neu starten.

## Einrichtung

Im Dialog **Tolino Cloud Sync**: Konto wählen, Partner (z. B. **8 – Books.ch / Orell Füssli**) und Hardware-ID eintragen, Refresh-Token einfügen. Bevorzugte Formate (meist EPUB + PDF) festlegen → **Synchronisierung starten**.

## Refresh-Token beschaffen

**Automatisch (empfohlen):** Im Plugin **„Im Browser anmelden“** klicken — es öffnet sich der Standardbrowser (das eingebaute QtWebEngine-Fenster wurde entfernt, es lief zuverlässig gegen den Bot-Schutz). Bei Keycloak-Partnern wie Orell Füssli öffnet sich direkt der **Web Reader**; nach dem Anmelden (Bibliothek mit Bücherliste) einfach **warten**: Das Plugin übernimmt den Token automatisch, sobald sich der Reader beruhigt hat (Snapshot der Browser-Storages ~20–30 s unverändert). Keycloak-Refresh-Tokens sind **einmal verwendbar** — bei der Übernahme rotiert das Plugin den Token, sodass sich der offene Web Reader dabei abmelden kann; das ist normal und kein Fehler. Wichtig ist, den Reader nach der Anmeldung **in Ruhe zu lassen** (nicht hin- und herklicken), damit nichts in der Wartephase rotiert. Robusteste Variante: nach dem Laden der Bücherliste den Web-Reader-Tab oder den ganzen Browser schließen — die ruhende Sitzung wird genauso erkannt. Kein privates/Inkognito-Fenster verwenden. Alternativ **„Frischen Token aus laufendem Web Reader übernehmen“**, wenn der Web Reader im Standardbrowser geöffnet und angemeldet ist. Beide Wege **validieren gefundene Tokens live** am Token-Endpunkt und speichern nur den frischen, rotierten Token; Browser-Storages enthalten nach Hintergrund-Rotationen oft mehrere Tokens, von denen ältere bereits verbraucht sind — das Plugin probiert sie nacheinander, frischeste zuerst.

**Frische Kandidaten zuerst (0.9.12):** Browser-Storages enthalten nach jeder Hintergrund-Rotation mehrere Token-Generationen. Das Plugin sortiert die Kandidaten jetzt nach Frische: Die neuesten Schreibvorgänge (Chromium Write-Ahead-Log, Firefox-LSNG-Datenbank mit WAL) werden zuerst geprüft, alte Shadow-Kopien (z. B. `webappsstore.sqlite` eines geschlossenen Firefox) zuletzt.

**Verbrauchte Token merken & weiter pollen (0.9.14):** Kandidaten, die der Token-Endpunkt mit `invalid_grant` abgelehnt hat, sind dauerhaft tot und werden nicht mehr erneut getestet — früher lief der Versuch dadurch endlos ins Leere, während der frische Token unentdeckt blieb. Stattdessen pollt das Plugin weiter (bis zu 5 Minuten) und übernimmt automatisch jeden neu geschriebenen Kandidaten: Der Web Reader schreibt nach jeder Hintergrund-Rotation einen frischen Token, und Chromium schreibt seinen Local Storage beim Schließen des Reader-Tabs zuverlässig auf die Festplatte — genau dann ist der allerfrischeste Token lesbar. Der Leser darf dafür ruhig offen bleiben; verbrauchte Kandidaten blockieren den Ablauf nicht mehr.

**JWT-Alter & schlüsselunabhängiges Sweeping (0.9.15):** Tolino-Refresh-Tokens sind signierte JWTs und tragen ihre Ausstellungszeit (`iat`) unverschlüsselt im Token. Das Plugin liest dieses Alter und sortiert die Kandidaten jetzt nach ihrem **echten Token-Alter** statt nach Storage-Heuristik — der erste Validierungsversuch trifft damit den Token, den der Web Reader gerade benutzt. Zusätzlich wird der komplette Storage **schlüsselunabhängig** nach Token-Formen durchsucht: Der Keycloak-Webreader legt seinen aktuellen Tokensatz unter namenlosen Schlüsseln wie `oidc.user:<issuer>:webreader` ab — genau diesen Live-Token haben frühere Versionen übersehen, während sie nur die historischen (verbrauchten) Kopien fanden. Fehlschläge ohne eindeutiges Urteil (Netzwerkfehler, Bot-Schutz, 5xx) markieren einen Kandidaten nicht mehr als tot, sondern werden nach 30 Sekunden erneut versucht. Die Fehlermeldung nennt jetzt das Alter jedes geprüften Kandidaten (z. B. „1x gerade geschrieben, 3x vor 1 Tagen“), damit erkennbar ist, ob der frische Token überhaupt im Storage angekommen ist.

**WebSocket-Parser & Live-Cutoff korrigiert (0.9.19):** Der minimale WebSocket-Client des Live-Grabs hatte einen Frame-Header-Fehler: Das zweite Header-Byte (Länge + Masken-Bit) wurde verworfen, deshalb zerfiel jede DevTools-Antwort an falschen Offsets und der Live-Grab fand „nichts“, obwohl das Anmeldefenster angemeldet war — der berichtete Fehler „ich melde mich an, das Plugin findet nichts“ (Reader-Tab sichtbar auf Port 9223, aber kein Token). Der Parser liest Header jetzt RFC-6455-konform (Regressionstest mit echtem In-Process-WS-Server). Außerdem akzeptiert der Altersfilter des Live-Grabs jetzt Tokens bis 30 Minuten statt 2 Minuten: Keycloak-Refresh-Tokens leben ~1 Stunde, und wer für die Anmeldung länger als zwei Minuten braucht, bekam bisher seinen völlig gültigen Live-Token weggefiltert.

**Flatpak- und Snap-Browser (0.9.18):** Chromium/Brave als Flatpak (unter `~/.var/app/...`) oder Snap werden jetzt über ihren offiziellen Launcher (`flatpak run`/`snap run`) gestartet statt über Binary-Pfade aus dem Store — die laufen ohne die Sandbox-Runtime nicht. Damit funktioniert die Live-Anmeldung auch auf Systemen, die nur Flatpak-Browser installiert haben.

**Live-Token-Grab statt Festplatten-Replay (0.9.17):** Der Web Reader rotiert seinen Refresh-Token bei jedem Hintergrund-Refresh, und Keycloak akzeptiert innerhalb einer Sitzung nur den neuesten Token — jede Kopie im Browser-Storage auf der Festplatte ist damit potentiell schon verbraucht, und ihr Replay kann den Wiederverwendungsschutz auslösen, der die ganze Sitzung widerruft. Die Browser-Anmeldung öffnet jetzt ein **eigenes Chromium-Fenster mit privatem Profil** und liest den aktuellen Token direkt aus dem Speicher der laufenden Web-Reader-Seite (Chrome DevTools Protocol) — einmalig, unmittelbar vor dem Tausch am Token-Endpunkt. Kein Replay, kein Rennen gegen die Rotation, keine abgemeldeten Sitzungen mehr. Der Web Reader rotiert den Token danach normal weiter; sobald die Anmeldung durch ist, kann das Fenster geschlossen werden. Nur wenn kein Chromium-Browser (Chrome/Chromium/Brave/Edge) gefunden wird, greift der bisherige Disk-Scrape-Flow — dort gilt weiterhin: **alle Browser-Fenster schließen**, damit der letzte Token auf die Festplatte geschrieben wird.

**Eine Sitzung, ein Versuch (0.9.16):** Alle Refresh-Token eines Web-Reader-Logins tragen dieselbe Keycloak-Sitzungs-ID (`sid`) im JWT, und innerhalb einer Sitzung ist nur der **neueste** Token gültig. Das Plugin prüft jetzt pro Sitzung nur noch den frischesten Kandidaten und überspringt Sitzungen, von denen bereits ein Token endgültig abgelehnt wurde — ältere Geschwister-Tokens werden nicht mehr gegen den Token-Endpunkt wiedereingesetzt, weil ein Replay Keycloaks Wiederverwendungsschutz auslösen und die ganze Sitzung abschießen kann (das war die Ursache für „wird direkt wieder abgemeldet“). Zudem bekommt jeder Kandidat **genau einen** Endpunkt-Versuch: Der Token-Austausch enthält keine Hardware-ID (verifiziert am Original-Request des Web Readers), die früher drei Wiederholungen pro Token waren also reine Replays. Netzwerk-/Bot-Schutz-Fehlversuche verbrennen einen Kandidaten nicht mehr — nur eine eindeutige Keycloak-Ablehnung (`invalid_grant`) markiert ihn als verbraucht.

**Token-Keep-alive:** Keycloak-Refresh-Tokens verfallen nach ca. einer Stunde Untätigkeit (`refresh_expires_in` ≈ 3598). Solange Calibre läuft, rotiert das Plugin deshalb alle 45 Minuten still den gespeicherten Token und speichert die Rotation — der Token bleibt zwischen zwei Synchronisierungen gültig. Nebenwirkung: Der im Browser angemeldete Web Reader muss sich gelegentlich neu anmelden, da auch seine Token bei der Rotation veralten.

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
