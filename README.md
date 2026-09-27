# Tolino Cloud Sync für Calibre

Plugin-Version: **0.9.44** — synchronisiert Bücher zwischen Calibre und der Tolino Cloud
(Upload, Download, Sammlungen, Gelesen-Markierung).

## Läuft auf

- **Windows 11** — getestet
- **Linux mit nativ installiertem Calibre** — getestet
- **Linux mit Calibre als Flatpak (Flathub)** — getestet;
  die Browser des Rechners brauchen dort eine einmalige Freigabe
  (siehe „Häufige Fehlermeldungen“)

## Installation

```text
python3 build_plugin.py
```

`tolino_cloud_sync.zip` in Calibre laden: **Einstellungen > Plugins > Plugin aus Datei
laden**, dann Calibre neu starten.

## Einrichtung

Dialog **Tolino Cloud Sync**: Buchhändler, Hardware-ID und Refresh-Token prüfen,
Formate festlegen → **Vergleichen und hochladen** (Auswahl nach oben sortiert,
Spaltenköpfe klicken sortiert um, **In Calibre**/**In Cloud** zeigt die Lage jedes
Buchs, Tooltips erklären die Cloud-Aktionen).

## Refresh-Token beschaffen

**Empfohlen: „Im Browser anmelden (frischen Token holen)“** — das eigene
Chromium-Fenster öffnet den Web Reader, dort bis zur **Bücherliste** anmelden;
das Plugin liest den Token live und schließt das Fenster danach selbst.

- **Ohne Chromium:** Knopf drücken → Web Reader im System-Browser öffnen → **alle
  Browserfenster schließen** → Knopf erneut drücken. (Flatpak: zuerst die Freigabe
  aus der Tabelle unten erteilen.)
- **Manuell:** F12 → Netzwerk → Aufnahme an → im Web Reader anmelden →
  `hardware_id` (Filter `registerhw`) und `refresh_token` (Filter `token`) kopieren.

**Wichtig:** Tolino-Tokens sind nach einmaliger Nutzung ungültig (`invalid_grant`);
kein Inkognito-Fenster.

### Häufige Fehlermeldungen

| Meldung | Tun |
|---|---|
| „warte auf eine frische Rotation …“ / „Fenster ist noch offen“ | Fenster zeigt die Anmeldeseite: **dort** neu anmelden (Bücherliste laden), Knopf erneut — das Fenster bleibt offen und wird wiederverwendet. „Fenster wurde geschlossen“: Browser-Anmeldung neu starten und im **neuen** Fenster anmelden. |
| `invalid_grant` / „alle verbraucht“ | Live-Weg benutzen; Festplatten-Kopien nur mit geschlossenen Browserfenstern lesen (gekoderte/verschlüsselte Werte entpackt das Plugin automatisch). |
| HTTP 403 „Zugriff geblockt“ | Bot-Schutz: das Plugin wartet und wiederholt den Aufruf automatisch (zwei Warteversuche mit Transportwechsel); bleibt die 403, später erneut versuchen. |
| „Tolino HTTP 400: {}“ / „Vorbereitung fehlgeschlagen“ | Unbekannte Hardware-ID: das Gerät wird automatisch übernommen bzw. registriert und der Aufruf wiederholt. |
| Flatpak: kein Browser / „Freigabe erteilen“ | Einmalig erlauben, danach Calibre **vollständig** neu starten: User-Installation `flatpak override --user --talk-name=org.freedesktop.Flatpak com.calibre_ebook.calibre`, System-Installation dasselbe mit `sudo` ohne `--user`. Wirksam? `flatpak info --show-permissions com.calibre_ebook.calibre` muss `org.freedesktop.Flatpak` zeigen. |
| Es öffnet sich nur Firefox / das eigene Fenster bleibt zu | Eine installierte Chromium-Variante (Chrome/Chromium/Brave/Edge) wird immer zuerst genutzt, egal welcher Standardbrowser eingestellt ist — ohne eine davon bitte eine installieren. |

## Konten

Jedes Konto speichert seine Tolino-IDs selbst (Kontowechsel wirkt sofort); die
optionale **Tolino-ID-Spalte** wird nur bei **genau einem Konto** gelesen und
geschrieben. Alle Buchhandlungen (Thalia, Hugendubel, eBook.de, buecher.de,
Osiander, Orell Füssli) laufen über den gemeinsamen Web Reader auf
`webreader.mytolino.com`. Feldbefunde: `docs/browser-login-diagnose.md`.

## Technik (Kurzfassung)

- **Live-Grab statt Replay:** Keycloak akzeptiert pro Sitzung nur den neuesten
  Refresh-Token; das Plugin liest ihn per CDP aus der laufenden Reader-Seite und
  tauscht genau einmal (in-page `fetch`, derselbe TLS-Fingerprint wie der Reader).
- **Kandidaten automatisch aufbereiten:** gekoderte und CryptoJS-verschlüsselte
  Werte werden vor dem Tausch entpackt bzw. entschlüsselt.
- **Token-Keep-alive:** rotiert alle 45 Minuten, solange Calibre läuft.
- **403-Bot-Schutz:** Transportkette curl → urllib, bei 403 automatischer
  Wechsel; OAuth-Antworten brechen sofort ab (kein Grant-Replay).

## Validierung

```text
python3 -m unittest calibre_plugin.test_sync
python3 build_plugin.py
```
