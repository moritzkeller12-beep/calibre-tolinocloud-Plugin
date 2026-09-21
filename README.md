# Tolino Cloud Sync für Calibre

Plugin-Version: **0.9.26** — synchronisiert Bücher aus Calibre mit der Tolino Cloud.

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

**Blob-bewusste Invalidierung + Storage-Polling als Fangweg (0.9.26):** Der Feldbefund zur 0.9.25: „1 Refresh-Kandidat, 0 Hardware, keine frische Rotation" — die Access-Token-Invalidierung hatte nichts getroffen, denn der Reader legt seine Tokens typischerweise als **JSON-Blob** ab (`oidc.user:…` mit `access_token` als *Eigenschaft*), während die Invalidierung nur Key-Namen matchte. Dazu läuft der Token-POST möglicherweise über den **Service Worker** und wäre für die Fetch-Interception unsichtbar. v0.9.26 behebt beides: Die Invalidierung **parst JSON-Blobs** und ersetzt deren `access_token`-Eigenschaft (plus `expires_at = 0`), sodass der Reader proaktiv erneuert statt den toten Token zu verwenden — Refresh-Tokens bleiben unberührt. Traf auch das nichts (unbekanntes Layout), wird der **gesamte Storage geleert**: Der Reader gilt dann als abgemeldet, re-authentifiziert sich beim Reload aber **still über das Keycloak-SSO-Cookie** und schreibt brandneue Tokens. Als zweiter Fangweg neben der Fetch-Interception **pollt das Plugin den Seiten-Speicher** (alle 3 s) und übernimmt die frisch rotierten Tokens — immun gegen Service-Worker-Routing; bereits erfolglos getauschte Kopien werden per `exclude_refresh` ausgeschlossen.

**Rotations-Anstoß durch Access-Token-Invalidierung (0.9.25):** Der Feldbefund zur 0.9.24: Der Reader ist lebendig und rotiert selbst erfolgreich — aber ein `Page.reload` allein erzwingt **keine** Rotation, solange sein Access-Token noch gültig ist (~50 Minuten). Der Reader holt sich beim Reload einfach die weiterhin gültigen Tokens und ruft den Token-Endpunkt gar nicht erst auf. v0.9.25 stößt die Rotation deshalb **aktiv an**: Vor dem Reload schreibt das Plugin per CDP (dieselbe WebSocket-Verbindung) einen ungültigen Wert in die **Access-Token**-Einträge des Seiten-Speichers — die Refresh-Tokens bleiben unberührt. Die frisch geladene Seite startet mit totem Access-Token, ihr nächster API-Call läuft auf 401, und der Reader tauscht **sofort selbst** mit seinem aktuellen Refresh-Token — dessen Antwort fängt das Plugin ab, garantiert ungenutzt, inklusive frischem Access-Token (kein zweiter POST nötig) und mit der `hardware-id` der aktuellen Sitzung aus den mitgelesenen Request-Headern.

**Erzwungene Token-Rotation + Hardware-ID-Mitlesen (0.9.24):** Der Feldbefund nach dem Browser-Tausch-Fix: Der im Seiten-Speicher liegende Token war **bereits verbraucht** (der Reader rotiert intern weiter, die neue Kopie erreicht den Storage erst verzögert) — und die konfigurierte Hardware-ID (`da284d4b…`) gehörte zu einer **älteren Anmeldung**, während die aktuelle Reader-Sitzung mit einer anderen Geräte-ID lief (`eb22e4cf…`, sichtbar im Netzwerk-Dump). v0.9.24 zieht deshalb beide Register: Nach einer abgelehnten Storage-Exchanges **erzwingt das Plugin die nächste Token-Rotation selbst** (`Page.reload` per CDP) und fängt die frische Token-Antwort ab, **bevor** der Reader sie verbraucht — dieser Token ist garantiert ungenutzt. Parallel werden die Reader-Requests passiv mitgelesen: deren `hardware-id`/`device-id`-Header tragen die **Geräte-ID der aktuellen Sitzung**, die nun beim Tausch und beim Speichern verwendet wird (die mitgelesene ID gewinnt gegen die konfigurierte). Die Token-Antwort-Verarbeitung läuft jetzt auf beiden Tausch-Wegen (Browser-Exchange und Plugin-POST) über dieselbe `_apply_token_response`-Logik inkl. sofortigem Speichern des rotierten Tokens.

