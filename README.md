# Listenbrücke

Eine selbst gehostete App mit deutscher Weboberfläche für die Einkaufslisten
von **Bring!**, **Monsieur Cuisine** und **Home Assistant**. Konten verbinden,
Verbindung testen, Listen auswählen und den automatischen Abgleich einschalten.

Die App kann als Home-Assistant-App oder eigenständig mit Docker laufen.
Der PC und diese Codex-Sitzung werden für den späteren Betrieb nicht benötigt.

## Stand

Version **0.1.0, experimentell**. Die Anbieter-Anbindungen, Anmeldung, Speicherung
und Synchronisierung werden mit simulierten Antworten getestet. Eine erfolgreiche
Anmeldung mit einem echten Konto und ein vollständiger Abgleich müssen zusätzlich
in der eigenen Installation geprüft werden. Eine erreichbare Vorschau ist noch
keine Installation in Home Assistant. Es erfolgt keine automatische Veröffentlichung
auf GitHub oder in einem App-Store.

## Schnellstart mit Docker

```sh
docker compose up --build -d
```

Danach `http://localhost:8099` öffnen und ein Passwort für die Oberfläche setzen.
Der Container speichert Konten und Synchronisierungszustand im persistenten Volume
`listenbruecke_data`. Der Anwendungsprozess läuft nach der Volume-Einrichtung als
Benutzer mit UID 1000. Für Zugriff außerhalb des eigenen Netzes einen HTTPS-Reverse-
Proxy verwenden; `LISTENBRUECKE_SECURE_COOKIE=1` setzt sichere Sitzungscookies.
Eigene Domains müssen mit `LISTENBRUECKE_ALLOWED_HOSTS=listen.example.org`
(mehrere Namen kommasepariert) freigegeben werden. Standardmäßig sind lokale
Adressen zugelassen.

## Home Assistant

Für Home Assistant OS bzw. eine Installation mit Supervisor liegt eine App unter
`listenbruecke/` bereit. Für einen lokalen Test diesen vollständigen Ordner in den
lokalen App-Ordner `/addons/listenbruecke` kopieren, den App-Store neu laden und
**Listenbrücke** installieren. Nach Veröffentlichung dieses Repositorys kann dessen
URL als App-Repository hinzugefügt werden. Ein vorgefertigtes Registry-Image ist
nicht erforderlich; der Supervisor baut das Image aus dem Dockerfile.

Die Weboberfläche öffnet sich über **Ingress** in Home Assistant. Home-Assistant-
Zugriff wird dort über das App-Token eingerichtet; der App-Prozess liest keine
fremden Anmeldedateien. Die App benötigt weder Host-Netzwerk noch privilegierten
Modus noch Zugriff auf das Home-Assistant-Konfigurationsverzeichnis.

Ein Home-Assistant-Container ohne Supervisor kann die Docker-Variante verwenden.

## Konten und Abgleich

1. **Bring!**: E-Mail und Passwort in der Oberfläche eingeben, speichern und testen.
2. **Monsieur Cuisine**: Browserhelfer herunterladen, in Chromium/Chrome/Edge als
   entpackte Erweiterung installieren und **Mit Lidl anmelden** verwenden. Das
   kopierte Anmeldeergebnis des Helfers in die Oberfläche einfügen. Passwort und
   Captcha werden auf der Lidl-Seite eingegeben. Es werden nur die anschließend
   benötigten Sitzungstokens gespeichert.
3. **Home Assistant**: In der Home-Assistant-App automatisch verfügbar. Bei Docker
   die Instanz-URL und ein Zugriffstoken in der Oberfläche eingeben und testen.
4. Unter **Listen & Abgleich** mindestens zwei Listen auswählen. Intervall und
   Löschverhalten festlegen, speichern und den automatischen Abgleich aktivieren.

Eine Bring!-Liste in Home Assistant ist bereits dieselbe Bring!-Liste. Solche
Spiegel werden nicht noch einmal als getrennte HA-Liste angeboten. Eine zusätzliche
lokale Home-Assistant-Liste ist optional; Bring! und Monsieur Cuisine reichen aus,
während die Bring!-Integration dieselbe Liste in Home Assistant anzeigt.

## Verhalten und Grenzen

