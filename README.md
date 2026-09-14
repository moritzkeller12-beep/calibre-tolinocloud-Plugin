# Tolino Cloud Sync für Calibre

Plugin-Version: **0.7.0**. Autor: **moritzkeller12-beep**.

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
Plugin-Struktur. In Calibre **Einstellungen > Plugins > Plugin aus Datei
laden** auswählen und Calibre anschließend neu starten.

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
2. Partner **8 – Books.ch / Orell Füssli** auswählen.
3. Eine stabile Hardware-ID übernehmen oder selbst festlegen.
4. Den aktuellen `refresh_token` aus den Web-Reader-Netzwerkanfragen
   einfügen. Umgebende Leerzeichen und äußere Anführungszeichen werden
   entfernt.
5. Bevorzugte Formate festlegen, normalerweise EPUB und PDF. Cover-Upload
   und Löschungen sind optional.

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

## Bekannte Einschränkung

Die Tolino-API ist inoffiziell und kann sich ohne Vorankündigung ändern.
Das Plugin lädt Dateien und optional Cover hoch, bietet aber keinen
verifizierten Metadaten-Upload. Partner-, Token- und Dienständerungen können
eine erneute Einrichtung erfordern.

Das Plugin liest keine Browserprofile, Cookies oder LocalStorage-Daten und
extrahiert keine Hardware-IDs oder Tokens heimlich. Die Anmeldung bleibt ein
bewusst gestarteter Browser-/manueller Refresh-Token-Workflow; Tokenwerte
werden nicht protokolliert.

## Lokale Validierung

```text
python3 -m unittest calibre_plugin.test_sync
python3 -m py_compile calibre_plugin/*.py build_plugin.py
python3 build_plugin.py
```
