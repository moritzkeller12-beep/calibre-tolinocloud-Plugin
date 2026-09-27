# Browser-Anmeldung: Diagnose-Historie und Lösungsarchitektur

Zusammenfassung der Feldbefunde (Mai–September 2026), die zu den
Versionen 0.9.16–0.9.28 geführt haben. Keine Token-Werte — nur
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
11. **Erzwungene Rotation und Netzwerk-Interception entfernt
    (0.9.28):** Die Fangmechanismen der 0.9.22–0.9.26 (Access-Token-
    Invalidierung plus `Page.reload`, `Fetch`-Interception der
    Token-Antworten, `hardware-id`-Mitlesen aus Network-Events,
    Notfall-Clear des Storage) kosteten mehrere hundert Zeilen CDP-
    Code und waren die Quelle zweier Feldbefunde: „Das Anmeldefenster
    wurde geschlossen" (obwohl offen) und der Hänger „erzwinge eine
    frische Token-Rotation im Anmeldefenster ...". Der Web Reader
    rotiert ohnehin selbst etwa alle 40–60 s — das Plugin liest
    deshalb nur noch, was der Reader schreibt: nach einer Ablehnung
    wird der Seiten-Speicher (localStorage, sessionStorage, IndexedDB)
    bis zu ~90 s alle 3 s erneut gelesen und die erste neu
    geschriebene Kopie getauscht. Kein erzwungener Reload, kein
    Abfangen von Antworten, kein Mithören von Headern.
12. **Anmeldefenster wird nach dem Lauf geschlossen (0.9.30):** Ein
    offen gebliebenes Fenster war die Quelle des Dauer-Loops „1 Refresh-
    Kandidat, invalid_grant, nichts nachgeschoben": Nach einem
    erfolgreichen Tausch rotiert der Reader im Hintergrund mit der
    soeben verbrauchten Kopie weiter (Reuse-Schutz widerruft die ganze
    Sitzung), und ein Fehlschlag hinterließ dasselbe tote Profil, das
    der nächste Versuch wieder benutzte. Jetzt wird das Fenster
    geschlossen — nach Erfolg (die Sitzung gehört ab jetzt Calibre)
    und nach endgültiger Ablehnung (dort lag nur noch ein verbrauchter
    Token). Der nächste Start arbeitet mit frischem Profil und zeigt
    eine echte Anmeldeseite. Timeouts (Anmeldung läuft noch) lassen
    das Fenster bewusst offen.
13. **Wartephase unterscheidet den Fensterzustand (0.9.32):** Der
    Feldbefund „kein Web-Reader-Tab im Anmeldefenster offen“ nach
    `invalid_grant` vermischte zwei Fälle: Fenster weg (DevTools-
    Endpunkt antwortet nicht) und Fenster offen, aber der Tab liegt auf
    der Anmeldeseite des Buchhändlers (die Sitzung ist tot). Beim
    zweiten schloss das Plugin das Fenster und verlangte einen
    vollständigen Neustart, obwohl der Nutzer genau dort weitermachen
    konnte. Jetzt: `grabber_window_state()` (Endpunkt plus Tab-Klasse),
    Abbruch der Wartezeit nach drei Endpunkt-Fehlschlägen, bei offenem
    Fenster ohne Reader-Tab verlängerte Wartezeit (+30 Versuche) mit
    Aufforderung zur erneuten Anmeldung **in diesem Fenster** (bleibt
    offen, `launch_reader_window` verwendet es wieder), und die
    Altersnotiz beschreibt undatierte Kandidaten strukturell
    (Teile-Anzahl, Länge, Payload-Lesbarkeit) — „ohne datierbares
    JWT-alter“ unterscheidet damit opake Werte von JWE bzw. falsch
    extrahierten Zeichenketten.
