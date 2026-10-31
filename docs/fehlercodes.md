# Fehlercodes

Jede Fehlermeldung des Plugins endet mit einem Code, z. B. «(Code A01)».
Der Code steht im Dialog; technische Details stehen im Calibre-Log.
Das Plugin protokolliert weder Zugangstoken noch andere Anmeldedaten.

| Code | Bedeutung |
|------|-----------|
| A01 | Anmeldung erforderlich oder von tolino abgelehnt |
| B01 | Browser-Anmeldung nicht möglich (kein Browser, Start- oder Flatpak-Problem) |
| B02 | Browser-Anmeldung nicht abgeschlossen (geschlossen oder Zeitüberschreitung) |
| N01 | Verbindung fehlgeschlagen oder Zeitüberschreitung |
| N02 | tolino-Antwort ungültig oder unvollständig; Upload, Download oder Metadaten nicht bestätigt |
| N03 | tolino meldet einen HTTP-Fehler (u. a. 403 Bot-Schutz) |
| S91 | Zugang wird gerade erneuert (Refresh-Token in Benutzung) |
| E99 | unerwarteter Fehler |

Die Codes vergibt der Quelltext an genau einer Stelle je Meldung
(`code="A01"` usw.). Ein Test hält Tabelle und Quelltext zusammen: steht im
Code ein Code, der hier fehlt, schlägt die Suite fehl. Den nächsten Schritt
zu jedem Code nennt der Dialog selbst; er steht nur in
`ui_common._LOGIN_GUIDANCE`, damit derselbe Satz nicht zweimal gepflegt wird.

Bei A01 einmal neu bei tolino anmelden. Bei B01 entweder einen Chromium-Browser
installieren oder Zugangstoken und Geräte-ID in den Einstellungen eintragen;
unter Flatpak steht der passende Freigabebefehl im Calibre-Log. Bei B02 das
Anmeldefenster offen lassen, bis die Bücherliste lädt. Eine fehlgeschlagene
Verbindung kann nach einem Timeout bereits den einmalig nutzbaren
Zugangstoken verbraucht haben; erscheint danach A01, neu anmelden.
