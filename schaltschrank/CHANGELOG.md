# Changelog

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
