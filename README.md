# Tolino Cloud Sync für Calibre

Plugin-Version: **0.9.36** — synchronisiert Bücher zwischen Calibre und der Tolino Cloud
(Upload, Download, Sammlungen, Gelesen-Markierung).

## Installation

```text
python3 build_plugin.py
```

`tolino_cloud_sync.zip` in Calibre laden: **Einstellungen > Plugins > Plugin aus Datei
laden**, dann Calibre neu starten.

## Einrichtung

Dialog **Tolino Cloud Sync**: Buchhändler und Hardware-ID prüfen, Refresh-Token
eintragen, Formate festlegen → **3. Synchronisierung starten**.

## Refresh-Token beschaffen

**Empfohlen: „Im Browser anmelden (frischen Token holen)".**

1. Das eigene Chromium-Fenster öffnet den Web Reader — dort **anmelden**, bis die
   **Bücherliste** lädt,
2. Fenster offen lassen — das Plugin liest den Token live und speichert ihn,
3. bei „Token gespeichert" schließt sich das Fenster automatisch.

Alternativen:

- **„Token aus laufendem Web Reader übernehmen"** liest den Token aus einem schon
  offenen Reader-Fenster (sonst die Festplatten-Kopien).
- **Ohne Chromium:** Knopf drücken → Web Reader im Systems-Browser öffnen →
  **alle Browserfenster schließen** → Knopf erneut drücken.
- **Manuell:** F12 → Netzwerk → Aufnahme an → im Web Reader anmelden →
  `hardware_id` (Filter `registerhw`) und `refresh_token` (Filter `token`) kopieren.

**Wichtig:** Tolino-Tokens sind nach einmaliger Nutzung ungültig (`invalid_grant`).
Kein Inkognito-Fenster.

### Häufige Fehlermeldungen

| Meldung | Tun |
|---|---|
| „… warte auf eine frische Rotation des Web Readers" | Token verbraucht; das Plugin wartet bis zu ~90 s auf eine neu geschriebene Kopie. Fenster offen lassen. |
| „… weder frischer Token … noch nachgeschoben", **„Fenster ist noch offen"** | Das Fenster zeigt die Anmeldeseite: **dort** neu anmelden (Bücherliste laden) und den Knopf erneut drücken — das Fenster bleibt offen und wird wiederverwendet. |
| dieselbe Meldung, „Fenster wurde geschlossen" | Verbrauchter Token: Browser-Anmeldung erneut starten und im **neuen** Fenster anmelden, bis die Bücherliste lädt. |
| `invalid_grant` / „verbraucht oder widerrufen" | Alter Kandidat — empfohlenen Live-Weg benutzen; Disk-Kopien nur mit geschlossenen Browserfenstern lesen. Gekoderte und verschlüsselte Kandidaten entpackt das Plugin automatisch vor dem Tausch. |
| HTTP 403 „Zugriff geblockt" | Bot-Schutz → unten „Bot-Schutz-Komponente installieren". |
| „Tolino HTTP 400: {}" / „Vorbereitung fehlgeschlagen" | Unbekannte Hardware-ID am BOSH-Dienst: ab 0.9.35 übernimmt bzw. registriert das Plugin das Gerät automatisch, wiederholt den Aufruf einmal und zeigt die echte Servermeldung. |

Feldbefunde und Architektur-Historie: `docs/browser-login-diagnose.md`.

## Technik (Kurzfassung)

- **Live-Grab statt Replay:** Keycloak akzeptiert pro Sitzung nur den **neuesten**
  Refresh-Token. Das Plugin liest ihn direkt aus dem Speicher der laufenden
  Reader-Seite (CDP) und tauscht genau einmal — als in-page `fetch` mit demselben
  TLS-Fingerprint wie der Web Reader, dadurch geht es am Bot-Schutz vorbei.
- **Kandidaten automatisch aufbereiten:** gekoderte Werte (base64/JSON/Prozent) und
  CryptoJS-verschlüsselte Blöcke (userToken/userInfos) werden vor dem Tausch
  entpackt bzw. entschlüsselt.
- **Token-Keep-alive:** Refresh-Tokens verfallen nach ~1 Stunde Untätigkeit; solange
  Calibre läuft, rotiert das Plugin alle 45 Minuten still weiter.

## 403 und Bot-Schutz

Der Token-Endpunkt prüft TLS-Fingerprints. Transportreihenfolge: **curl_cffi**
(Chrome-TLS, bei aktivem Bot-Schutz erforderlich) → **curl** → **urllib**. System-curl
bekommt HTTP 403, curl_cffi passiert die WAF.

**Ein-Klick:** Button **„Bot-Schutz-Komponente installieren (einmalig)"** lädt die
curl_cffi-Wheels von PyPI, prüft SHA-256 und entpackt sie in den
Calibre-Plugin-Ordner — ohne pip, ohne Neustart.

## Validierung

```text
python3 -m unittest calibre_plugin.test_sync
python3 build_plugin.py
```

## Weitere Funktionen

- **Hardware-ID automatisch auflösen** aus der Tolino-Geräteliste
- **Buch herunterladen**, **Sammlungen** und **Gelesen-Markierung** in der
  Bestandsvergleich-Liste
