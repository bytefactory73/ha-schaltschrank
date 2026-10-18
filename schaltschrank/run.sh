#!/bin/sh
# Start of the add-on. The database lives in /data (kept across updates, part of Home Assistant backups).
# On the very first start there is none: the app creates an empty one, the data then comes in through
# Einstellungen -> "Datenbank importieren". An update of the add-on never touches /data.
set -e
DB=/data/schaltschrank.sqlite3
# Test installations before the first release called the file schaltschrank8.sqlite3: renamed once, the data stay.
if [ ! -e "$DB" ] && [ -f /data/schaltschrank8.sqlite3 ]; then
    mv /data/schaltschrank8.sqlite3 "$DB"
    echo "Database renamed: /data/schaltschrank8.sqlite3 -> $DB"
fi
# KNX panel entities and entity registry from the read-only config folder.
export SCHALTSCHRANK_HA_STORAGE=/homeassistant/.storage
exec python /app/app.py --ingress --port 8765 --db "$DB" --create-if-missing
