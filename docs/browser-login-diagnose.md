# Browser-Login: Diagnose

Wie die Browser-Anmeldung funktioniert und was abgesichert ist. Die vollständige
Historie der Feldbefunde steht im Git-Log (`git log`).

## Aktuelle Login-Kette

1. Eigenes Chromium-Fenster (privates Profil, DevTools-Port, Flatpak-fähig)
   öffnet den Web Reader; als offen gilt es, solange der DevTools-Endpunkt
   antwortet (Partner-Keycloak-Seite statt `mytolino.com` ist kein Abbruch).
2. Storage-Grab (localStorage + sessionStorage inkl. JSON-Blobs + IndexedDB)
   → frischster `typ: Refresh`-Kandidat; gekoderte Werte (base64/JSON/URL)
   werden entpackt, CryptoJS-verschlüsselte userToken/userInfos-Blobs
   entschlüsselt (Klartext-JWT plus Hardware-Id).
3. Tausch **in der Reader-Seite** (in-page fetch) → sofortiges Speichern des
   rotierten Tokens; danach schließt sich das Anmeldefenster (die Sitzung
   gehört ab jetzt Calibre).
4. Bei Ablehnung/Leere: Speicher alle 3 s erneut lesen, bis der Reader selbst
   einen frischen Token schreibt (bis ~90 s; Fenster auf der Anmeldeseite bis
   ~180 s mit Aufforderung zur Neu-Anmeldung vor Ort), dann genau einmal
   tauschen. Kommt nichts: Fenster weg → schließen und Neustart auffordern;
   Fenster offen ohne Reader-Tab → offen lassen, Neu-Anmeldung vor Ort
   (das Fenster wird wiederverwendet). Meldungen nennen das ALTER des
   Kandidaten (JWT-iat), nie den Wert.
5. Ohne Chromium: Disk-Scrape-Fallback (vorher ALLE Browserfenster schließen,
   damit der letzte Token geflushed wird).

## Was historisch schiefging — und jetzt abgesichert ist

- **Nur der neueste Token zählt:** Replay verbrauchter Kopien löst Keycloak-
  Reuse-Schutz aus und beendet die Sitzung → Live-Grab, genau ein Tausch.
- **Gekoderte/verschlüsselte Kandidaten** werden vor dem Tausch entpackt bzw.
  entschlüsselt.
- **Hardware-ID:** BOSH-400 (`{}`) heilt über Geräte-Übernahme/-Registrierung
  mit kurzem Retry.
- **Bot-Schutz (403):** Transportkette curl → urllib mit automatischem
  Wechsel; OAuth-Antworten brechen sofort ab (kein Grant-Replay).
- **Alle Shops über den gemeinsamen Web Reader** (`webreader.mytolino.com`);
  localhost-Callback nur noch bei Reseller 8.
- **Flatpak:** die Host-Sonde endet mit `exit 0` und wertet ihre Ausgabe statt
  des Fehlercodes aus (sonst meldete sie immer „Freigabe fehlt“); die
  Meldungen nennen beide Override-Varianten plus Nachweis-Befehl.

## Flatpak-Freigabe (Calibre von Flathub)

```text
flatpak override --user --talk-name=org.freedesktop.Flatpak com.calibre_ebook.calibre
# System-Installation (flatpak info zeigt „Installation: system“):
sudo flatpak override --talk-name=org.freedesktop.Flatpak com.calibre_ebook.calibre
```

Danach Calibre **vollständig** neu starten. Nachweis:
`flatpak info --show-permissions com.calibre_ebook.calibre` muss
`org.freedesktop.Flatpak` enthalten.
