# Changelog

## 1.0.8

- New: terminals can hang behind a load's output, e.g. sockets fed by a UPS ("Notstrom"): in review mode, "⇅"
  on the terminal page swaps a terminal with the load below it (and back), for L, N and PE; "+" on the wire below
  the load adds further terminals beside it. Only links on a terminal, device channel or circuit breaker carry an
  output, not a load connected directly to N or PE.
- Overall graph ("Gesamtgraph", view "Sicherungen"):
  - Loads behind such an output are drawn with their L, N and PE; the wires leave the load's card, never a busbar.
  - The L, N and PE terminals in front of a load stand one above the other (also in front of a UPS), and a line of
    terminals behind a terminal box gets the matching N and PE terminal in each column's box.
  - Circuit breakers sharing one load (e.g. a three-phase socket on L1, L2, L3) always share one block, also when
    one of them feeds a further load; that load stands below the shared one in its column. The block is no wider
    than its content.
  - Neutral/PE terminals shared by several loads of one circuit breaker are drawn once; numbered slots of a
    terminal strip (300/6, 300/n) no longer count as one cable; wires run straight where they can.
  - N and PE connected directly to their conductor no longer show an empty terminal box.
- PDF export of the overall graph: rows broken onto a new line keep their N/PE wires in their own lanes and enter
  the terminals from the left; a line of terminal boxes no longer breaks inside.
- Fix: adding or inserting a terminal no longer moves a loose node of it together with a load and everything
  behind that load.

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
