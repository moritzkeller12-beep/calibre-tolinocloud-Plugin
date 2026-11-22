# tolino Cloud Sync für Calibre

Synchronisiert Bücher, Metadaten, Lesestatus und Fortschritt zwischen Calibre und der tolino Cloud.

Aktuelle Version: **0.1.09** (Quelle: [`__init__.py`](__init__.py)).

0.1.09 schliesst das Dashboard per Esc wie das Fensterkreuz (laufende Worker werden abgebrochen, sichtbare Einstellungen noch gesichert; schlägt das Sichern fehl, bleibt das Fenster offen); der übrige Code ist ohne Verhaltensänderung gestrafft. Config-Speicher, Konten und Tokens bleiben unverändert.

## Installation

1. [Plugin-ZIP für 0.1.09 herunterladen](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/raw/0.1.09/tolino_cloud_sync.zip).
2. In Calibre: **Einstellungen → Plugins → Plugin aus Datei laden** und ZIP auswählen.
3. Calibre neu starten.

Voraussetzung: Calibre 6 oder neuer. Die automatische Anmeldung benötigt Chrome, Chromium, Brave oder Edge; verschlüsselte Web-Reader-Tokens zusätzlich PyCryptodome.

## Benutzung

Plugin öffnen und **Bei tolino anmelden** – den Buchhändler erkennt das
Plugin am Zugang. Danach **Bibliothek vergleichen …** wählen, gewünschte Aktionen markieren und **Synchronisierung starten**.

Fehlermeldungen enthalten einen Code; die Abhilfe steht in der [Fehlercodeliste](docs/fehlercodes.md).

## Anmeldung haltbar halten

Solange Calibre läuft, erneuert das Plugin den Zugang nach der Laufzeit des
gespeicherten Refresh-Tokens (ein Fünftel Vorlauf, spätestens alle 45, mindestens
alle 5 Minuten) und beginnt mit einer sofortigen Prüfung. Ein noch gültiger
Access-Token wird mitgespeichert, damit ein Start keinen unnötigen Tausch
auslöst; beim Erneuern gilt der Scope des ausgestellten Tokens, kein
hartkodierter Wert.

Wer Calibre überwiegend geschlossen hat, lässt den Zugang von einem
System-Timer erneuern. `tools/token_refresh.py` startet Calibres eigene Python,
lädt das installierte Plugin aus seinem ZIP und tauscht nur fällige Konten —
sonst passiert kein Netzaufruf. Der Takt darf deshalb kurz sein und sollte
unter der Idle-Grenze des Buchhändlers liegen:

```ini
# ~/.config/systemd/user/tolino-refresh.service
[Unit]
Description=tolino-Zugang erneuern

[Service]
Type=oneshot
ExecStart=%h/calibre-tolinocloud-plugin/tools/token_refresh.py

# ~/.config/systemd/user/tolino-refresh.timer
[Unit]
Description=tolino-Zugang regelmäßig erneuern

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
```

`systemctl --user enable --now tolino-refresh.timer`. Ohne Systemd genügt Cron
(`*/5 * * * * /pfad/zu/tools/token_refresh.py`), unter Windows die
Aufgabenplanung mit demselben Skript. Findet das Skript das Plugin-ZIP nicht,
nennt `--plugin` den Pfad, ein abweichendes Calibre-Verzeichnis `--config-dir`.

## Entwicklung

Tests: `python3 -X dev -m unittest calibre_plugin.test_sync` (mit Calibres
Python: `calibre-debug -c` mit demselben Modul).

Die Dialogtests laufen mit echtem PyQt6 im Offscreen-Modus
(`QT_QPA_PLATFORM=offscreen`); ohne PyQt6 werden sie übersprungen. Echte
Qt-Threads laufen nicht in der Suite: Widgets und Tabellen sind echt, der
QThread und die modalen Dialoge bleiben kontrollierte Doppel.
Testabhängigkeiten: `pycryptodome`, `PyQt6`.

Die Einstellungsdatei wird bei jedem Lese- und Schreibzugriff erneut geprüft:
ein abgeschlossener Schreibvorgang eines zweiten Calibre-Fensters wird
übernommen, halb geschriebene oder nicht lesbare Stände nie. Eine Sperrdatei
neben den Einstellungen serialisiert Schreibvorgänge zwischen Calibre-Prozessen.
Fehlt der Sperrmechanismus oder lässt sich die Sperrdatei nicht anlegen,
bleibt nur die prozesslokale Sperre. Schlägt der Erwerb einer verfügbaren
Dateisperre fehl, bricht der Schreibvorgang ab.

Die Suite ist bewusst klein und prüft Nutzerabläufe statt Interna:
Anmeldung und Konten, Vergleich und Planung, den tolino-Client gegen einen
lokalen HTTP-Server, den Sync-Worker, die Oberfläche mit echtem PyQt6 und den
CDP-Lebenszyklus ohne echten Browser. Eine feste Testzahl nennt sie nicht.
Einzelne interne Helfer (etwa `_Ws`, `_reader_target`, `_exchange_live_grab`)
werden direkt geprüft, wo nur so ein Fehlerfall erreichbar ist.

Live-Tests gegen die echte Cloud liegen nur lokal: `calibre_plugin/tests/live_tests.py`
wird nicht im Repo geführt und ist von Git ignoriert. Datei lokal anlegen,
`refresh_token` und `hardware_id` in der Umgebung setzen und `python3 -m
unittest calibre_plugin.tests.live_tests` ausführen. Ohne diese Werte werden
die Tests übersprungen. Der Refresh-Token ist einmalig – nach einem Live-Lauf ist er
verbraucht und muss neu hinterlegt werden; das Plugin selbst speichert den
rotierten Token beim Anmelden im Konto.

Release-ZIP: `python3 build_plugin.py`

Lizenz: [GPL-3.0-or-later](LICENSE). Änderungen: [CHANGELOG](CHANGELOG.md).