**Token-Tausch im Browser ausgeführt (0.9.23):** Der entscheidende Feldbefund: Der Web Reader tauscht denselben Refresh-Token **im Browser erfolgreich** (echter Chrome-TLS-Fingerprint), während der Plugin-eigene POST am selben Endpunkt mit dem WAF-403 „Zugriff geblockt" abgewiesen wurde — der Token war gültig, nur der Transport wurde geblockt. Seit 0.9.23 führt das Plugin den Refresh-Grant deshalb **innerhalb der Reader-Seite aus** (in-page `fetch` über das Chrome DevTools Protocol): gleicher TLS-Fingerprint, gleiche Header-Familie wie der Web Reader selbst — der Bot-Schutz akzeptiert genau diese Anfragen, der Reader stellt sie ja ständig selbst. Nur wenn kein Anmeldefenster offen ist oder der Browser-Tausch fehlschlägt, greift der alte Plugin-POST als Fallback. Die Token-Antwort-Verarbeitung (Rotation, sofortiges Speichern) wurde dazu aus `_login` in `_apply_token_response` ausgelagert und wird von beiden Wegen geteilt.

**Netzwerk-Interception als letzter Ausweg (0.9.22):** Feldbefund: Der gelesene Storage-Token wurde am Token-Endpunkt als „wiederverwendet/ungültig" abgelehnt — der Reader hatte den Token intern schon weiterrotiert, ohne die neue Kopie sofort in Storage zu schreiben. Seit 0.9.22 fängt das Plugin die **nächste Token-Rotation des Readers direkt aus dem Netzwerk-Verkehr ab** (CDP Fetch-Interception auf dem Reader-Tab): Sobald der Reader den Token-Endpunkt aufruft, wird die Antwort mitgelesen — dieser Token ist garantiert ungenutzt, denn der Reader verbraucht ihn ja selbst. Der Live-Grab probiert jetzt der Reihe nach: (1) frischer Storage-Token, (2) falls leer/veraltet: bis zu 3 Minuten Lauschen auf die nächste Rotation (Einmal-Lesen im Extrahieren-Knopf: 60 Sekunden), (3) erst danach Fehler mit Anleitung.

**IndexedDB-Grab & GUI-Freeze behoben (0.9.21):** Zwei Befunde aus dem Feld: (1) Der Web Reader legt sein aktuelles Token-Set häufig in **IndexedDB** ab statt im localStorage — der Live-Grab las bisher nur localStorage/sessionStorage und meldete deshalb bei angemeldetem Fenster „kein aktueller Refresh-Token gefunden". Der Grab liest jetzt alle IndexedDB-Datenbanken der Seite mit (per `awaitPromise` über das Chrome DevTools Protocol) und führt localStorage-, sessionStorage- und IndexedDB-Treffer zusammen. (2) Der Extrahieren-Knopf pollte auf dem Calibre-GUI-Thread bis zu 60 Sekunden — Calibre fror ein, der eingefrorene Prozess hinterließ einen toten Socket, und der nächste Start scheiterte mit „Failed to contact running instance of calibre". Der Knopf macht jetzt genau **einen** Leseversuch (Sekunden statt Minuten); nach einer Anmeldung im Fenster genügt ein weiterer Klick. Zusätzlich: Werden mehrere JWT-Kandidaten gelesen, werden Access-Tokens (`typ` ≠ `Refresh`) zugunsten des echten Refresh-Tokens hintangestellt, und die Fehlermeldung enthält eine redigierte Seiten-Zusammenfassung (Anzahl Kandidaten, IndexedDB-Datenbanknamen — nie Token-Werte).

**Extrahieren-Knopf nutzt das offene Anmeldefenster (0.9.20):** Der Knopf „Token aus Browser extrahieren“ versuchte bisher immer zuerst die Festplatten-Kopien und lief dabei in die Meldung „18 Refresh-Token gefunden, aber alle waren bereits verbraucht“ — selbst dann, wenn das Anmeldefenster von „Im Browser anmelden“ noch offen und angemeldet war (der Reader-Tab war auf DevTools-Port 9223 sichtbar). Der Knopf liest jetzt zuerst den aktuellen Token live aus einem laufenden Anmeldefenster und prüft die Festplatten-Kopien nur noch, wenn keines läuft. Läuft ein Fenster, aber es liefert keinen Token (z. B. noch nicht angemeldet), kommt eine klare Anleitung statt eines stillen Disk-Scrapes, der die offene Sitzung durch Replays gefährden würde.

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
