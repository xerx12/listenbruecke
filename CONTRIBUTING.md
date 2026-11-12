# Zu Listenbrücke beitragen

Fehlerberichte und neue Anbieter sind willkommen. Ein Issue sollte den gewünschten
Dienst, vorhandene API-Dokumentation und die benötigten Listenfunktionen nennen.
Zugangsdaten, Tokens, persönliche Einkaufslisten und Backups gehören nicht ins Issue.

## Einen Anbieter ergänzen

1. Die echte Einkaufslisten-Schnittstelle, Anmeldung und Lizenz prüfen. Ein Rezept-
   Export ersetzt keine laufende Einkaufsliste. Offizielle APIs bevorzugen; Grenzen
   inoffizieller Zugänge offen dokumentieren. Keine fremden Zugangsdaten benötigen.
2. Unter `listenbruecke/src/listenbruecke/connectors/` einen asynchronen Connector
   nach `connectors/base.py` implementieren. Konstruktoren erhalten Zugangsdaten
   und bei Bedarf einen asynchronen Callback zum Speichern erneuerter Sitzungen.
3. In `providers.py` einen `ProviderSpec` ergänzen: stabile ID, Anzeigename,
   Formularfelder, Hinweise, Validierung und Factory. Die GUI erzeugt zusätzliche
   Formulare aus diesen Daten; `/api/providers` enthält ausschließlich öffentliche
   Metadaten. Nur geprüfte, mitgelieferte Connectoren werden geladen.
4. Für die Anbieteranzeige ein vorhandenes SVG-Symbol verwenden oder ein passendes
   Symbol in `web/index.html` ergänzen. Optional eine Icon-Farbklasse ergänzen.
   Die native HA-Integration erzeugt Verbindungsentitäten aus der Anbieterliste.
5. Abhängigkeiten exakt pinnen, `requirements.lock` ergänzen und erforderliche
   Lizenzhinweise beilegen. Hersteller- und Projektbezeichnungen nicht vermischen.
6. Connector mit synthetischen Antworten testen und Grenzen in README und DOCS
   dokumentieren. Einen Pull Request mit Verhalten, Einschränkungen und Prüfungen öffnen.

## Vertrag und wichtige Grenzen

`ShoppingList` und `Item` sind die gemeinsamen Modelle. Benötigt werden
`test_connection`, `list_lists`, `get_items`, `add_item`, `update_item`, `delete_item`
und `close`. IDs müssen stabil sein; eine Mutation muss den bestätigten Eintrag
zurückgeben. Freitext-Mengen bleiben Text. Rezeptzutaten mit nur änderbarem Status
bekommen `editable=False` und `deletable=False`.

Ein Abruffehler darf niemals als leere Liste zurückkommen. Vollständige Pagination,
fehlende oder fehlerhafte Antworten und abgelaufene Sitzungen beachten. Bei unklarem
Schreibausgang keine automatische Wiederholung, die Duplikate erzeugen könnte.
Fehlertexte sind fest und dürfen keine Zugangsdaten oder Serverantworten enthalten.
SDK-Debuglogs dürfen keine Sitzungen, Notizen oder Einkaufstexte ausgeben.

Tokenrotation gehört in den verschlüsselten Store-Callback, Zugangsdaten niemals in
Settings, Events, API-Status oder Dateien des Repositorys. Nur vom Benutzer gewählte
Listen bearbeiten. Fremde Notizen oder ganze Rezepte nicht entfernen, um einen
Eintrag zu löschen. Ein HA-Spiegel derselben Cloudliste muss als solcher erkannt
werden, damit keine Schleife entsteht.

## Prüfen

Im App-Verzeichnis eine Python-Umgebung ab 3.13 mit den gepinnten Laufzeit- und
Testabhängigkeiten einrichten. Aus dem Repository-Hauptverzeichnis:

```sh
PYTHONPATH=listenbruecke/src python -m pytest -q listenbruecke/tests
cd listenbruecke
npm ci
npm run test:ui
npm run test:ui:backend
```

Die native HA-Integration hat eine eigene Umgebung mit den Anforderungen aus
`tests/ha_native/requirements.txt`; dort `python -m pytest -q tests/ha_native` vom
Repository-Hauptverzeichnis ausführen. Synthetische Tests ersetzen keine ausdrücklich
als solche dokumentierten Live-Tests mit dem eigenen Konto.