14. **Gekodeter Kandidat wird entpackt (0.9.33):** Feldbefund der
    Extrahier-Schaltfläche: genau ein Refresh-Kandidat, „1 Teil, 920
    Zeichen“, am Endpunkt abgelehnt (HTTP 400, `invalid_grant: Invalid
    refresh token`) und ohne datierbares JWT-alter. Ein JWT trägt immer
    zwei Punkte — der Wert lag also gekodiert im Speicher (base64/
    base64url; ~690 Bytes JWT ergeben base64 genau 920 Zeichen). Die
    Hülle kann am Endpunkt nie funktionieren: Keycloak kann sie nicht
    parsen, und undatierte Kandidaten passieren den 30-Minuten-Filter
    absichtlich — eine gekoderte Kopie wurde damit geprüft und
    verbrannte den Lauf. Quelle des Werts ist der rohe Push im
    IndexedDB-Snippet (unter refresh-achtigen Schlüsseln, sobald der
    Wert kein JSON-Objekt ist). Jetzt: `_normalize_live_grab` zieht die
    Hülle bereits beim Eintritt des Grabs ab — nur wenn das Ergebnis
    eine dreiteilige JWT-Form mit lesbarem Payload ist (opake Tokens
    bleiben unverändert); danach Altersfilter, Tausch-Reihenfolge,
    `tried`-Buchung und Altersnotiz dieselbe austauschbare
    Repräsentation.
15. **Verschlüsselte Kandidaten werden entschlüsselt (0.9.34):** Das
    curl-Export-Feldbild (zum 0.9.33-Bericht, erneut „1 Teil, 920
    Zeichen“) zeigte die wahre Hülle: kein base64(JWT), sondern
    `base64("Salted__" + Salt + AES-256-CBC)` — CryptoJS.AES aus
    `VERSION.PHRASE` (Leer-Passphrase), exakt das Format, das die
    Platten-Version seit jeher entschlüsselt. Der Live-Grab pushte den
    Ciphertext dagegen roh: undatiert, am Endpunkt mit invalid_grant
    abgelehnt, und `userInfos` (die Hardware-Id) kam gar nicht erst in
    den Eimer („0 Hardware-Kandidat(en)“) — obwohl derselbe Export
    beide Werte im Klartext zeigt (Token im eigenen Refresh-Grant,
    Hardware-Id als `hardware-id`-Header). Fix: die Snippets sammeln
    `userToken`/`userInfos`-Blobs, `_normalize_live_grab`
    entschlüsselt (`_decrypt_reader_blob`, `_hardware_from_reader_blob`)
    und zieht den JWT-Kern bzw. die `hardwareId`; undurchdringbarer
    Ciphertext wird stillschweigend verworfen, statt getauscht oder
    gespeichert zu werden. Die Vermutung aus 0.9.33 (base64 eines
    ~690-Byte-JWT) ist damit widerlegt; der strukturelle Peel bleibt
    für echte Hüllen erhalten.    Verifikation mit dem Feldwert selbst:
    Entschlüsselung ergab `typ: Refresh`, gleiche `sid` wie der
    erfolgreiche Grant des Readers, Alter datierbar.
16. **BOSH-400 „{}“ heilt über Geräte-Recovery (0.9.35):** Nach der
    ersten erfolgreichen Browser-Anmeldung (0.9.34) starb die
    Vorbereitung mit „Tolino HTTP 400: {}“ bei `inventory/delta`.
    Der BOSH-Dienst prüft die `hardware_id`-Header gegen seine
    Geräteliste und antwortet auf unbekannte IDs mit leerem `{}` —
    die eigentliche Servermeldung (`ResponseInfo.message`) ging durch
    den Fehlerfilter verloren, der nur error/error_description
    zuließ. Die Referenz-Clients (tolino-python, pytolino)
    registrieren vor jedem BOSH-Aufruf (`registerhw`) bzw. adoptieren
    das zuletzt genutzte Gerät der eigenen Liste
    (`handshake/devices/list`); das Plugin tat nie eines von beidem —
    die pytolino-Ableitung `fetch_hardware_id` war tot angelötet.
    Fix: bei einem authentifizierten 400 (ohne Token-Endpunkt, genau
    ein Versuch) übernimmt `_recover_device_registration` das
    registrierte Gerät (gemeldet über `hardware_callback`, damit die
    UI es speichert) oder registriert die eigene ID (`registerhw`,
    `v2` dann klassisch, `hardware_type: HTML5`), danach ein Retry;
    ist das konfigurierte Gerät bereits das registrierte, bleibt der
    echte Fehler sichtbar. Zusätzlich zeigt der Fehlerfilter jetzt
    auch `message`/`ResponseInfo.message` (redigiert), nicht mehr
    nur `{}`. Nachtrag 0.9.37: `registerhw` braucht einen Moment, bis
    der BOSH-Dienst die ID akzeptiert — der direkte Retry der ersten
    Diagnose schlug deshalb noch fehl (der nächste Klick lief). Der
    erste Retry wartet jetzt 1,5 s, dazu gibt es genau einen zweiten
    Versuch; der Diagnose-Button baut seinen Client zusätzlich über
    `_cloud_client()` (gleiche Callbacks wie der Sync-Lauf).

