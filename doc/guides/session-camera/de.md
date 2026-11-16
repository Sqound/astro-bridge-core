## Sitzungen und lokaler Verlauf

Karten verbundener Instrumente zeigen aktivierte Sitzungsdaten nur lesend:
Vorgang, Ziel, Werte mit Einheiten und Alter. Nach drei Quellintervallen wird ein
Wert als letzter Messwert markiert. Pausiert bedeutet, dass der Beobachter
den Zugriff abgegeben hat, nicht dass das Instrument angehalten wurde. Fehlende Werte
bleiben unbekannt. Wählen Sie die Daten in jedem Teleskopprofil. Seestar-Stapeldaten
benötigen einen Schlüssel des Eigentümers; der Abruf erfordert eine ausdrückliche
Aktion und Internet auf der Bridge. Gespeicherte Mitleseentscheidungen bleiben beim
Schlüsselwechsel erhalten. DWARF bietet nur Verbindungsdaten und erlaubt eine App.

Sitzung öffnen zeigt aufbewahrte Diagramme, Bildmaxima, Ablehnungsgründe und
Umgebung. Beobachtete Belichtung zählt nur gesehene Zunahmen behaltener Bilder bei
unveränderter Einzelbelichtungszeit. Fehlende Ausgangswerte, Änderungen oder Resets
können die Summe unvollständig machen; die letzte Einzelbelichtungszeit steht separat.
Schraffuren bedeuten keinen aufgezeichneten Rohdatenabruf durch eine gekoppelte App,
nicht eine geschlossene App. Die Abdeckung alter oder zusammengefasster Protokolle
bleibt unbekannt. Unverifizierte Uhren liefern ungefähre Zeiten; die Sequenzreihenfolge
bleibt gültig. Letzte Ereignisse sind auf 500 Messwerte und Lebenszykluseinträge
begrenzt. CSV/JSON enthalten alle aufbewahrten Messwerte; JSON auch Ereignisse und
Lesernamen. Konfiguration → Sitzungen legt Nächte und MiB fest; die zuerst erreichte
Grenze gilt. Niedrigere Grenzen können alte Daten entfernen. Löschen ist endgültig:
zuerst exportieren, dann ausdrücklich bestätigen. Nur gekoppelte Apps mit Telemetrie-
oder Administratorrecht dürfen Sitzungen lesen; diese Rechte steuern kein Instrument.
Der Verlauf bleibt auch ohne lesende App lokal gespeichert.

## Angeschlossene Kameras und Grenzen

Pi Zero W und Zero 2 W dienen nur Verbindungen und Telemetrie. Kameras benötigen
die freigegebene Pi-3/4-Klasse; beginnen Sie die physische Kameraabnahme auf einem
Pi-3B-Prüfstand. Aktivieren Sie Bereitstellung und Erkennung bewusst unter
Konfiguration → Kameras. Demo startet ohne Kamera; der Anschlussknopf fügt nur einen
Loopback-Simulator hinzu. Clients entdecken die Bridge und fragen tatsächliche
Endpunkte je Verbindung ab; Sie müssen keine Instrumentadresse eingeben. Alpaca-
Erkennung antwortet nur in bestätigten Verwaltungsnetzen. Adressen erscheinen erst,
wenn der jeweilige Listener erfolgreich gebunden wurde.

Nur physisch angeschlossene unterstützte Kameras werden bereitgestellt. UVC braucht
unterstützte Aufnahmeformate und eine Eigentümerkalibrierung des Pixelabstands über
camera-calibration.json; es gibt keinen Kalibrierungseditor. Folgen Sie dem
Eigentümerverfahren, erhalten Sie andere Einträge und stecken
Sie danach sicher neu an oder starten Sie neu. Portbasierte IDs beweisen keine
physische Identität. UVC erfasst begrenzte Luminanzbilder (GREY/Y16/YUYV), höchstens
1280×960 und nur Binning 1. Lange Anforderungen mitteln mehrere Bilder; dies ist
keine echte durchgehende Langzeitbelichtung. Ohne Belichtungssteuerung ist die
Bilddauer nominal. Kameras ohne Verschluss erzeugen keine künstlichen Dunkelbilder.
Kein Hersteller-SDK wird mitgeliefert; SDK-Kameras brauchen installierte unterstützte
Treiber. Große Übertragungen über 2.4 GHz können langsam sein. Unterstütztes Binning 2
ist nur ein Anfangswert; Apps behalten ihre Auswahl.

Jeweils ein Alpaca-Client besitzt die Kamera. ClientID ist eine Protokollnummer,
keine gekoppelte App. DHCP-Namen erscheinen nur bei aktuellem exaktem Adressabgleich.
Diese App darf sie nutzen verlangt ausdrückliche Zustimmung zur Freigabe des
angezeigten aktuellen Besitzers und kann eine Belichtung unterbrechen. Ein
Besitzerwechsel macht alte Zustimmung ungültig. Die andere App muss sich nach der
Freigabe neu verbinden; ein neuer Besitzer wird nicht automatisch bestimmt.
Wiederholte Trennungen legen Kabelprüfung und einen versorgten USB-Hub nahe, beweisen
aber keinen Spannungsfehler. Echte Kameras/Telefone, Pi-Leistung und ConformU bleiben
**dokumentiert, nicht an Hardware verifiziert**; siehe USB-Grenzen.
