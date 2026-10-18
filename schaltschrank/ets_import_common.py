from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from itertools import combinations
from pathlib import Path


DEVICE_ID = re.compile(r"\d+\.\d+\.\d+\Z")
GROUP_ADDRESS = re.compile(r"\d+(?:/\d+){0,2}\Z")
FUSE_LABEL = re.compile(r"([^\W\d_]+)\s*(\d+)\Z", re.UNICODE)
VALID_PHASES = {"L1", "L2", "L3"}


def join_phases(phases: object) -> str:
    """Canonical stored form of a phase set: sorted, comma separated (e.g. 'L1,L3'); '' for none."""
    return ",".join(sorted(set(phases)))


# `fuses.phases` column: a constrained L1/L2/L3 set, stored in the canonical form of join_phases().
PHASES_COLUMN = "phases TEXT NOT NULL DEFAULT '' CHECK(phases IN ({}))".format(
    ", ".join(
        f"'{join_phases(combination)}'"
        for combination in ((), *(c for size in (1, 2, 3) for c in combinations(sorted(VALID_PHASES), size)))
    )
)


def normalize_fuse_label(value: object) -> str:
    """Collapse spacing and separate alphabetic fuse prefixes from numbers."""
    label = " ".join(str(value or "").split())
    match = FUSE_LABEL.fullmatch(label)
    return f"{match.group(1)} {match.group(2)}" if match else label


def upsert_fi_device(
    db: sqlite3.Connection,
    location_id: int,
    number: str = "",
    label: str = "",
) -> int | None:
    number = " ".join(number.split())
    label = " ".join(label.split())
    if not number and not label:
        return None
    existing = None
    if number:
        existing = db.execute(
            "SELECT id, label FROM fi_devices WHERE location_id=? AND number=?",
            (location_id, number),
        ).fetchone()
    if existing is None and label:
        existing = db.execute(
            "SELECT id, number FROM fi_devices WHERE location_id=? AND label=?",
            (location_id, label),
        ).fetchone()
    if existing:
        db.execute(
            "UPDATE fi_devices SET number=CASE WHEN ?<>'' THEN ? ELSE number END, "
            "label=CASE WHEN ?<>'' THEN ? ELSE label END WHERE id=?",
            (number, number, label, label, existing[0]),
        )
        return int(existing[0])
    cursor = db.execute(
        "INSERT INTO fi_devices(location_id, number, label) VALUES (?,?,?)",
        (location_id, number or None, label),
    )
    return int(cursor.lastrowid)


def upsert_fuse(
    db: sqlite3.Connection,
    location_id: int,
    label: str,
    rating: str = "",
    description: str = "",
) -> int:
    label = normalize_fuse_label(label)
    if not label:
        raise ValueError("Fuse designation is required.")
    db.execute(
        """INSERT INTO fuses(location_id, label, rating, description)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(location_id, label) DO UPDATE SET
             rating=CASE WHEN excluded.rating<>'' THEN excluded.rating ELSE fuses.rating END,
             description=CASE WHEN excluded.description<>'' THEN excluded.description ELSE fuses.description END""",
        (location_id, label, rating, description),
    )
    row = db.execute(
        "SELECT id FROM fuses WHERE location_id=? AND label=?", (location_id, label)
    ).fetchone()
    return int(row[0])


def migrate_optional_fi_labels(db: sqlite3.Connection) -> None:
    """Remove the legacy uniqueness constraint from optional FI labels."""
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='fi_devices'"
    ).fetchone()
    normalized_sql = "".join(str(row[0] or "").lower().split()) if row else ""
    if "unique(location_id,label)" not in normalized_sql:
        return
    existing_columns = {column[1] for column in db.execute("PRAGMA table_info(fi_devices)")}
    reviewed_at_expression = "reviewed_at" if "reviewed_at" in existing_columns else "NULL"
    foreign_keys_enabled = bool(db.execute("PRAGMA foreign_keys").fetchone()[0])
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.executescript(
            f"""BEGIN;
               CREATE TABLE fi_devices_new (
                   id INTEGER PRIMARY KEY,
                   location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
                   number TEXT,
                   label TEXT NOT NULL DEFAULT '',
                   reviewed_at TEXT,
                   UNIQUE(location_id, number)
               );
               INSERT INTO fi_devices_new(id,location_id,number,label,reviewed_at)
                   SELECT id,location_id,number,label,{reviewed_at_expression} FROM fi_devices;
               DROP TABLE fi_devices;
               ALTER TABLE fi_devices_new RENAME TO fi_devices;
               COMMIT;"""
        )
    finally:
        db.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys_enabled else 'OFF'}")


