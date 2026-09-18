# Tolino Cloud Sync für Calibre

Plugin-Version: **0.9.1**

Das Plugin synchronisiert unterstützte Bücher aus einer geöffneten Calibre-
Bibliothek mit der Tolino Cloud. Vor dem Upload zeigt es einen Vergleich von
Calibre- und Tolino-Bestand. Titel, Autor und ISBN werden zum Abgleich
angezeigt; die technische `deliverableId` steht separat als Tolino-ID.

## Voraussetzungen

- Calibre **7.6 oder neuer**
- ein oder mehrere Tolino-Web-Reader-Refresh-Tokens
- Netzwerkzugriff auf die Tolino-Dienste

## Installation

Im Repository:

```text
python3 build_plugin.py
```

Die Datei `tolino_cloud_sync.zip` enthält die installierbare Calibre-
Plugin-Struktur. Nach einem Upgrade die alte Plugin-Version in Calibre
entfernen und `tolino_cloud_sync.zip` erneut laden, damit kein gecachtes
Archiv verwendet wird. In Calibre **Einstellungen > Plugins > Plugin aus
Datei laden** auswählen und Calibre anschließend neu starten.

## Einrichtung

Die optionale Text-Spalte **Tolino ID** mit Lookup-Namen **`#tolino_id`** kann
unter **Einstellungen > Eigene Spalten** angelegt werden. Sie ist im Plugin
standardmäßig aktiviert, wird aber nur verwendet, wenn sie tatsächlich
vorhanden ist; ohne diese Spalte synchronisiert das Plugin normal und legt
keine Spalte an. Nach erfolgreichen Uploads wird die jeweilige
`bosh_...`-`deliverableId` dort gespeichert; vorhandene Werte werden beim
Matching zuerst als Fallback verwendet. Die Vergleichstabelle zeigt die
technische ID separat. Die Option **Tolino-ID-Spalte verwenden, wenn vorhanden**
kann im Synchronisierungsdialog deaktiviert werden.

Im Dialog **Tolino Cloud Sync**:

1. Das aktive, benannte Konto auswählen (mit **Neues Konto** können weitere
   Konten angelegt werden). Pro Konto werden Partner, Hardware-ID und
   `refresh_token` getrennt gespeichert.
2. Partner auswählen (z.B. **8 – Books.ch / Orell Füssli**).
3. Eine stabile Hardware-ID übernehmen oder selbst festlegen.
4. Den aktuellen `refresh_token` einfügen – oder ihn über die eingebettete
   Browser-Anmeldung beziehen (siehe unten). Umgebende Leerzeichen und
   äußere Anführungszeichen werden entfernt.
   
   **Für Orell Füssli (Partner 8):**
   - Die externe Browser-Anmeldung mit Callback funktioniert nicht
     (Keycloak-Redirect)  - Verwenden Sie die **eingebettete Browser-Anmeldung** (Standard) oder
    **"Token aus Browser extrahieren"**
5. Bevorzugte Formate festlegen, normalerweise EPUB und PDF. Cover-Upload
   und Löschungen sind optional.

> **Hinweis (Pop!_OS/Ubuntu 24.04):** Erscheint beim Öffnen des
> Anmeldefensters `Sandbox: CanCreateUserNamespace() clone() failure:
> EPERM`, blockiert das System unprivilegierte User-Namespaces. Das Plugin
> umgeht das automatisch, indem es die WebEngine-Sandbox für den
> Anmelde-Dialog deaktiviert; an der normalen Browsernutzung ändert sich
> nichts.

Der Refresh-Token wird kontenbezogen von Calibre gespeichert und bei einer
Token-Rotation sofort aktualisiert. Auch der Synchronisationsstatus und die
Tolino-IDs werden pro Konto geführt; pro Synchronisierung ist genau ein Konto
aktiv. Alte Einzelkonto-Einstellungen werden beim ersten Öffnen automatisch in
ein Konto `default` migriert. Ein Access-Token ist kein Ersatz für einen
Refresh-Token. Tokens niemals in Tickets, Screenshots, Logs oder
Diagnoseausgaben veröffentlichen; alte offengelegte Tokens beim Partner
widerrufen oder ersetzen.