17. **Erste 403 nach der Browser-Anmeldung (0.9.38):** Feldbefund nach
    0.9.37: „Diagnose – Tolino-Antwort testen" warf direkt nach der
    Browser-Anmeldung `Tolino authentication failed: … (403): Zugriff
    geblockt layout-fehlerseite …` (System-curl am Token-Endpunkt, erster Transport im Prozess), während
    der Sync-Lauf Sekunden später lief. Die Bot-Schutz-Laufzeitbahn
    lehnt den ersten Server-Grant nach dem Browser-Grant kurzfristig
    ab. Ursache: `_impersonate_session()` verwarf die von
    `import_from_plugin_dir()` gelieferte Session-Fabrik und rief den
    ungebundenen Namen `Session` auf (NameError → None) — der ERSTE
    Token-Aufruf im Prozess landete damit immer beim systemweiten,
    WAF-geblockten System-curl, dessen 403 sofort abbrach, bevor
    urllib mit den Browser-Headern drankam; der Bootstrap-Seiten-
    effekt heilte erst die späteren Läufe (Diagnose rot, Sync grün).
    Fix: die Fabrik wird jetzt verwendet, 403-Bot-Seiten fallen bei
    Token-Aufrufen auf den nächsten Transport durch (OAuth-Fehler wie
    invalid_grant brechen weiterhin sofort ab — kein Grant-Replay),
    erst danach greifen zwei Warteversuche (2 s, 5 s), bevor der
    Fehler mit curl_cffi-Hinweis erscheint. Daneben
    Benutzerführung im Bestandsvergleich: Zeilen sortiert (Upload-
    Auswahl, Titel, Autoren), neue Spalten „In Calibre"/"In Cloud",
    Tooltips zu den Cloud-Aktionen.

18. **Alle Shops laufen über den gemeinsamen Web Reader (0.9.39):**
    Feldbefund nach der Orell-Füssli-Fix-Serie (0.9.34–0.9.38): die
    anderen Buchhandlungen „müssten sich gleich verhalten — die nutzen
    ja alle webreader.mytolino.com“. Belegt über die OAuth-Konfiguration
    jedes Resellers (`v2/resellerconfig`, client `TOLINO_WEBREADER`):
    **jeder** Reseller — thalia.de/at, hugendubel.de, ebook.de,
    buecher.de, osiander.de, orell fuessli, sogar das eingestellte
    buch.de — trägt als `URL_OAUTH_REDIRECT`
    `https://webreader.mytolino.com/library/`, nie localhost. Der
    localhost-Code-Flow konnte bei ihnen also nie ankommen; nur Orell
    Füssli lief bislang über den geführten Weg (`LOCAL_CALLBACK_
    UNSUPPORTED_RESELLERS`). Fix: ein gemeinsames Profil
    (`uses_shared_webreader()`), das für alle Shops mit diesem Reader
    denselben Weg öffnet — Chromium-Fenster auf dem Reader, Live-Grab,
    genau ein Tausch — und am Token-POST dieselben Origin/Referer-
    Kopfzeilen sowie `client_type`/`client_version`
    (`TOLINO_WEBREADER`/`5.2.0`) setzt wie Orell Füssli. Endpunkte,
    Client-IDs und Scopes wurden an derselben Quelle ausgerichtet
    (u. a. Hugendubel `www.hugendubel.de/oauth/token`, buecher.de
    `www.buecher.de/auth/oauth2/*` mit `webreader`/`SCOPE_BOSH`, Thalia.at
    `www.thalia.at/auth/oauth2/*`); Hugendubels alter
    `webreader.hugendubel.de` leitet 301 auf den gemeinsamen Reader.
    **eBook.de** fehlte als Partner komplett und ist jetzt Reseller 81
    neu (Client `ebookde0501html5readerV0001`, Scope `e-publishing`,
    `www.ebook.de/oauth/{authorize,token}`); das tote Buch.de-Eintrag
    behält ohne Endpunkte den klaren „keine OAuth-Anmeldeseite“-Fehler.
    Daneben zwei Konten-Fixes in derselben Runde: der Dialog wechselt
    das aktive Konto sofort (State/Keep-alive lesen immer das
    angezeigte Konto, Keep-alive-Persistenz war tot — `settings()`
    kennt kein `account_name`) und die Tolino-ID-Spalte wird nach
    EINER Regel gelesen **und** geschrieben (nur bei genau einem Konto).

