# Changelog

## 1.0.7

- Fix: saving a terminal (Klemme dialog) no longer removes the connections of terminals wired on behind other
  terminals anywhere in the database (e.g. 94/o → 300/10), and a feed terminal chosen by hand for a load keeps
  that choice. If such connections are missing in your database, restore them from a backup made before the save
  or wire them on again in review mode.
- Fix: the overall graph ("Gesamtgraph", view "Sicherungen") no longer stops with an error when a load's neutral
  terminal is drawn in the terminal box of another load.
- Fix: in the overall graph, a device channel behind a terminal box now sits on its wire; the wires into its loads
  no longer end beside the cards.

## 1.0.6

Initial release.

- Schaltschrank (electrical cabinet) database in the Home Assistant sidebar (ingress, only through Home
  Assistant's login): terminals, circuit breakers, RCDs, devices/channels, loads and the overall graph with PDF
  printing. The user interface is in German.
- Mapping of the KNX entities from the KNX panel to device, channel, terminal, circuit breaker and RCD, with live
  states, search and a review list for entities without a clear match; Home Assistant is only read.
- Optional Schaltschrank symbol in the dialog of every KNX entity (`schaltschrank-link.js`), opening the entity's
  terminal path.
- "Datenbank exportieren" / "Datenbank importieren" (export / import database) in the settings; the add-on starts
  with an empty database `/data/schaltschrank.sqlite3`, which is included in Home Assistant backups.
- Configuration tab: **Titel** (title, the small line above "Schaltschrank") and **Anzahl Sicherungen** (number
  of backups, default 20).