## Nutzung

**Synchronisierung starten** lädt den Tolino-Bestand und öffnet den
Vergleich. Das Matching erfolgt in dieser Reihenfolge:

1. gespeicherte Tolino-ID
2. Calibre-UUID
3. ISBN
4. normalisierter Titel

Verschachtelte Tolino-Antworten (`dict`, `list`, `edata`, `ebook`,
`epubMetaData` und `deliverable`) werden normalisiert. Ein Tolino-Eintrag
ohne sichtbaren Titel wird als **Nicht matchbar** markiert; seine technische
ID wird nie als Buchtitel verwendet. Eindeutige Titel-Treffer sind
standardmäßig nicht zum Upload ausgewählt. Neue oder ausdrücklich ausgewählte
Bücher werden im gewählten Format hochgeladen; ein optionales Cover kann
folgen. Löschungen bleiben eine separate, standardmäßig deaktivierte Option.

## Token-Extraktion aus dem Browser

Das Plugin kann Refresh-Tokens und Hardware-IDs automatisch aus Ihrem Browser
extrahieren, wenn Sie im Tolino Web Reader angemeldet sind:

1. Melden Sie sich im [Tolino Web Reader](https://webreader.mytolino.com) an
2. Schließen Sie den Browser **komplett**
3. Klicken Sie im Plugin auf **"Token aus Browser extrahieren"**
4. Die Tokens werden automatisch gefunden und gespeichert

**Unterstützte Browser:**
- Google Chrome / Chromium
- Microsoft Edge
- Brave
- Firefox
- Opera

**Unterstützte Speicherorte:**
- Local Storage
- Session Storage (für Keycloak, z.B. Orell Füssli)

**Unterstützte Origins:**
- https://webreader.mytolino.com
- https://www.orellfuessli.ch
- https://orellfuessli.ch
- https://bosh.pageplace.de
- Und alle anderen Tolino-Partner

## Browser-Anmeldung

**Eingebettete Anmeldung (Standard, alle Partner inkl. Orell Füssli):**

1. Klicken Sie auf **"Im Browser anmelden"**
2. Ein in Calibre eingebettetes Browser-Fenster öffnet die Anmeldeseite des
   Partners (OAuth/Keycloak)
3. Melden Sie sich an; nach dem Redirect in den Tolino Web Reader werden
   Refresh-Token und Hardware-ID automatisch aus dem Local Storage des
   Web Readers übernommen und gespeichert – auch für Keycloak-Partner wie
   Orell Füssli, bei denen der externe Callback-Flow scheitert

**Externe Anmeldung (Fallback):**

Falls Ihre Calibre-Installation keine eingebettete WebEngine mitbringt,
fällt das Plugin auf den bisherigen externen OAuth-Callback-Flow zurück.
Dieser funktioniert bei den meisten Partnern (Thalia, Hugendubel,
Bücher.de, etc.), aber nicht bei Orell Füssli (Partner 8), da Keycloak
nach dem Login direkt zum Web Reader weiterleitet, ohne zur Callback-URL
zurückzukehren. Verwenden Sie in dem Fall die eingebettete Anmeldung oder
**"Token aus Browser extrahieren"**.

## Bekannte Einschränkungen

- Die Tolino-API ist inoffiziell und kann sich ohne Vorankündigung ändern.
- Das Plugin lädt Dateien und optional Cover hoch, bietet aber keinen
  verifizierten Metadaten-Upload.
- Partner-, Token- und Dienständerungen können eine erneute Einrichtung erfordern.
- Ohne eingebettete WebEngine bleibt für Orell Füssli (Partner 8) nur die
  manuelle Token-Extraktion.

Das Plugin liest keine Browserprofile, Cookies oder LocalStorage-Daten ohne
explizite Nutzeraktion (Klick auf "Im Browser anmelden" oder "Token aus
Browser extrahieren"). Die Anmeldung bleibt ein bewusster
Browser-/manueller Refresh-Token-Workflow; Tokenwerte werden nicht
protokolliert.

## Lokale Validierung

```text
python3 -m unittest calibre_plugin.test_sync
python3 -m py_compile calibre_plugin/*.py build_plugin.py
python3 build_plugin.py
```

## Versionshistorie

### 0.9.1 (2026-09-18)
- **Toolbar-Icon repariert:** Das Plugin-Icon (eingebettetes SVG, Fallback
  aufs gebündelte PNG) wird jetzt beim Start explizit der Calibre-Aktion
  zugewiesen – vorher zeigte das Menüband nur den Text
- Der Button „Token aus Browser extrahieren" bleibt als Rückfallebene
  (z.B. wenn der Bot-Schutz die eingebettete Anmeldung blockiert)

## Versionshistorie

### 0.9.0 (2026-09-18)
- **Browser muss nicht mehr geschlossen werden:** Der Scanner liest
  LevelDB-/SQLite-Dateien roh ohne Sperren – das Live-Auslesen aus einem
  laufenden Browser funktioniert; alle Hinweistexte entsprechend angepasst
- **Präzisere Diagnose:** Zeigt bei Fehlschlag gefundene Tolino-Schlüssel
  (Werte geschwärzt) bzw. teilt mit, wenn Storage gelesen wurde, aber keine
  Tolino-Einträge darin liegen (→ Web Reader vollständig laden)
- Plugin-Version ist jetzt im Dialog-Titel sichtbar (erkennt veraltete
  Installationen sofort)

### 0.8.9 (2026-09-18)
### 0.8.9 (2026-09-18)
- **OAuth-Code-Abfangen (neuer Hauptweg):** Die eingebettete Anmeldung fängt
  den Autorisierungs-Code aus dem OAuth-Redirect ab und tauscht ihn direkt
  am Token-Endpoint des Partners ein – unabhängig davon, ob der Web Reader
  localStorage beschreibbar/lesbar macht. Scheitert der Austausch,
  läuft die Storage-Übernahme weiter
- **Firefox-Dekodierung repariert:** LSNG-Werte mit `\x01`-Präfix werden
  jetzt korrekt als UTF-16LE gelesen (vorher verkehrt herum – deshalb
  blieben die Tokens in Firefox-Profilen unsichtbar); `\x02` = UTF-8
- Qt-Warnung "Invalid ButtonRole" endgültig entfernt: keine
  QDialogButtonBox mehr, einfache Buttons mit Close-Fallback

### 0.8.8 (2026-09-18)
### 0.8.8 (2026-09-18)
- **"Keine Browser-Profile gefunden" behoben:** Die Speicherort-Erkennung
  läuft jetzt als durchsuchender Walk über alle Kandidatenverzeichnisse und
  findet Local-Storage-LevelDB-Ordner und Firefox-webappsstore.sqlite an
  beliebiger Tiefe – auch in Flatpak- (`~/.var/app/<App-ID>/config/…`) und
  Snap-Profilen (`~/snap/<name>/common/…`)
- Vollständige Diagnose: Bei Fehlschlag listet der Dialog jeden geprüften
  Pfad mit Status (vorhanden/fehlt) – so lässt sich sofort erkennen, welches
  Browserprofil übersehen wurde
- Qt-Warnungen entfernt: "Invalid ButtonRole, button not added" (Button
  wird jetzt nur noch mit garantiert gültiger Rolle hinzugefügt) und die
  Profil-Lebensdauer-Warnung beim Schließen (parentlose Page/Profil,
  deleteLater nach Dialogende)

### 0.8.7 (2026-09-18)
### 0.8.7 (2026-09-18)
- **Token-Extraktion komplett neu**: moderne Chromium-Browser (Chrome, Edge,
  Brave, Chromium, Vivaldi, Opera) speichern Local Storage als LevelDB;
  die bisherige Implementierung benötigte dafür das nie installierte
  Python-Modul `leveldb` und fand deshalb nichts ("Es wurden keine Tokens
  gefunden"). Jetzt: eigener, abhängigkeitsfreier LevelDB-Reader (Log- und
  Tabellendateien, Origin-/Schlüssel-Matching inkl. neuem
  `_https://origin\x00\x01key`-Schema)
- Firefox: moderner LSNG-Speicher (webappsstore.sqlite, BLOB-Werte mit
  utf8/utf16-Präfix) wird jetzt gelesen; alte `webappsstore2`-Abfragen
  entfernt
- Diagnose: bei Fehlschlag zeigt der Dialog durchsuchte Speicherorte an,
  detaillierter Hinweistext (Web Reader statt Shop, Browser vollständig
  beenden)
- Qt-Warnung "Release of profile requested…" endgültig behoben (Page vor
  Profil, ohne Parent, deleteLater nach Dialogende)
- "Sandbox: CanCreateUserNamespace() clone() failure: EPERM" unter
  Pop!_OS/Ubuntu 24.04: QtWebEngine-Sandbox wird für das Anmeldefenster
  deaktiviert (bekannte Systemrichtlinie gegen unprivilegierte
  User-Namespaces, kein Plugin-Fehler)
### 0.8.6 (2026-09-18)
- Konsistente Client Hints: Sec-CH-UA-Header und navigator.userAgentData
  stimmen jetzt mit dem bereinigten User-Agent überein (fehlende Hints waren
  der wahrscheinlichste Grund für den endlos neu ladenden DataDome-Check)
- Neuer Plan B im Anmeldefenster: **"Im Standardbrowser öffnen"** führt durch
  die Anmeldung im normalen Browser (dort akzeptiert der Bot-Schutz Sie) und
  übernimmt die Tokens nach dem Schließen automatisch aus dem Browser-Storage

### 0.8.5 (2026-09-18)
- DataDome-/Bot-Schutz-Härtung: das Anmeldeprofil persistiert Cookies
  (benanntes Profil), und ein Stealth-Skript bringt die eingebettete Engine
  auf konsistente Browser-Signale (navigator.webdriver=false, window.chrome,
  Sprachen, Plugins, WebGL-Renderer, Permissions-API), damit der
  "Verify human"-Check nicht endlos neu lädt

### 0.8.4 (2026-09-18)
- Bot-Schutz-Fix: Der User-Agent des eingebetteten Browsers enthält kein
  "QtWebEngine"-Token mehr (Chrome-ähnlicher UA), damit DataDome & Co. den
  Anmelde-Dialog nicht blockieren; Sicherheits-Checks lassen sich einmalig
  direkt im Fenster lösen, Hinweistext im Dialog ergänzt

### 0.8.3 (2026-09-18)
- Stille WebEngine-Konsole: harmlose Seiten-Warnungen (Permissions-Policy,
  OTS-Schriften, Gamepad, HEVC-Video) werden nicht mehr ausgegeben
- Sauberer Dialog-Abbau (WebEnginePage/Profil via deleteLater) – behebt die
  Qt-Warnung "Release of profile requested but WebEnginePage still not deleted"

### 0.8.2 (2026-09-18)
- Fix: Qt6/PyQt6-kompatible Qt-Enums in der eingebetteten Anmeldung
  (PersistentCookiesPolicy, Dialog-Button, runJavaScript) – behebt
  "AttributeError: type object 'QWebEngineProfile' has no attribute
  'ForcePersistentCookies'" unter Calibre 7.x

### 0.8.1 (2026-09-18)
- Eingebettete Browser-Anmeldung (QtWebEngine) für alle Partner, inklusive
  Orell Füssli/Keycloak: Tokens werden nach dem Login direkt aus dem
  Web-Reader-Storage übernommen
- Externer OAuth-Callback-Flow bleibt als Fallback erhalten

### 0.8.0 (2024-09-14)
- Fix für Calibre 7.6: Cover- und Format-Pfad-Abfrage mit expliziten Buch-IDs
- Erweiterte Token-Extraktion aus Browser (Local Storage + Session Storage)
- Unterstützung für Keycloak (Orell Füssli) hinzugefügt
- OAuth-Fix für alle Partner
- Neues Plugin-Icon für die Calibre-Menüleiste
- Token-Rotation wird jetzt korrekt gehandhabt (keine "reuse exceeded" Fehler)

### 0.7.2 (2024-08-XX)
- Initiale Version
