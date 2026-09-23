# Browser-Anmeldung: Diagnose-Historie und Lösungsarchitektur

Zusammenfassung der Feldbefunde (Mai–September 2026), die zu den
Versionen 0.9.16–0.9.27 geführt haben. Keine Token-Werte — nur
Struktur und Mechanik.

## Kernproblem

Der Web Reader rotiert seinen Refresh-Token bei jedem Hintergrund-Refresh.
Keycloak akzeptiert pro Sitzung (`sid`) nur den **neuesten** Token; das
Wiederholen eines älteren löst den Wiederverwendungsschutz aus, der die
**gesamte Sitzung** widerruft. Historische Kopien (Festplatten-Storage,
Seiten-Speicher-Snapshots) sind damit zwangsläufig wertlos oder gefährlich.

## Diagnosekette (chronologisch)

1. **Session-Replay killt Sitzungen (0.9.16):** Sämtliche
   Geschwister-Tokens einer `sid` wurden nacheinander gegen den Token-
   Endpunkt replays — Reuse-Schutz widerrief die Live-Sitzung.
   Fix: pro `sid` nur der frischeste Kandidat, ein Endpunkt-POST pro
   Kandidat.
2. **Festplatten-Scrape ist immer zu spät (0.9.17–0.9.20):** Der Reader
   rotiert weiter, bevor der Storage geflushed wird. Ersetzt durch den
   Live-Grab über das Chrome DevTools Protocol in einem eigenen
   Chromium-Fenster (Flatpak-Unterstützung inklusive).
3. **WS-Parser-Bug (0.9.19):** Byte 1 des RFC-6455-Headers (Länge + Mask-
   Bit) wurde verworfen — jede CDP-Antwort zerfiel, der Grab fand
   „nichts“. Regressionstest mit echtem In-Process-WebSocket-Server.
4. **Altersfilter zu streng (0.9.19):** 2-Minuten-Cutoff warf den
   gültigen Live-Token weg (Keycloak-Refresh-Tokens leben ~1 Stunde,
   Anmeldungen dauern länger). Jetzt 30 Minuten.
5. **IndexedDB (0.9.21):** Der Reader legt sein Token-Set häufig in
   IndexedDB ab, nicht im localStorage — der Grab liest beides.
6. **WAF-403 (0.9.23):** Der Plugin-POST wurde vom Bot-Schutz geblockt
   („Zugriff geblockt“-Layoutseite), obwohl der Token gültig war — der
   Reader selbst tauschte denselben Token im Browser erfolgreich. Fix:
   Der Refresh-Grant wird **in der Reader-Seite** ausgeführt (in-page
   `fetch` per CDP) — gleicher TLS-Fingerprint, gleiche Header-Familie.
7. **Hardware-ID (0.9.24):** Die konfigurierte Geräte-ID gehörte zu einer
   älteren Anmeldung; die aktuelle Sitzung lief mit einer anderen ID
   (sichtbar im Netzwerk-Dump). Fix: `hardware-id`/`device-id`-Header der
   Reader-Requests werden passiv mitgelesen; die live erfasste ID gewinnt.
8. **Reload erzwingt keine Rotation (0.9.25):** Solange der Access-Token
   gültig ist (~50 Minuten), ruft der Reader den Token-Endpunkt nach einem
   Reload gar nicht erst auf. Fix: Vor dem Reload werden die
   **Access-Token**-Einträge im Seiten-Speicher per CDP ungültig
   geschrieben (Refresh-Tokens unberührt); die Seite startet mit totem
   Access-Token, 401-t beim nächsten API-Call und tauscht binnen Sekunden
   selbst. Die abgefangene Antwort ist garantiert ungenutzt und enthält
   einen frischen Access-Token.
9. **Fenster-Lebensprüfung falsch (0.9.27):** „Das Anmeldefenster wurde
   geschlossen" kam, solange der Tab nur auf der Keycloak-Anmeldeseite
   des Partners lag (andere Domain als `mytolino.com`) — der Nutzer
   hatte sich noch nicht einmal angemeldet; ein kurz nicht
   antwortendes `/json/list` sah identisch aus. Fix: „geschlossen"
   entscheidet nur noch das DevTools-Endpunkt (mehrere Fehlschläge in
   Folge); alles andere ist „Fenster offen, weiter warten" mit Hinweis
   in der Statuszeile.
10. **Blob-blindes Grab & defektes Polling (0.9.27):** Der Grab-Snippet
    parste JSON-Blobs (`oidc.user:…`) auf Speicher-Ebene nie (nur
    JWT-Form) — der aktuelle Token im Blob war für Initial-Grab und
    Polling unsichtbar. Und die Poll-Antworten kamen per
    `JSON.stringify` als **String** an, den die `isinstance`-(dict)-
    Prüfung still verwarf (nur die Test-Server antworteten mit dict):
    die erzwungene Rotation fing so nie etwas („kein frischer Token").
    Fix: Blob-Parsing top-level in beiden Snippets, String-Antworten
    werden geparst, der Poll liest die IndexedDB-Datenbanken mit, und
    abgefangene Token-Antworten laufen mit Original-Body an den Reader
    durch.

## Aktuelle Login-Kette (0.9.27)

1. Eigenes Chromium-Fenster (privates Profil, DevTools-Port, Flatpak-fähig)
   öffnet den Web Reader; während der Anmeldung zählt das Fenster als
   offen, solange der DevTools-Endpunkt antwortet (Partner-Keycloak-
   Seite statt `mytolino.com` ist kein Abbruch).
2. Storage-Grab (localStorage + sessionStorage **inkl. JSON-Blobs** +
   IndexedDB) → frischester `typ: Refresh`-Kandidat.
3. Tausch **in der Reader-Seite** (in-page fetch) → `_apply_token_response`
   mit sofortigem Speichern des rotierten Tokens.
4. Bei Ablehnung/Leere: Access-Tokens im Seiten-Speicher invalidieren,
   `Page.reload`, Token-Antwort per `Fetch`-Interception abfangen
   (Durchreichen mit Original-Body) **und** Seiten-Speicher inkl.
   IndexedDB alle 3 s pollen; `hardware-id`-Header mitlesen.
5. Ohne Chromium: Disk-Scrape-Fallback (alle Browser schließen, damit der
   letzte Token geflushed wird).