- Der Zustand mit Artikel-IDs und letzten bestätigten Werten bleibt bei Neustarts
  erhalten. Änderungen werden vor dem Schreiben vorgemerkt und anschließend vom
  jeweiligen Anbieter bestätigt.
- Bereits erledigte Alt-Einträge werden beim ersten Einlesen nicht in alle Listen
  kopiert. Späteres Abhaken und erneutes Öffnen bekannter Artikel wird abgeglichen.
- Namen und Mengen werden als Text erhalten. Gleiche Namen mit unterschiedlichen
  Mengen bleiben getrennt; unterschiedliche Einheiten werden nicht stillschweigend
  addiert. Mehrfach vorkommende Zutaten innerhalb einer Liste bleiben erhalten.
- Gleichzeitige widersprüchliche Änderungen werden im Protokoll als Konflikt
  angezeigt. Der Benutzer kann die Angaben in den betroffenen Listen angleichen.
- Entfernen wird standardmäßig nicht in die anderen Listen übertragen. Ein bewusst
  entfernter Artikel wird trotzdem nicht einfach wieder angelegt. Übergreifendes
  Löschen ist eine einschaltbare Einstellung.
- **Monsieur-Cuisine-Rezeptzutaten** können abgehakt werden. Umbenennen, Mengenändern
  und Löschen einer einzelnen Rezeptzutat sind durch die untersuchte Schnittstelle
  nicht verlässlich belegt und werden ausdrücklich abgelehnt. Freie Einträge
  unterstützen Anlegen, Ändern, Abhaken und Löschen.
- Ein fehlgeschlagener Abruf gilt niemals als leere Liste. Bei einem unklaren
  Schreibausgang wird nicht blind erneut angelegt. Bleibt kein eindeutiger Artikel
  zum Bestätigen übrig, zeigt das Protokoll den nötigen manuellen Prüfbedarf.
- Die verwendeten Bring!- und Monsieur-Cuisine-Schnittstellen sind inoffiziell.
  Änderungen der Anbieter können Anpassungen erforderlich machen. Keine Verbindung
  oder Unterstützung durch Bring! Labs, Lidl oder Tecpal wird behauptet.

## Daten und Veröffentlichung

Zugangsdaten liegen mit Fernet authentifiziert verschlüsselt in SQLite. Der Schlüssel
liegt mit Dateirechten `0600` im selben privaten Datenverzeichnis. Das schützt vor
versehentlichen Datenbankkopien, nicht vor Administratoren mit Zugriff auf das ganze
Volume. Für ein Backup immer **Datenbank und Schlüssel zusammen** sichern. Sitzungen
nutzen HttpOnly-Cookies, das Oberfläche-Passwort einen gesalzenen Scrypt-Hash.
API-Antworten enthalten keine gespeicherten Passwörter oder Tokens.

Das Repository enthält Quellcode, Docker-Konfiguration, Home-Assistant-Metadaten,
Lizenzhinweise und Tests als Grundlage für eine Veröffentlichung. Reale Konten,
App-Daten, lokale Anmeldehelfer-Laufzeitdateien und Anhänge gehören nicht ins Git.
Vor einer öffentlichen stabilen Version sind echte Konto- und Container-Tests auf
den unterstützten Plattformen erforderlich. Code-Lizenz: MIT; übernommene Teile
siehe [Drittanbieter-Hinweise](listenbruecke/THIRD_PARTY_LICENSES.md).

## Entwicklung

```sh
python3 -m venv .venv
.venv/bin/pip install -e './listenbruecke[test]'
PYTHONPATH=listenbruecke/src .venv/bin/pytest listenbruecke/tests
node --test listenbruecke/tests/test_browser_helper.cjs
npm --prefix listenbruecke ci
npx --prefix listenbruecke playwright install chromium
npm --prefix listenbruecke run test:ui
LISTENBRUECKE_DATA=./data .venv/bin/listenbruecke
```

Der frühere `ha_connect.py`-Helfer ist ein getrenntes, temporäres Werkzeug für eine
einmalige Diagnose dieser Arbeitsumgebung. Er ist weder Bestandteil des App-Containers
noch eine Voraussetzung für den dauerhaften Abgleich.