def drop_terminal_fi_label(db: sqlite3.Connection) -> None:
    """Remove the legacy per-Klemme FI label; the FI of a Klemme is derived from the tree (`fi_devices.label`)."""
    if "fi_label" in {row[1] for row in db.execute("PRAGMA table_info(terminals)")}:
        db.execute("ALTER TABLE terminals DROP COLUMN fi_label")


def merge_fuse_phases(db: sqlite3.Connection) -> None:
    """Move the legacy `fuse_phases` rows into the `fuses.phases` column and drop that table."""
    if "phases" not in {row[1] for row in db.execute("PRAGMA table_info(fuses)")}:
        db.execute(f"ALTER TABLE fuses ADD COLUMN {PHASES_COLUMN}")
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fuse_phases'").fetchone():
        for fuse_id, phases in db.execute("SELECT fuse_id, group_concat(phase) FROM fuse_phases GROUP BY fuse_id").fetchall():
            db.execute("UPDATE fuses SET phases=? WHERE id=?", (join_phases(phases.split(",")), fuse_id))
        db.execute("DROP TABLE fuse_phases")


def drop_legacy_schema(db: sqlite3.Connection) -> None:
    """Remove unused tables and indexes that other constraints already cover."""
    db.execute("DROP TABLE IF EXISTS pdf_klemme_observations")
    db.execute("DROP TABLE IF EXISTS klemmleisten_source_rows")
    # UNIQUE(device_id, object_number) and UNIQUE(location_id, label) already provide these lookups.
    for index in ("idx_channels_device", "idx_terminals_location", "idx_fuses_location", "idx_terminals_label"):
        db.execute(f"DROP INDEX IF EXISTS {index}")


def backup_database(path: Path) -> Path | None:
    """Create a verified timestamped backup without replacing an existing file.

    Backups go into the folder `backups` next to the database (created on first use).
    """
    if not path.is_file():
        return None

    backup_dir = path.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}.backup-", suffix=path.suffix, dir=backup_dir
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = backup_dir / f"{path.stem}.backup-{stamp}{path.suffix}"
    try:
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(temporary_path)) as destination:
            source.backup(destination)
        with closing(sqlite3.connect(f"file:{temporary_path}?mode=ro", uri=True)) as verification:
            result = verification.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise sqlite3.DatabaseError(f"Backup verification failed for {path}.")
        created_backup = False
        try:
            with temporary_path.open("rb") as source:
                with backup_path.open("xb") as destination:
                    created_backup = True
                    shutil.copyfileobj(source, destination)
        except OSError:
            if created_backup:
                backup_path.unlink(missing_ok=True)
            raise
        return backup_path
    finally:
        temporary_path.unlink(missing_ok=True)


SCHEMA = f"""
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    manual_name TEXT NOT NULL DEFAULT '',
    is_manual INTEGER NOT NULL DEFAULT 0,
    manufacturer TEXT,
    order_number TEXT,
    product TEXT,
    application TEXT,
    description TEXT,
    location TEXT,
    comment TEXT,
    reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    object_number TEXT NOT NULL,
    physical_channel TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    function TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    data_type TEXT NOT NULL DEFAULT '',
    priority TEXT NOT NULL DEFAULT '',
    flags TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT,
    UNIQUE(device_id, object_number)
);
CREATE TABLE IF NOT EXISTS group_addresses (
    address TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    data_type TEXT NOT NULL DEFAULT '',
    length TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS channel_group_addresses (
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    address TEXT NOT NULL REFERENCES group_addresses(address) ON DELETE CASCADE,
    direction TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(channel_id, address)
);
CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS fi_devices (
    id INTEGER PRIMARY KEY,
    location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    number TEXT,
    label TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT,
    UNIQUE(location_id, number)
);
CREATE TABLE IF NOT EXISTS fuses (
    id INTEGER PRIMARY KEY,
    location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    rating TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT,
    {PHASES_COLUMN},
    UNIQUE(location_id, label)
);
CREATE TABLE IF NOT EXISTS terminals (
    id INTEGER PRIMARY KEY,
    location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    terminal_number TEXT NOT NULL DEFAULT '',
    position TEXT NOT NULL DEFAULT '',
    wire_color TEXT NOT NULL DEFAULT '',
    used INTEGER NOT NULL DEFAULT 1 CHECK (used IN (0, 1)),
    reviewed_at TEXT,
    UNIQUE(location_id, label)
);
CREATE TABLE IF NOT EXISTS source_records (
    id INTEGER PRIMARY KEY,
    source_file TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    record_type TEXT NOT NULL,
    record_key TEXT NOT NULL,
    raw_text TEXT NOT NULL DEFAULT '',
    UNIQUE(source_file, page_number, record_type, record_key)
);
CREATE INDEX IF NOT EXISTS idx_channels_device_physical ON channels(device_id, physical_channel, object_number);
CREATE INDEX IF NOT EXISTS idx_channel_group_address ON channel_group_addresses(address);
"""


