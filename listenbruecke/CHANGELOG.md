# Änderungen

## 0.3.0

- Native Home-Assistant-Integration mit Gerät, gemeinsamer To-do-Liste und Steuerung.
- Artikel aus Dashboard, Standardaktionen und Assist in die gemeinsame Liste aufnehmen.
- Anbieterstatus, letzter Erfolg, offene Artikel, Problemstatus und übertragene Änderungen.
- Automatischen Abgleich schalten, Intervall ändern und manuell starten.
- Dauerhafte lokale Artikel mit stabilen IDs und Übernahme vorhandener Sync-Baselines.
- Eigener Verbindungsschlüssel mit beschränkter lokaler API statt zusätzlicher Herstellerzugänge.
- Eigene App-Store- und Integrationsgrafiken sowie gültiges Sidebar-Brückensymbol.
- Integration für Home Assistant ab 2026.10.4, Installation über HACS oder manuell.

## 0.2.0

- Amazon Alexa direkt mit Amazon-Anmeldung und aktuellem Authenticator-Code verbinden.
- Verschlüsselte Amazon-Sitzung; Passwort und Einmalcode werden nicht gespeichert.
- Alexa-Listen lesen, ergänzen, ändern, abhaken und löschen.
- Beliebige zwei bis vier Anbieter auswählen, auch Bring! und Alexa allein.
- Schutz vor gleichzeitigem Abgleich eines direkten Alexa-Kontos mit seinem HA-Spiegel.
- Python ab 3.13 für den aktuellen Amazon-Client; Docker bleibt auf Python 3.13.

## 0.1.0

- Deutsche Weboberfläche für Konten, Verbindungstests, Listenauswahl und Protokoll.
- Bring!, Monsieur Cuisine und native Home-Assistant-To-do-Listen.
- Verschlüsselte Speicherung, Browseranmeldung mit PKCE und kleiner Browserhelfer.
- Persistenter Abgleich mit Erledigt-Status, Konflikterkennung und Schreibbestätigung.
- Home-Assistant-App mit Ingress sowie eigenständiger Docker-Betrieb.
- Experimenteller Erststand; echte Anbieter- und Containerprüfung noch erforderlich.
