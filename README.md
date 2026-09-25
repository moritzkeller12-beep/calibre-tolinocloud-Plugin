# Tolino Cloud Sync für Calibre

Plugin-Version: **0.9.29** — synchronisiert Bücher aus Calibre mit der Tolino Cloud
(Upload, Download, Sammlungen, Gelesen-Markierung).

## Installation

```text
python3 build_plugin.py
```

`tolino_cloud_sync.zip` in Calibre laden: **Einstellungen > Plugins > Plugin aus Datei
laden**, dann Calibre neu starten.

## Einrichtung

Dialog **Tolino Cloud Sync**: Konto wählen, Partner (z. B. **8 – Books.ch / Orell
Füssli**) und Hardware-ID eintragen, Refresh-Token einfügen, bevorzugte Formate (EPUB +
PDF) festlegen → **Synchronisierung starten**.

## Refresh-Token beschaffen

**Empfohlen: Knopf „Im Browser anmelden (frischen Token holen)".** Er öffnet ein
eigenes Chromium-Fenster mit privatem Profil auf dem Web Reader des Partners:

1. dort **anmelden**, bis die **Bücherliste** lädt,
2. Fenster in Ruhe lassen — das Plugin liest den Token aus dem Seitenspeicher und
   tauscht ihn am Token-Endpunkt,
3. bei „Token gespeichert" darf das Fenster zu.

**Ohne Chromium** (kein Chrome/Chromium/Brave/Edge installiert) fällt derselbe Knopf
automatisch auf den Disk-Fallback zurück: Web Reader im Systems-Browser öffnen,
anmelden, **alle Browserfenster schließen** (nur dann schreibt Chromium den letzten
Token auf die Festplatte), Knopf erneut drücken.

**Ohne eigenes Anmeldefenster:** Knopf **„Frischen Token aus laufendem Web Reader
übernehmen"** liest genau einen Token live aus einem schon offenen Anmeldefenster und
prüft sonst die Festplatten-Kopien.

**Manuell (ohne Anmeldefenster):** F12 → Netzwerk → Aufnahme an + „Preserve log" → im
Web Reader anmelden → Filter `registerhw`, Header `hardware_id` kopieren → Filter
`token`, `/auth/oauth2/token` → Antwort → `refresh_token` kopieren → beide Werte
eintragen und speichern.

**Wichtig:** Token sofort verwenden — Tolino-Tokens sind nach einmaliger Nutzung
ungültig (`invalid_grant`). Kein Inkognito-Fenster.

### Häufige Fehlermeldungen

| Meldung | Tun |
|---|---|
| „… warte auf eine frische Rotation des Web Readers" | Token war verbraucht; das Plugin wartet bis zu ~90 s auf eine neu geschriebene Kopie. Fenster angemeldet lassen. |
| „… weder frischer Token … noch nachgeschoben" | **F5 im Fenster**, neu anmelden bis die Bücherliste lädt, Knopf direkt danach erneut drücken. |
| `invalid_grant` / „verbraucht oder widerrufen" | Alter Kandidat — empfohlenen Live-Weg benutzen; Disk-Kopien nur mit geschlossenen Browserfenstern lesen. |
| HTTP 403 „Zugriff geblockt" | Bot-Schutz → unten „Bot-Schutz-Komponente installieren". |

Feldbefunde und Architektur-Historie: `docs/browser-login-diagnose.md`.

## Technik (Kurzfassung)

- **Live-Grab statt Replay:** Keycloak akzeptiert pro Sitzung nur den **neuesten**
  Refresh-Token; ältere Kopien sind verbraucht und ihr Replay kann die ganze Sitzung
  widerrufen. Das Plugin liest deshalb den aktuellen Token direkt aus dem Speicher der
  laufenden Reader-Seite (CDP: localStorage, sessionStorage inkl. `oidc.user:`-Blobs,
  alle IndexedDB-Datenbanken).
- **Ein Kandidat, ein Versuch:** frischeste zuerst (JWT-`iat`), abgelehnte Kopien werden
  nie wieder eingesetzt; danach wird bis zu ~90 s alle 3 s auf eine vom Reader selbst
  geschriebene Kopie gewartet.
- **Tausch in der Reader-Seite:** der Refresh-Grant läuft als in-page `fetch` —
  derselbe TLS-Fingerprint wie beim Web Reader, dadurch geht er am Bot-Schutz vorbei.
- **Token-Keep-alive:** Refresh-Tokens verfallen nach ~1 Stunde Untätigkeit; solange
  Calibre läuft, rotiert das Plugin alle 45 Minuten still weiter.

## 403 und Bot-Schutz

Der Token-Endpunkt prüft TLS-Fingerprints. Transportreihenfolge: **curl_cffi**
(Chrome-TLS, bei aktivem Bot-Schutz erforderlich) → **curl** → **urllib**.

| Transport | Ergebnis |
|---|---|
| System-curl 7.81 (Ubuntu) | ❌ HTTP 403 „Zugriff geblockt" |
| curl_cffi (Chrome-Imitation) | ✅ HTTP 400 `invalid_grant` — WAF passiert |

**Ein-Klick:** Button **„Bot-Schutz-Komponente installieren (einmalig)"** lädt die
curl_cffi-Wheels von PyPI, prüft SHA-256 und entpackt sie nach
`<Calibre-Konfig>/plugins/curl_cffi-libs/` — ohne pip, ohne Neustart. Bei 403 zeigt die
Fehlermeldung die bereinigte Serverantwort: „Access denied" (Bot-Schutz) ist von
`invalid_grant` (verbrauchter Token) unterscheidbar.

## Validierung

```text
python3 -m unittest calibre_plugin.test_sync
python3 build_plugin.py
```

## Weitere Funktionen

- **Hardware-ID automatisch auflösen** aus der Tolino-Geräteliste
- **Buch herunterladen** (`TolinoClient.download(deliverable_id)`)
- **Sammlungen** (`add_to_collection` / `remove_from_collection`) und
  **Gelesen-Markierung** (`mark_read`)
