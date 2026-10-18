# Schaltschrank

The Schaltschrank (electrical cabinet) database – terminals (Klemmen), circuit breakers (Sicherungen), RCDs (FI),
devices/channels, loads (Verbraucher) and the overall graph – in the Home Assistant sidebar, plus the mapping of
KNX entities to their device, channel, terminal, circuit breaker and RCD. The app's user interface is in German;
its labels are quoted below as they appear.

## What the add-on does in Home Assistant

It changes nothing. It reads:

- the entities of the KNX panel and the entity registry from `.storage` (configuration folder, mounted
  read-only),
- the current states through the Home Assistant API (read-only).

## First start

The add-on ships without data and starts with an empty database. Then:

1. Switch on "Überprüfungsmodus" (review mode): gear icon → Einstellungen (settings).
2. "Datenbank importieren" (import database) → choose the database file (`.sqlite3`), e.g. one downloaded
   before from an earlier installation with "Datenbank exportieren" (export database).
3. The file is checked and loaded, the server restarts and the page reloads.

## Configuration

- **Titel** (title): the small line at the top left above "Schaltschrank" (e.g. the name of the house); when
  empty, only "Schaltschrank" is shown.
- **Anzahl Sicherungen** (number of backups): how many backups in `/data/backups/` the app keeps (default 20);
  older ones are deleted with the next backup.

Restart the app after saving.

## Schaltschrank symbol in the dialog of a KNX entity (optional)

The more-info dialog of every KNX entity (light, switch, …) gets a Schaltschrank symbol next to history and
settings; it opens the entity's terminal path (else its channel or device) with terminal, circuit breaker and RCD.

1. Copy `schaltschrank-link.js` from this folder to `/config/www/schaltschrank-link.js`.
2. Add to `configuration.yaml` (if `frontend:` already exists, only add `extra_module_url` below it):

   ```yaml
   frontend:
     extra_module_url:
       - /local/schaltschrank-link.js?v=2
   ```

3. Restart Home Assistant and reload the browser. "Show in sidebar" must be on for the add-on.

After a new version of the file, increase the number after `?v=`, otherwise the browser keeps the old one.

## Data

- The database is stored in `/data/schaltschrank.sqlite3` of the add-on and is included in Home Assistant
  backups (the add-on pauses briefly during a backup). **It is the database of record.**
- Add-on updates never change it.
- Before every start and every import the app writes a verified copy to `/data/backups/`.
- "Datenbank exportieren" (export database) downloads a consistent copy at any time (e.g. to work on a copy).

## Access

Only through Home Assistant (ingress, with Home Assistant's login). The server in the add-on answers only
requests from the ingress proxy and has no port of its own to the outside.

## Updates

New versions appear in the app store as "Update", with the changelog in the update dialog. If not, use
App store → ⋮ → "Check for updates". The database in `/data` is kept.