def ensure_device_metadata_schema(db: sqlite3.Connection) -> None:
    """Add fields used to keep addressless, manually entered devices distinct."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(devices)")}
    if "manual_name" not in columns:
        db.execute("ALTER TABLE devices ADD COLUMN manual_name TEXT NOT NULL DEFAULT ''")
    if "is_manual" not in columns:
        db.execute("ALTER TABLE devices ADD COLUMN is_manual INTEGER NOT NULL DEFAULT 0")


def record_source(
    db: sqlite3.Connection,
    source: str,
    page: int,
    record_type: str,
    key: str,
    raw_text: str,
) -> None:
    db.execute(
        """INSERT INTO source_records(source_file, page_number, record_type, record_key, raw_text)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(source_file, page_number, record_type, record_key)
           DO UPDATE SET raw_text=excluded.raw_text""",
        (source, page, record_type, key, raw_text.strip()),
    )


def upsert_device(db: sqlite3.Connection, device_id: str, **values: object) -> None:
    if not DEVICE_ID.fullmatch(device_id):
        return
    if not values:
        db.execute("INSERT INTO devices(device_id) VALUES (?) ON CONFLICT(device_id) DO NOTHING", (device_id,))
        return
    fields = ["device_id", *values]
    params = [device_id, *values.values()]
    assignments = ", ".join(
        f"{field}=COALESCE(excluded.{field}, devices.{field})" for field in values
    )
    db.execute(
        f"INSERT INTO devices ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)}) "
        f"ON CONFLICT(device_id) DO UPDATE SET {assignments}",
        params,
    )


def upsert_group(
    db: sqlite3.Connection,
    address: str,
    name: str = "",
    data_type: str = "",
    length: str = "",
) -> None:
    if not GROUP_ADDRESS.fullmatch(address):
        return
    db.execute(
        """INSERT INTO group_addresses(address, name, data_type, length) VALUES (?, ?, ?, ?)
           ON CONFLICT(address) DO UPDATE SET
             name=CASE WHEN excluded.name <> '' THEN excluded.name ELSE group_addresses.name END,
             data_type=CASE WHEN excluded.data_type <> '' THEN excluded.data_type ELSE group_addresses.data_type END,
             length=CASE WHEN excluded.length <> '' THEN excluded.length ELSE group_addresses.length END""",
        (address, name, data_type, length),
    )


def upsert_channel(
    db: sqlite3.Connection,
    device_id: str,
    object_number: str,
    name: str = "",
    function: str = "",
    description: str = "",
    data_type: str = "",
    priority: str = "",
    flags: str = "",
) -> int:
    if not DEVICE_ID.fullmatch(device_id):
        raise ValueError(f"Refusing channel for invalid device ID: {device_id}")
    upsert_device(db, device_id)
    db.execute(
        """INSERT INTO channels(device_id, object_number, name, function, description, data_type, priority, flags)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(device_id, object_number) DO UPDATE SET
             name=CASE WHEN excluded.name <> '' THEN excluded.name ELSE channels.name END,
             function=CASE WHEN excluded.function <> '' THEN excluded.function ELSE channels.function END,
             description=CASE WHEN excluded.description <> '' THEN excluded.description ELSE channels.description END,
             data_type=CASE WHEN excluded.data_type <> '' THEN excluded.data_type ELSE channels.data_type END,
             priority=CASE WHEN excluded.priority <> '' THEN excluded.priority ELSE channels.priority END,
             flags=CASE WHEN excluded.flags <> '' THEN excluded.flags ELSE channels.flags END""",
        (device_id, object_number, name, function, description, data_type, priority, flags),
    )
    row = db.execute(
        "SELECT id FROM channels WHERE device_id=? AND object_number=?",
        (device_id, object_number),
    ).fetchone()
    return int(row[0])


def link_channel_group(
    db: sqlite3.Connection,
    channel_id: int,
    address: str,
    direction: str,
) -> None:
    upsert_group(db, address)
    db.execute(
           """INSERT INTO channel_group_addresses(channel_id, address, direction)
             VALUES (?, ?, ?)
           ON CONFLICT(channel_id, address) DO UPDATE SET
               direction=CASE WHEN excluded.direction <> '' THEN excluded.direction ELSE channel_group_addresses.direction END""",
           (channel_id, address, direction),
    )