19. **Flatpak-Calibre öffnet keinen Browser (0.9.40):** Feldbefund
    Pop!_OS, Calibre über Flathub: weder das eigene Anmeldefenster
    noch der System-Browser öffnen sich. Ursache:
    `com.calibre_ebook.calibre` läuft in einer Sandbox OHNE
    `--talk-name=org.freedesktop.Flatpak` — dort findet
    `pick_chromium()` nichts (Host-Browser liegen nicht im
    Sandbox-PATH, `flatpak`/`snap` fehlen), und das `webbrowser`-Modul
    scheitert zweifach: ohne Browser in PATH wirft es „could not
    locate runnable browser“, sonst startet es `xdg-open` asynchron
    und verrät dessen Fehlschlag nie — beide Fälle endeten vor
    0.9.40 im Warten ohne Fenster. Fix in drei Schichten: (a)
    `open_system_browser()` startet in der Sandbox SYNCHRON die
    Öffnerkette xdg-open (Portal-Wrapper) → gio/gnome-open/kde-open →
    `flatpak-spawn --host xdg-open` mit Exit-Code-Prüfung und wertet
    Modul-Exceptions als Fehlschlag; (b) `chromium_candidates()`
    plant per `flatpak-spawn --host`-Sonde die Browser des HOSTS
    (native Pfade plus Host-Flatpak-Apps) und startet das
    Anmeldefenster damit außerhalb derselben Netzkennzeichnung
    (Port 9223 bleibt erreichbar); (c) beide Fehlermeldungen nennen
    bei erkannter Sandbox den Befehl
    `flatpak override --user --talk-name=org.freedesktop.Flatpak
    com.calibre_ebook.calibre` und den manuellen Token-Weg.

## Aktuelle Login-Kette (ab 0.9.35)

1. Eigenes Chromium-Fenster (privates Profil, DevTools-Port, Flatpak-fähig)
   öffnet den Web Reader; während der Anmeldung zählt das Fenster als
   offen, solange der DevTools-Endpunkt antwortet (Partner-Keycloak-
   Seite statt `mytolino.com` ist kein Abbruch).
2. Storage-Grab (localStorage + sessionStorage **inkl. JSON-Blobs** +
   IndexedDB) → frischester `typ: Refresh`-Kandidat; in der Ablage
   gekodet liegende Kandidaten (base64/JSON/URL) werden vor dem Tausch
   entpackt (0.9.33), CryptoJS-verschlüsselte userToken/userInfos-Blobs
   entschlüsselt — Klartext-JWT plus Hardware-Id (0.9.34).
3. Tausch **in der Reader-Seite** (in-page fetch) → `_apply_token_response`
   mit sofortigem Speichern des rotierten Tokens; danach wird das
   Anmeldefenster geschlossen (die Sitzung gehört ab jetzt Calibre).
4. Bei Ablehnung/Leere: Seiten-Speicher (inkl. IndexedDB) alle 3 s
   erneut lesen, bis der Reader selbst einen frischen Token schreibt
   (bis zu ~90 s; zeigt das Fenster die Anmeldeseite des Buchhändlers,
   bis zu ~180 s mit Aufforderung zur Neu-Anmeldung vor Ort), dann
   genau einmal tauschen. Kommt nichts Nachgeschobenes: Fenster weg →
   schließen und Fehlermeldung „Browser-Anmeldung erneut starten, im
   neuen Fenster anmelden“; Fenster offen ohne Reader-Tab → offen
   lassen, Neu-Anmeldung in diesem Fenster anstoßen (wird beim
   nächsten Knopfdruck wiederverwendet). Die Meldung nennt
   zusätzlich das ALTER des geprüften Kandidaten (JWT-iat, nie der
   Wert): Sekunden = frische Anmeldung sofort abgelehnt, Stunden =
   verbrauchte Kopie aus wiederverwendetem Fenster; undatierte
   Kandidaten werden strukturell beschrieben (Teile, Zeichen,
   Payload-Lesbarkeit). Erzwungene Rotation und Netzwerk-
   Interception gibt es seit 0.9.28 nicht mehr.
5. Ohne Chromium: Disk-Scrape-Fallback (alle Browser schließen, damit der
   letzte Token geflushed wird).
