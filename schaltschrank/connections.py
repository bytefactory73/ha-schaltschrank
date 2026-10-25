"""Ordered connection tree: conductor -> FI -> Sicherung -> Kanal / Klemme (-> Klemme ...) -> Verbraucher.

`connection_nodes` is the single source of truth for how FI devices, Sicherungen, device channels (Kanäle),
Klemmen and Verbraucher are connected, and in which order. Everything else is derived from it by views:

* `channel_terminals`, `terminal_channel_links`, `fuse_fi` and `terminal_fuse` are read-only views; the FI of a
  Sicherung and the Sicherung of a Klemme are not stored on `fuses`/`terminals` (see `fuse_fi_join`).
* A Klemme wired on to another Klemme hangs below it as a normal `terminal` node: a path can run through
  several Klemmen (e.g. Sa 1 -> Kanal -> 10/u -> 40/1). Such a following Klemme carries the conductor of
  the Klemme above it.
* Conductors (`wire_phases`: L1, L2, L3, N, PE) are `phase` root nodes. N carries the FIs (each FI starts its
  own neutral "N<number>") and the Klemmen on N; PE carries the PE Klemmen; L1-L3 carry the Sicherungen
  without FI. The connections decide a Klemme's role, never its wire colour.
* A Verbraucher (`loads`) is a `load` leaf behind each Klemme that feeds it (L, N, PE), on a Gerät-Kanal, directly
  on a Sicherung, or directly on N, an FI's neutral or PE. Load nodes take no part in ordering. Exception: a
  Verbraucher with an output (e.g. a UPS feeding a Steckdose) carries the Klemmen behind it at its L link; such a
  carrying load node is part of the path (`STRUCTURAL_SQL`) and can be swapped with the Klemme above or below it.
"""
from __future__ import annotations

import functools
import re
import sqlite3
from typing import Any

NODE_TYPES = ("phase", "fi", "fuse", "channel", "terminal", "load")
# Allowed parent type per node type (None = root of a chain). Which phase may carry what is checked in `validate`.
ALLOWED_PARENTS: dict[str, set[str | None]] = {
    "phase": {None},
    "fi": {"phase"},
    "fuse": {None, "phase", "fi", "channel", "terminal"},
    "channel": {None, "fuse", "terminal"},
    # A Klemme can follow another Klemme (wired on to it), or a Verbraucher's output (UPS -> Steckdose; only at
    # its L link, `validate`).
    "terminal": {None, "phase", "fi", "fuse", "channel", "terminal", "load"},
    # A Verbraucher hangs on its Klemme, or on a Gerät-Kanal behind that Klemme (e.g. the L input of a boiler).
    # It can also hang directly on a Sicherung (its L), on N, an FI (its neutral) or PE, without a Klemme (which
    # conductors: `_conductor_problems`).
    "load": {"terminal", "channel", "fuse", "phase", "fi"},
}
TYPE_RANK = {name: rank for rank, name in enumerate(NODE_TYPES)}
# Allowed child types per node type (inverse of ALLOWED_PARENTS).
TYPE_CHILDREN = {kind: {child for child in NODE_TYPES if kind in ALLOWED_PARENTS[child]} for kind in NODE_TYPES}
# Leaf node types outside the chain structure (no ordering, swapping or path rules).
SIDE_TYPES = ("load",)
SIDE_TYPES_SQL = "('load')"
# Nodes of the path structure (FROM connection_nodes): every node but a Verbraucher, which counts only while Klemmen
# hang behind it (its output).
STRUCTURAL_SQL = """(connection_nodes.node_type<>'load' OR EXISTS (SELECT 1 FROM connection_nodes behind
    WHERE behind.parent_id=connection_nodes.id AND behind.node_type='terminal'))"""
# The same for a node aliased `child` (a Klemme node is "bare" without such children: it may be moved in).
STRUCTURAL_CHILD_SQL = """(child.node_type<>'load' OR EXISTS (SELECT 1 FROM connection_nodes behind
    WHERE behind.parent_id=child.id AND behind.node_type='terminal'))"""
# The nearest Klemme above node `p` (no other Klemme between them).
NEAREST_TERMINAL_SQL = """SELECT a.terminal_id FROM connection_ancestors ca JOIN connection_nodes a ON a.id=ca.ancestor_id
    WHERE ca.node_id=p.id AND a.node_type='terminal' AND NOT EXISTS (
        SELECT 1 FROM connection_ancestors below JOIN connection_nodes k ON k.id=below.ancestor_id
        JOIN connection_ancestors above ON above.node_id=k.id AND above.ancestor_id=a.id
        WHERE below.node_id=p.id AND k.node_type='terminal')"""
# The Klemme that feeds a Verbraucher node `ld` with parent `p`: the parent itself, or the nearest Klemme above
# the Kanal (a Kanal sits on a path at most once; Klemmen above it count from the nearest one).
LOAD_FEEDER_SQL = f"""(CASE WHEN p.node_type='terminal' THEN p.terminal_id ELSE ({NEAREST_TERMINAL_SQL}) END)"""
# Every Verbraucher node with its parent and feeding Klemme.
LOAD_LINKS_SQL = f"""SELECT ld.id, ld.load_id, ld.parent_id, p.node_type AS parent_type, p.device_id, p.channel_key,
    {LOAD_FEEDER_SQL} AS terminal_id
    FROM connection_nodes ld JOIN connection_nodes p ON p.id=ld.parent_id AND ld.node_type='load'"""
# `note` of a Verbraucher link the user fixed to a Klemme with Klemmen wired on behind it (not moved to the chain end).
FIXED_LOAD_NOTE = "fixed"
# Conductors: code, name, wire colour of its Klemmen (matches terminals.wire_color).
WIRE_PHASES = (
    ("L1", "Außenleiter L1", "br"),
    ("L2", "Außenleiter L2", "bk"),
    ("L3", "Außenleiter L3", "gr"),
    ("N", "Neutralleiter", "bl"),
    ("PE", "Schutzleiter", "grge"),
)
LINE_PHASES = ("L1", "L2", "L3")
WIRE_PHASE_SEED = ",\n".join(
    f"    ({index}, '{code}', '{name}', '{color}', {index})" for index, (code, name, color) in enumerate(WIRE_PHASES, 1)
)
NEUTRAL_COLOR = "bl"
PE_COLOR = "grge"


class ConnectionTreeError(ValueError):
    """Raised when a requested change would break the connection rules."""


# Node columns besides id/parent/order/type, in table order (used when the table is rebuilt).
NODE_COLUMNS = ("fi_id", "fuse_id", "terminal_id", "device_id", "channel_key", "channel_id", "note",
                "wire_phase_id", "load_id")

CONNECTION_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS wire_phases (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE CHECK(code IN ('L1', 'L2', 'L3', 'N', 'PE')),
    name TEXT NOT NULL,
    wire_color TEXT NOT NULL,
    sort_order INTEGER NOT NULL
);
INSERT OR IGNORE INTO wire_phases(id, code, name, wire_color, sort_order) VALUES
{WIRE_PHASE_SEED};

-- A Verbraucher fed by the Klemmen of one cable (same location and Klemmen number).
CREATE TABLE IF NOT EXISTS loads (
    id INTEGER PRIMARY KEY,
    location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    terminal_number TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT,
    -- 1: the Verbraucher has no PE connection at all (e.g. protection class II, valve drives).
    no_pe INTEGER NOT NULL DEFAULT 0 CHECK (no_pe IN (0, 1)),
    UNIQUE(location_id, terminal_number, name)
);

CREATE TABLE IF NOT EXISTS connection_nodes (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES connection_nodes(id) ON DELETE CASCADE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    node_type TEXT NOT NULL CHECK(node_type IN ('phase', 'fi', 'fuse', 'channel', 'terminal', 'load')),
    fi_id INTEGER REFERENCES fi_devices(id) ON DELETE CASCADE,
    fuse_id INTEGER REFERENCES fuses(id) ON DELETE CASCADE,
    terminal_id INTEGER REFERENCES terminals(id) ON DELETE CASCADE,
    device_id TEXT REFERENCES devices(device_id) ON DELETE CASCADE,
    channel_key TEXT,
    channel_id INTEGER REFERENCES channels(id) ON DELETE CASCADE,
    note TEXT NOT NULL DEFAULT '',
    wire_phase_id INTEGER REFERENCES wire_phases(id),
    load_id INTEGER REFERENCES loads(id) ON DELETE CASCADE,
    CHECK (
        (node_type='fi' AND fi_id IS NOT NULL AND fuse_id IS NULL AND terminal_id IS NULL
            AND device_id IS NULL AND channel_key IS NULL AND channel_id IS NULL)
     OR (node_type='fuse' AND fuse_id IS NOT NULL AND fi_id IS NULL AND terminal_id IS NULL
            AND device_id IS NULL AND channel_key IS NULL AND channel_id IS NULL)
     OR (node_type='terminal' AND terminal_id IS NOT NULL AND fi_id IS NULL AND fuse_id IS NULL
            AND device_id IS NULL AND channel_key IS NULL AND channel_id IS NULL)
     OR (node_type='channel' AND device_id IS NOT NULL AND channel_key IS NOT NULL
            AND fi_id IS NULL AND fuse_id IS NULL AND terminal_id IS NULL)
     OR (node_type='phase' AND wire_phase_id IS NOT NULL AND fi_id IS NULL AND fuse_id IS NULL
            AND terminal_id IS NULL AND device_id IS NULL AND channel_key IS NULL AND channel_id IS NULL)
     OR (node_type='load' AND load_id IS NOT NULL AND fi_id IS NULL AND fuse_id IS NULL
            AND terminal_id IS NULL AND device_id IS NULL AND channel_key IS NULL AND channel_id IS NULL)
    ),
    CHECK ((node_type='phase') = (wire_phase_id IS NOT NULL)),
    CHECK ((node_type='load') = (load_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_connection_nodes_fi ON connection_nodes(fi_id) WHERE node_type='fi';
CREATE UNIQUE INDEX IF NOT EXISTS idx_connection_nodes_fuse ON connection_nodes(fuse_id) WHERE node_type='fuse';
CREATE UNIQUE INDEX IF NOT EXISTS idx_connection_nodes_terminal
    ON connection_nodes(COALESCE(parent_id, 0), terminal_id) WHERE node_type='terminal';
CREATE UNIQUE INDEX IF NOT EXISTS idx_connection_nodes_channel
    ON connection_nodes(COALESCE(parent_id, 0), device_id, channel_key, COALESCE(channel_id, 0), note)
    WHERE node_type='channel';
CREATE INDEX IF NOT EXISTS idx_connection_nodes_parent ON connection_nodes(parent_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_connection_nodes_terminal_ref ON connection_nodes(terminal_id) WHERE terminal_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_connection_nodes_channel_ref ON connection_nodes(device_id, channel_key) WHERE node_type='channel';

-- Deleting a node keeps its children: they move to the deleted node's parent when the chain
-- rules allow that, else they become roots.
CREATE TRIGGER IF NOT EXISTS connection_nodes_splice BEFORE DELETE ON connection_nodes
BEGIN
    UPDATE OR IGNORE connection_nodes
    SET parent_id = CASE
        WHEN node_type='load' THEN OLD.id
        WHEN (SELECT node_type FROM connection_nodes WHERE id=OLD.parent_id) IS NULL THEN NULL
        WHEN node_type='fuse' AND (SELECT node_type FROM connection_nodes WHERE id=OLD.parent_id) IN ('fi', 'channel', 'terminal')
            THEN OLD.parent_id
        WHEN node_type='channel' AND (SELECT node_type FROM connection_nodes WHERE id=OLD.parent_id) IN ('fuse', 'terminal')
            THEN OLD.parent_id
        WHEN node_type='terminal' AND (SELECT node_type FROM connection_nodes WHERE id=OLD.parent_id) IN ('fuse', 'channel', 'terminal')
            THEN OLD.parent_id
        END
    WHERE parent_id=OLD.id;
END;

-- Every (descendant, ancestor) pair; UNION stops the recursion even on a cycle.
CREATE VIEW IF NOT EXISTS connection_ancestors AS
WITH RECURSIVE ancestry(node_id, ancestor_id) AS (
    SELECT id, parent_id FROM connection_nodes WHERE parent_id IS NOT NULL
    UNION
    SELECT ancestry.node_id, parent.parent_id
    FROM ancestry JOIN connection_nodes parent ON parent.id=ancestry.ancestor_id
    WHERE parent.parent_id IS NOT NULL
)
SELECT node_id, ancestor_id FROM ancestry;

-- Nodes on the same root-to-leaf path are connected, in either direction.
CREATE VIEW IF NOT EXISTS connection_paths AS
SELECT node_id, ancestor_id AS other_id FROM connection_ancestors
UNION ALL
SELECT ancestor_id, node_id FROM connection_ancestors;

CREATE VIEW IF NOT EXISTS terminal_fuse_links AS
SELECT DISTINCT terminal.terminal_id AS terminal_id, fuse.fuse_id AS fuse_id
FROM connection_nodes terminal
JOIN connection_paths path ON path.node_id=terminal.id
JOIN connection_nodes fuse ON fuse.id=path.other_id AND fuse.node_type='fuse'
WHERE terminal.node_type='terminal';

-- The Sicherung of a Klemme (the lowest id if a legacy tree ever holds several).
CREATE VIEW IF NOT EXISTS terminal_fuse AS
SELECT terminal_id, MIN(fuse_id) AS fuse_id FROM terminal_fuse_links GROUP BY terminal_id;

-- The FI above a Sicherung.
CREATE VIEW IF NOT EXISTS fuse_fi AS
SELECT fuse.fuse_id AS fuse_id, MIN(fi.fi_id) AS fi_id
FROM connection_nodes fuse
JOIN connection_ancestors ancestry ON ancestry.node_id=fuse.id
JOIN connection_nodes fi ON fi.id=ancestry.ancestor_id AND fi.node_type='fi'
WHERE fuse.node_type='fuse'
GROUP BY fuse.fuse_id;

-- A pair is identified by its deeper node, so ids stay stable while siblings are reordered.
-- Only the nearest Klemme pairs with a Kanal: a Klemme wired on behind another one is not wired to the Kanal.
CREATE VIEW IF NOT EXISTS connection_channel_pairs AS
SELECT ancestry.node_id AS id, terminal.terminal_id AS terminal_id, channel.id AS channel_node_id
FROM connection_ancestors ancestry
JOIN connection_nodes channel ON channel.id=ancestry.node_id AND channel.node_type='channel'
JOIN connection_nodes terminal ON terminal.id=ancestry.ancestor_id AND terminal.node_type='terminal'
WHERE NOT EXISTS (SELECT 1 FROM connection_ancestors up JOIN connection_nodes k ON k.id=up.ancestor_id AND k.node_type='terminal'
                  JOIN connection_ancestors k_up ON k_up.node_id=k.id AND k_up.ancestor_id=terminal.id
                  WHERE up.node_id=channel.id)
UNION ALL
SELECT ancestry.node_id, terminal.terminal_id, channel.id
FROM connection_ancestors ancestry
JOIN connection_nodes terminal ON terminal.id=ancestry.node_id AND terminal.node_type='terminal'
JOIN connection_nodes channel ON channel.id=ancestry.ancestor_id AND channel.node_type='channel'
WHERE NOT EXISTS (SELECT 1 FROM connection_ancestors up JOIN connection_nodes k ON k.id=up.ancestor_id AND k.node_type='terminal'
                  JOIN connection_ancestors k_up ON k_up.node_id=k.id AND k_up.ancestor_id=channel.id
                  WHERE up.node_id=terminal.id);

-- Workbook-derived links to a physical channel label.
CREATE VIEW IF NOT EXISTS terminal_channel_links AS
SELECT MIN(pair.id) AS id, pair.terminal_id AS terminal_id, channel.device_id AS device_id,
       channel.channel_key AS physical_channel
FROM connection_channel_pairs pair
JOIN connection_nodes channel ON channel.id=pair.channel_node_id
WHERE channel.channel_id IS NULL
GROUP BY pair.terminal_id, channel.device_id, channel.channel_key;

-- Assignments to one ETS object (channels row).
CREATE VIEW IF NOT EXISTS channel_terminals AS
SELECT MIN(pair.id) AS id, channel.channel_id AS channel_id, pair.terminal_id AS terminal_id,
       MIN(channel.note) AS note
FROM connection_channel_pairs pair
JOIN connection_nodes channel ON channel.id=pair.channel_node_id
WHERE channel.channel_id IS NOT NULL
GROUP BY channel.channel_id, pair.terminal_id;
"""


# Indexes on the generation-6 columns; older node tables (used by the history migrations) lack them.
CONDUCTOR_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_connection_nodes_phase ON connection_nodes(wire_phase_id) WHERE node_type='phase';
CREATE UNIQUE INDEX IF NOT EXISTS idx_connection_nodes_load ON connection_nodes(parent_id, load_id) WHERE node_type='load';
"""


def natural_key(value: str) -> tuple[tuple[int, Any], ...]:
    """Sort key that orders digit runs numerically (Sa 2 before Sa 10)."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold().strip())
        for part in re.split(r"(\d+)", value)
    )


def channel_key(physical_channel: str | None, object_number: str | None) -> str:
    """Channel identity: the physical label, or the ETS object when there is none."""
    return physical_channel or f"object:{object_number}"


def has_connection_schema(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='connection_nodes'").fetchone() is not None


def has_legacy_link_tables(db: sqlite3.Connection) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name IN ('channel_terminals', 'terminal_channel_links')"
    ).fetchone() is not None


# (table, column) pairs that used to cache tree relations.
RELATION_COLUMNS = ("fuses", "fi_id"), ("terminals", "fuse_id")


def has_routes(db: sqlite3.Connection) -> bool:
    """True for a generation-7 (or older) database that still stores Klemme -> Klemme routes."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='terminal_routes'").fetchone():
        return True
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='connection_nodes'").fetchone()
    return row is not None and "'route'" in row[0]


def has_wire_phase_schema(db: sqlite3.Connection) -> bool:
    """True when the node table can hold phase and load nodes (generation 6), or does not exist yet."""
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='connection_nodes'").fetchone()
    return row is None or ("'phase'" in row[0] and "'load'" in row[0])


def has_legacy_columns(db: sqlite3.Connection) -> bool:
    """True while `fuses.fi_id` or `terminals.fuse_id` still exist (the tree is the only source now)."""
    return any(
        column in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for table, column in (RELATION_COLUMNS)
    )


def relation_maps(db: sqlite3.Connection) -> tuple[dict[int, int], dict[int, int]]:
    """Return (fuse -> FI, Klemme -> fuse): from the views when present, else from the legacy columns."""
    views = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    if {"fuse_fi", "terminal_fuse"} <= views:
        fuse_fi_sql = "SELECT fuse_id, fi_id FROM fuse_fi"
        terminal_fuse_sql = "SELECT terminal_id, fuse_id FROM terminal_fuse"
    else:
        fuse_fi_sql = "SELECT id, fi_id FROM fuses WHERE fi_id IS NOT NULL"
        terminal_fuse_sql = "SELECT id, fuse_id FROM terminals WHERE fuse_id IS NOT NULL"
    return dict(db.execute(fuse_fi_sql).fetchall()), dict(db.execute(terminal_fuse_sql).fetchall())


def fuse_fi_join(fuse: str, fi: str) -> str:
    """LEFT JOIN clauses attaching the FI (alias `fi`) of a Sicherung alias; aliases `<fi>_link` is used."""
    return (f"LEFT JOIN fuse_fi {fi}_link ON {fi}_link.fuse_id={fuse}.id "
            f"LEFT JOIN fi_devices {fi} ON {fi}.id={fi}_link.fi_id")


def terminal_fuse_join(terminal: str, fuse: str) -> str:
    """LEFT JOIN clauses attaching the Sicherung (alias `fuse`) of a Klemme alias; `<fuse>_link` is used."""
    return (f"LEFT JOIN terminal_fuse {fuse}_link ON {fuse}_link.terminal_id={terminal}.id "
            f"LEFT JOIN fuses {fuse} ON {fuse}.id={fuse}_link.fuse_id")


def drop_source_workbook(db: sqlite3.Connection) -> None:
    """Remove the provenance column; its view and unique index are recreated without it by the schema."""
    if "source_workbook" not in {row[1] for row in db.execute("PRAGMA table_info(connection_nodes)")}:
        return
    db.execute("DROP VIEW IF EXISTS terminal_channel_links")
    db.execute("DROP INDEX IF EXISTS idx_connection_nodes_channel")
    db.execute("ALTER TABLE connection_nodes DROP COLUMN source_workbook")


def ensure_schema(db: sqlite3.Connection) -> None:
    """Create the node table, triggers and views; refuse a database of an older generation."""
    if has_legacy_link_tables(db) or has_legacy_columns(db) or not has_wire_phase_schema(db):
        raise ConnectionTreeError(
            "This database is older than generation 7 and is kept as history only. Open schaltschrank8.sqlite3."
        )
    if has_routes(db):
        raise ConnectionTreeError(
            "This database still stores Klemme -> Klemme routes (generation 7). "
            "Run migrate_remove_routes.py to create a migrated copy."
        )
    drop_source_workbook(db)
    db.executescript(CONNECTION_SCHEMA + CONDUCTOR_INDEXES)
    # Added column (additive, existing Verbraucher keep their data): whether a Verbraucher has a PE at all.
    if "no_pe" not in {row[1] for row in db.execute("PRAGMA table_info(loads)")}:
        db.execute("ALTER TABLE loads ADD COLUMN no_pe INTEGER NOT NULL DEFAULT 0 CHECK (no_pe IN (0, 1))")


# ---------------------------------------------------------------- small helpers

def _no_pe_sql(db: sqlite3.Connection, alias: str = "ld") -> str:
    """SQL for the "no PE connection" flag of a Verbraucher; 0 in an older database without the column (e.g. a
    generation read by a migration)."""
    return f"{alias}.no_pe" if "no_pe" in {row[1] for row in db.execute("PRAGMA table_info(loads)")} else "0"


def _rows(db: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Run a query and return dicts, independent of the connection's row factory."""
    cursor = db.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _node(db: sqlite3.Connection, node_id: int) -> dict[str, Any]:
    rows = _rows(db, "SELECT * FROM connection_nodes WHERE id=?", (node_id,))
    if not rows:
        raise ConnectionTreeError("Connection node not found.")
    return rows[0]


def _children(db: sqlite3.Connection, parent_id: int | None) -> list[int]:
    """Ordered structural children; load nodes are not part of the chain structure."""
    return [row[0] for row in db.execute(
        f"SELECT id FROM connection_nodes WHERE parent_id IS ? AND node_type NOT IN {SIDE_TYPES_SQL} ORDER BY sort_order, id",
        (parent_id,),
    )]


def _write_order(db: sqlite3.Connection, ids: list[int]) -> None:
    db.executemany("UPDATE connection_nodes SET sort_order=? WHERE id=?", [(index, node_id) for index, node_id in enumerate(ids)])


def _renumber(db: sqlite3.Connection, parent_id: int | None) -> None:
    _write_order(db, _children(db, parent_id))


def _next_order(db: sqlite3.Connection, parent_id: int | None) -> int:
    return int(db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM connection_nodes WHERE parent_id IS ?", (parent_id,)
    ).fetchone()[0])


def _channel_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Normalize a channel reference (device + channel key, optional ETS object)."""
    return {
        "device_id": str(spec["device_id"]),
        "channel_key": str(spec["channel_key"]),
        "channel_id": spec.get("channel_id"),
        "note": str(spec.get("note") or ""),
    }


def _ref_columns(node_type: str, ref: dict[str, Any]) -> dict[str, Any]:
    columns: dict[str, Any] = {name: None for name in NODE_COLUMNS}
    columns["note"] = ""
    columns.update(_channel_spec(ref) if node_type == "channel" else {name: ref[name] for name in NODE_COLUMNS if name in ref})
    return columns


def _insert_node(db: sqlite3.Connection, parent_id: int | None, node_type: str, ref: dict[str, Any]) -> int:
    columns = _ref_columns(node_type, ref)
    cursor = db.execute(
        f"""INSERT INTO connection_nodes(parent_id, sort_order, node_type, {", ".join(NODE_COLUMNS)})
            VALUES (?,?,?,{",".join("?" * len(NODE_COLUMNS))})""",
        (parent_id, _next_order(db, parent_id), node_type, *(columns[name] for name in NODE_COLUMNS)),
    )
    return int(cursor.lastrowid)


def _find_node(db: sqlite3.Connection, parent_id: int | None, node_type: str, ref: dict[str, Any]) -> int | None:
    """Find an existing node: phase, FI and fuse are unique tree-wide, others per parent."""
    if node_type == "phase":
        row = db.execute("SELECT id FROM connection_nodes WHERE node_type='phase' AND wire_phase_id=?",
                         (ref["wire_phase_id"],)).fetchone()
    elif node_type == "load":
        row = db.execute("SELECT id FROM connection_nodes WHERE node_type='load' AND load_id=? AND parent_id IS ?",
                         (ref["load_id"], parent_id)).fetchone()
    elif node_type == "fi":
        row = db.execute("SELECT id FROM connection_nodes WHERE node_type='fi' AND fi_id=?", (ref["fi_id"],)).fetchone()
    elif node_type == "fuse":
        row = db.execute("SELECT id FROM connection_nodes WHERE node_type='fuse' AND fuse_id=?", (ref["fuse_id"],)).fetchone()
    elif node_type == "terminal":
        row = db.execute(
            "SELECT id FROM connection_nodes WHERE node_type='terminal' AND terminal_id=? AND parent_id IS ?",
            (ref["terminal_id"], parent_id),
        ).fetchone()
    else:
        spec = _channel_spec(ref)
        row = db.execute(
            """SELECT id FROM connection_nodes WHERE node_type='channel' AND parent_id IS ? AND device_id=?
               AND channel_key=? AND channel_id IS ? AND note=?""",
            (parent_id, spec["device_id"], spec["channel_key"], spec["channel_id"], spec["note"]),
        ).fetchone()
    return int(row[0]) if row else None


def _get_or_create(db: sqlite3.Connection, parent_id: int | None, node_type: str, ref: dict[str, Any]) -> int:
    return _find_node(db, parent_id, node_type, ref) or _insert_node(db, parent_id, node_type, ref)


def _move_raw(db: sqlite3.Connection, node_id: int, new_parent_id: int | None) -> None:
    """Re-parent a node to the end of the new parent's children."""
    old_parent = _node(db, node_id)["parent_id"]
    db.execute("UPDATE connection_nodes SET parent_id=?, sort_order=? WHERE id=?", (new_parent_id, _next_order(db, new_parent_id), node_id))
    _renumber(db, old_parent)


def _has_children(db: sqlite3.Connection, node_id: int) -> bool:
    return db.execute(
        f"SELECT 1 FROM connection_nodes WHERE parent_id=? AND node_type NOT IN {SIDE_TYPES_SQL} LIMIT 1", (node_id,)
    ).fetchone() is not None


def _structural_node(db: sqlite3.Connection, node_id: int) -> dict[str, Any]:
    """Like `_node`, but refuses side and phase nodes: Verbraucher hang on the Klemme, conductors are fixed."""
    node = _node(db, node_id)
    if node["node_type"] == "load":
        raise ConnectionTreeError("A Verbraucher is assigned to its Klemmen automatically and cannot be moved.")
    if node["node_type"] == "phase":
        raise ConnectionTreeError("The conductors L1, L2, L3, N and PE are fixed start nodes.")
    return node


# ---------------------------------------------------------------- derived data

def validate(db: sqlite3.Connection) -> None:
    """Check the tree against the connection rules; raise with all problems found."""
    problems: list[str] = []
    for node_id, node_type, parent_type in db.execute(
        """SELECT n.id, n.node_type, p.node_type FROM connection_nodes n
           LEFT JOIN connection_nodes p ON p.id=n.parent_id"""
    ):
        if parent_type not in ALLOWED_PARENTS[node_type]:
            problems.append(f"A {node_type} cannot be placed below a {parent_type or 'root'}.")
    if db.execute(f"{LOAD_LINKS_SQL} WHERE p.node_type='channel' AND {LOAD_FEEDER_SQL} IS NULL LIMIT 1").fetchone():
        problems.append("A Verbraucher below a Gerät-Kanal needs a Klemme above that Kanal.")
    if db.execute("SELECT 1 FROM connection_ancestors WHERE node_id=ancestor_id LIMIT 1").fetchone():
        problems.append("Connections must not form a loop.")
    # Several Klemmen may follow each other on a path; FI, Sicherung and Kanal appear once. Verbraucher may repeat
    # (a Steckdose behind a UPS feeds its own Verbraucher).
    if db.execute(
        """SELECT 1 FROM connection_ancestors a JOIN connection_nodes n ON n.id=a.node_id
           JOIN connection_nodes p ON p.id=a.ancestor_id WHERE n.node_type=p.node_type AND n.node_type NOT IN ('terminal', 'load')
           LIMIT 1"""
    ).fetchone():
        problems.append("A chain may contain each type (FI, Sicherung, Kanal) only once.")
    # Klemmen behind a Verbraucher hang on its output (L, N or PE of a UPS): behind a link on a Klemme, Gerät-Kanal or
    # Sicherung, not behind one directly on N, an FI's neutral or PE (no Klemme there to wire on from).
    if db.execute(
        """SELECT 1 FROM connection_nodes n JOIN connection_nodes ld ON ld.id=n.parent_id AND ld.node_type='load'
           JOIN connection_nodes p ON p.id=ld.parent_id WHERE n.node_type='terminal' AND p.node_type IN ('phase', 'fi') LIMIT 1"""
    ).fetchone():
        problems.append("A Klemme cannot hang behind a Verbraucher connected directly to a conductor.")
    if db.execute(
        """SELECT 1 FROM connection_ancestors a JOIN connection_nodes n ON n.id=a.node_id AND n.node_type='terminal'
           JOIN connection_nodes p ON p.id=a.ancestor_id AND p.node_type='terminal' WHERE n.terminal_id=p.terminal_id LIMIT 1"""
    ).fetchone():
        problems.append("A Klemme cannot follow itself on a path.")
    if db.execute(
        "SELECT 1 FROM terminal_fuse_links GROUP BY terminal_id HAVING COUNT(DISTINCT fuse_id) > 1 LIMIT 1"
    ).fetchone():
        problems.append("A Klemme can be connected to only one Sicherung.")
    problems.extend(_conductor_problems(db))
    if problems:
        raise ConnectionTreeError(" ".join(dict.fromkeys(problems)))


def _conductor_problems(db: sqlite3.Connection) -> list[str]:
    """Electrical rules for the conductor start nodes (which phase may carry what)."""
    checks = (
        # Only N and PE carry Klemmen (L1/L2/L3 carry Sicherungen). The wire colour plays no part in the path:
        # the electrician may have used any core; the connections alone decide.
        ("""SELECT 1 FROM connection_nodes n JOIN connection_nodes p ON p.id=n.parent_id AND p.node_type='phase'
            JOIN wire_phases w ON w.id=p.wire_phase_id
            WHERE n.node_type='terminal' AND w.code NOT IN ('N', 'PE') LIMIT 1""",
         "Below L1, L2 and L3 only Sicherungen can be connected; Klemmen hang below N or PE."),
        # An FI is fed from N (it starts its own neutral).
        ("""SELECT 1 FROM connection_nodes n JOIN connection_nodes p ON p.id=n.parent_id
            JOIN wire_phases w ON w.id=p.wire_phase_id WHERE n.node_type='fi' AND w.code<>'N' LIMIT 1""",
         "An FI can only be placed below N."),
        # A Klemme at N, an FI's neutral or PE is that conductor: it has no Sicherung anywhere.
        ("""SELECT 1 FROM connection_nodes n JOIN connection_nodes p ON p.id=n.parent_id AND p.node_type IN ('phase', 'fi')
            JOIN terminal_fuse_links l ON l.terminal_id=n.terminal_id WHERE n.node_type='terminal' LIMIT 1""",
         "A Klemme on N or PE cannot be connected to a Sicherung."),
        # A Sicherung below L1/L2/L3 must be on that phase.
        ("""SELECT 1 FROM connection_nodes n JOIN connection_nodes p ON p.id=n.parent_id
            JOIN wire_phases w ON w.id=p.wire_phase_id JOIN fuses f ON f.id=n.fuse_id
            WHERE n.node_type='fuse' AND instr(',' || f.phases || ',', ',' || w.code || ',')=0 LIMIT 1""",
         "A Sicherung can only be placed below the phase it is on."),
        # A Verbraucher is connected directly only to N or PE (an FI's neutral: below the FI); its L comes through a
        # Klemme or directly from its Sicherung.
        ("""SELECT 1 FROM connection_nodes n JOIN connection_nodes p ON p.id=n.parent_id
            JOIN wire_phases w ON w.id=p.wire_phase_id WHERE n.node_type='load' AND w.code NOT IN ('N', 'PE') LIMIT 1""",
         "A Verbraucher can be connected directly only to N, an FI's neutral or PE."),
    )
    return [message for sql, message in checks if db.execute(sql).fetchone()]


def finish(db: sqlite3.Connection) -> None:
    """Common end of every change: place loose FI/Sicherungen at their conductor, validate, drop empty FI/Kanal nodes."""
    place_roots(db)
    validate(db)
    # A Kanal without any Klemme on its path links nothing.
    db.execute(
        """DELETE FROM connection_nodes WHERE node_type='channel'
           AND NOT EXISTS (SELECT 1 FROM connection_nodes child WHERE child.parent_id=connection_nodes.id)
           AND NOT EXISTS (SELECT 1 FROM connection_ancestors a JOIN connection_nodes p ON p.id=a.ancestor_id
                           WHERE a.node_id=connection_nodes.id AND p.node_type='terminal')"""
    )
    db.execute(
        """DELETE FROM connection_nodes WHERE node_type='fi'
           AND NOT EXISTS (SELECT 1 FROM connection_nodes child WHERE child.parent_id=connection_nodes.id)"""
    )


def related_fuse_ids(db: sqlite3.Connection, terminal_id: int) -> set[int]:
    return {row[0] for row in db.execute("SELECT fuse_id FROM terminal_fuse_links WHERE terminal_id=?", (terminal_id,))}


def terminal_channel_specs(db: sqlite3.Connection, terminal_id: int) -> list[dict[str, Any]]:
    """Distinct channel references currently connected to a Klemme."""
    return _rows(
        db,
        """SELECT DISTINCT channel.device_id, channel.channel_key, channel.channel_id, channel.note
           FROM connection_channel_pairs pair JOIN connection_nodes channel ON channel.id=pair.channel_node_id
           WHERE pair.terminal_id=? ORDER BY channel.id""",
        (terminal_id,),
    )


# ---------------------------------------------------------------- conductors and Verbraucher

ZERO_BAR = re.compile(r"[0N]-Schiene\s*(\d+)", re.IGNORECASE)


def is_unfused_conductor(db: sqlite3.Connection, terminal_id: int) -> bool:
    """True for a Klemme of the neutral or protective conductor (by its conductor, else its colour).

    Used where the role decides the placement (an N/PE branch below a fused Kanal stays at its conductor).
    """
    return terminal_role(db, terminal_id) in ("N", "PE")


def never_fused(db: sqlite3.Connection, terminal_id: int) -> bool:
    """True when a Klemme may not get a Sicherung: it is connected to N, an FI's neutral or PE.

    Its wire colour does not matter (any core can carry any conductor)."""
    return db.execute(
        """SELECT 1 FROM connection_nodes n JOIN connection_nodes p ON p.id=n.parent_id
           WHERE n.node_type='terminal' AND n.terminal_id=? AND p.node_type IN ('phase', 'fi')""",
        (terminal_id,)).fetchone() is not None


def wire_role(color: str) -> str:
    """Conductor role of a wire colour: PE (green-yellow), N (blue), L (any other colour), '' when unknown."""
    if color == PE_COLOR:
        return "PE"
    if color == NEUTRAL_COLOR:
        return "N"
    return "L" if color else ""


def effective_role(code: str | None, color: str) -> str:
    """Role of a Klemme: its conductor decides when it has one (a neutral may be wired with another colour,
    e.g. a black core on N), else its wire colour."""
    if code:
        return "PE" if code == "PE" else "N" if code.startswith("N") else "L"
    return wire_role(color)


def terminal_role(db: sqlite3.Connection, terminal_id: int) -> str:
    """L, N or PE of one Klemme ('' for an unconnected Klemme without colour)."""
    row = db.execute("SELECT wire_color FROM terminals WHERE id=?", (terminal_id,)).fetchone()
    return effective_role(terminal_wire_phase(db, terminal_id), row[0]) if row else ""


def neutral_code(fi_number: str | None) -> str:
    """Name of a neutral: an FI starts its own one (FI 4 -> N4); without FI it is the main N."""
    return f"N{fi_number}" if fi_number else "N"


def phase_node(db: sqlite3.Connection, code: str) -> int:
    """The start node of a conductor, created on first use."""
    wire_phase_id = db.execute("SELECT id FROM wire_phases WHERE code=?", (code,)).fetchone()[0]
    return _get_or_create(db, None, "phase", {"wire_phase_id": wire_phase_id})


def place_roots(db: sqlite3.Connection) -> dict[str, int]:
    """Loose FIs go below N, loose single-phase Sicherungen below their L; returns how many moved."""
    moved = {"fi": 0, "fuse": 0}
    for (node_id,) in db.execute(
        "SELECT id FROM connection_nodes WHERE node_type='fi' AND parent_id IS NULL ORDER BY sort_order, id"
    ).fetchall():
        _move_raw(db, node_id, phase_node(db, "N"))
        moved["fi"] += 1
    for node_id, phases in db.execute(
        """SELECT n.id, f.phases FROM connection_nodes n JOIN fuses f ON f.id=n.fuse_id
           WHERE n.node_type='fuse' AND n.parent_id IS NULL ORDER BY n.sort_order, n.id"""
    ).fetchall():
        if phases in LINE_PHASES:
            _move_raw(db, node_id, phase_node(db, phases))
            moved["fuse"] += 1
    return moved


def _neutral_source(db: sqlite3.Connection, terminal_id: int) -> tuple[str, int | None]:
    """Where the N of a blue Klemme belongs, from the L Klemmen of the same cable (location + Klemmen number).

    Returns ('fi', fi_id), ('n', None) when their Sicherungen have no FI, ('conflict', None) for several
    FIs or a mix, and ('none', None) when no L Klemme of that number has a Sicherung.
    """
    rows = db.execute(
        f"""SELECT DISTINCT tf.fuse_id, ff.fi_id FROM terminals b
            JOIN terminals s ON s.location_id=b.location_id AND s.terminal_number=b.terminal_number AND s.id<>b.id
            JOIN terminal_fuse tf ON tf.terminal_id=s.id LEFT JOIN fuse_fi ff ON ff.fuse_id=tf.fuse_id
            WHERE b.id=? AND b.terminal_number<>'' AND s.wire_color NOT IN ('{NEUTRAL_COLOR}', '{PE_COLOR}', '')""",
        (terminal_id,),
    ).fetchall()
    fis = {fi_id for _, fi_id in rows}
    if not rows:
        return "none", None
    if fis == {None}:
        return "n", None
    if len(fis) == 1:
        return "fi", next(iter(fis))
    return "conflict", None


def _attach_terminal(db: sqlite3.Connection, terminal_id: int, parent_id: int | None) -> bool:
    """Hang a Klemme directly below `parent_id`; returns True when something changed.

    Nodes of the Klemme at a conductor place (root, Sicherung, phase, FI) move there with their followers;
    several of them are merged into one. Nodes inside a Kanal path stay where they are.
    """
    nodes = _rows(
        db,
        """SELECT n.id, n.parent_id, p.node_type AS parent_type FROM connection_nodes n
           LEFT JOIN connection_nodes p ON p.id=n.parent_id
           WHERE n.node_type='terminal' AND n.terminal_id=? ORDER BY n.id""",
        (terminal_id,),
    )
    loose = [node for node in nodes if node["parent_type"] in (None, "fuse", "phase", "fi")]
    target = next((node["id"] for node in nodes if node["parent_id"] == parent_id), None)
    changed = False
    if target is None:
        if loose:
            target = loose[0]["id"]
            _move_raw(db, target, parent_id)
        else:
            target = _insert_node(db, parent_id, "terminal", {"terminal_id": terminal_id})
        changed = True
    for node in loose:
        if node["id"] != target:
            # Followers join the kept node; exact duplicates disappear with the merged node.
            db.execute("UPDATE OR IGNORE connection_nodes SET parent_id=? WHERE parent_id=?", (target, node["id"]))
            db.execute("DELETE FROM connection_nodes WHERE id=?", (node["id"],))
            changed = True
    return changed


def conductor_options(db: sqlite3.Connection, terminal_id: int) -> list[dict[str, str]]:
    """Conductors a Klemme can be connected to by hand: N, an FI's neutral or PE, whatever its wire colour
    (the electrician may have used any core).

    A Klemme wired on behind another Klemme gets none (the path decides its conductor), nor does a Klemme
    with a Sicherung (it is an L).
    """
    if not db.execute("SELECT 1 FROM terminals WHERE id=?", (terminal_id,)).fetchone() or terminal_id in following_terminals(db):
        return []
    if db.execute("SELECT 1 FROM terminal_fuse_links WHERE terminal_id=?", (terminal_id,)).fetchone():
        return []
    return neutral_options(db) + [{"value": "PE", "label": "PE", "subtitle": "Schutzleiter"}]


def neutral_options(db: sqlite3.Connection) -> list[dict[str, str]]:
    """The neutrals to choose from by hand: the main N and the neutral of every numbered FI (N4 for FI 4)."""
    fis = db.execute("SELECT id, number, label FROM fi_devices ORDER BY CAST(number AS INTEGER), number, label").fetchall()
    return [{"value": "N", "label": "N", "subtitle": "Neutralleiter ohne FI"}] + [
        {"value": f"fi:{fi_id}", "label": neutral_code(number), "subtitle": " · ".join(filter(None, (f"FI {number}" if number else "", label)))}
        for fi_id, number, label in fis if number]


def connect_conductor(db: sqlite3.Connection, terminal_id: int, value: str) -> str:
    """Connect a Klemme by hand to N, the neutral of an FI ("fi:<id>") or PE.

    Its loose nodes (root, Sicherung, conductor) move there; `finish` checks the connection rules.
    Returns the conductor code it carries afterwards (N, N4, PE).
    """
    if value not in {option["value"] for option in conductor_options(db, terminal_id)}:
        raise ConnectionTreeError("This Klemme cannot be connected to that conductor.")
    if value.startswith("fi:"):
        fi_id = int(value.removeprefix("fi:"))
        # An FI without Sicherungen may not be in the tree yet; it is fed from N.
        parent = _find_node(db, None, "fi", {"fi_id": fi_id}) or _insert_node(db, phase_node(db, "N"), "fi", {"fi_id": fi_id})
    else:
        parent = phase_node(db, value)
    _attach_terminal(db, terminal_id, parent)
    finish(db)
    return terminal_wire_phase(db, terminal_id)


def assign_wire_phases(db: sqlite3.Connection) -> dict[str, Any]:
    """Connect conductors (idempotent): FI/Sicherungen to their start node, PE and N Klemmen to theirs.

    A blue Klemme goes below the FI of its cable (that FI's neutral) or below N when that cable has no FI.
    Unclear ones are never guessed: they only leave a Sicherung (a neutral is not fused) and stay for review.
    A Klemme wired on behind another Klemme carries that Klemme's conductor and is left alone.
    """
    report: dict[str, Any] = {"placed": place_roots(db), "pe": [], "n": [], "fi": {}, "unresolved": [], "unfused": []}
    following = following_terminals(db)
    # The PE start node only exists when there are green-yellow Klemmen.
    pe_terminals = [row for row in db.execute("SELECT id, label FROM terminals WHERE wire_color=? ORDER BY id", (PE_COLOR,))
                    if row[0] not in following]
    pe_node = phase_node(db, "PE") if pe_terminals else None
    for terminal_id, label in pe_terminals:
        if _attach_terminal(db, terminal_id, pe_node):
            report["pe"].append(label)
    for terminal_id, label in db.execute(
        "SELECT id, label FROM terminals WHERE wire_color=? ORDER BY id", (NEUTRAL_COLOR,)
    ).fetchall():
        if terminal_id in following:
            continue
        kind, fi_id = _neutral_source(db, terminal_id)
        if kind == "fi":
            fi_number = db.execute("SELECT number FROM fi_devices WHERE id=?", (fi_id,)).fetchone()[0]
            if _attach_terminal(db, terminal_id, _find_node(db, None, "fi", {"fi_id": fi_id})):
                report["fi"].setdefault(neutral_code(fi_number), []).append(label)
        elif kind == "n":
            if _attach_terminal(db, terminal_id, phase_node(db, "N")):
                report["n"].append(label)
        else:
            report["unresolved"].append(label)
    # A neutral or protective conductor is never fused: whatever still lies on a Sicherung's path leaves it.
    for terminal_id, label in db.execute(
        f"""SELECT DISTINCT t.id, t.label FROM terminal_fuse_links l JOIN terminals t ON t.id=l.terminal_id
            WHERE t.wire_color IN ('{NEUTRAL_COLOR}', '{PE_COLOR}') ORDER BY t.id"""
    ).fetchall():
        if terminal_id not in following:
            _unfuse_terminal(db, terminal_id)
            report["unfused"].append(label)
    return report


def following_terminals(db: sqlite3.Connection) -> set[int]:
    """Klemmen wired on behind another Klemme (a node of theirs hangs below a Klemme node)."""
    return {row[0] for row in db.execute(
        """SELECT DISTINCT n.terminal_id FROM connection_nodes n JOIN connection_nodes p ON p.id=n.parent_id
           WHERE n.node_type='terminal' AND p.node_type='terminal'""")}


def follower_rows(db: sqlite3.Connection) -> list[tuple[int, int]]:
    """Every Klemme -> following Klemme wire as (Klemme above, Klemme below), each once, oldest first."""
    return [tuple(row) for row in db.execute(
        """SELECT p.terminal_id, n.terminal_id FROM connection_nodes n JOIN connection_nodes p ON p.id=n.parent_id
           WHERE n.node_type='terminal' AND p.node_type='terminal' GROUP BY p.terminal_id, n.terminal_id ORDER BY MIN(n.id)""")]


def _unfuse_terminal(db: sqlite3.Connection, terminal_id: int) -> None:
    """Take a Klemme off every Sicherung path: its nodes there become roots (followers move along).

    Nodes wired on behind another Klemme stay: there the path decides the conductor.
    """
    for (node_id,) in db.execute(
        """SELECT DISTINCT n.id FROM connection_nodes n JOIN connection_paths path ON path.node_id=n.id
           JOIN connection_nodes fuse ON fuse.id=path.other_id AND fuse.node_type='fuse'
           LEFT JOIN connection_nodes p ON p.id=n.parent_id
           WHERE n.node_type='terminal' AND n.terminal_id=? AND COALESCE(p.node_type, '')<>'terminal' ORDER BY n.id""",
        (terminal_id,)
    ).fetchall():
        # Nodes below the Klemme on the same path (e.g. a Sicherung under it) stay with it; only the wire above is cut.
        if db.execute("SELECT 1 FROM connection_nodes WHERE id=? AND parent_id IS NOT NULL", (node_id,)).fetchone():
            _terminal_node_to_root(db, node_id, terminal_id)


def _terminal_node_to_root(db: sqlite3.Connection, node_id: int, terminal_id: int) -> None:
    """Cut the wire above a Klemme node: it becomes a root with what hangs below it.

    When the Klemme already has a root node, the followers join that one instead.
    """
    root = _find_node(db, None, "terminal", {"terminal_id": terminal_id})
    if root is None:
        _move_raw(db, node_id, None)
        return
    db.execute("UPDATE OR IGNORE connection_nodes SET parent_id=? WHERE parent_id=?", (root, node_id))
    db.execute("DELETE FROM connection_nodes WHERE id=?", (node_id,))


def disconnect_conductor(db: sqlite3.Connection, terminal_id: int) -> None:
    """Take a Klemme off its conductor (N, an FI's neutral or PE) again.

    Only Klemmen that can be connected by hand (`conductor_options`) are allowed; the Klemme stays as a chain of
    its own and shows up in the Prüfliste.
    """
    if not conductor_options(db, terminal_id):
        raise ConnectionTreeError("This Klemme is not connected to a conductor by hand.")
    for (node_id,) in db.execute(
        """SELECT n.id FROM connection_nodes n JOIN connection_nodes p ON p.id=n.parent_id
           WHERE n.node_type='terminal' AND n.terminal_id=? AND p.node_type IN ('phase', 'fi') ORDER BY n.id""",
        (terminal_id,),
    ).fetchall():
        _terminal_node_to_root(db, node_id, terminal_id)
    finish(db)


def _drop_unfed_loads(db: sqlite3.Connection, load_id: int | None = None) -> int:
    """Remove Verbraucher that no Klemme, Kanal or Sicherung feeds any more (all, or just `load_id`); returns how many.

    A direct N or PE link alone does not keep a Verbraucher: it goes with it.
    """
    fed = f"""SELECT ld.load_id FROM connection_nodes ld JOIN connection_nodes p ON p.id=ld.parent_id
              WHERE ld.node_type='load' AND p.node_type NOT IN ('phase', 'fi')"""
    scope = "" if load_id is None else f" AND load_id={int(load_id)}"
    db.execute(f"DELETE FROM connection_nodes WHERE node_type='load' AND load_id NOT IN ({fed}){scope}")
    return db.execute(f"DELETE FROM loads WHERE id NOT IN ({fed}){scope.replace('load_id', 'id')}").rowcount


def direct_conductor_links(db: sqlite3.Connection) -> dict[int, list[dict[str, Any]]]:
    """Verbraucher connected directly (without a Klemme) to N, an FI's neutral or PE: load id -> its links, each
    with the load node and the conductor code (N, N4, PE)."""
    links: dict[int, list[dict[str, Any]]] = {}
    for node_id, load_id, code, fi_number in db.execute(
        """SELECT ld.id, ld.load_id, w.code, fi.number FROM connection_nodes ld JOIN connection_nodes p ON p.id=ld.parent_id
           LEFT JOIN wire_phases w ON w.id=p.wire_phase_id LEFT JOIN fi_devices fi ON fi.id=p.fi_id
           WHERE ld.node_type='load' AND p.node_type IN ('phase', 'fi') ORDER BY ld.id"""):
        links.setdefault(load_id, []).append({"node_id": node_id, "code": code or neutral_code(fi_number)})
    return links


def direct_fuse_links(db: sqlite3.Connection) -> dict[int, list[dict[str, Any]]]:
    """Verbraucher hanging directly on a Sicherung (their L without a Klemme): load id -> its links."""
    links: dict[int, list[dict[str, Any]]] = {}
    for row in _rows(db, """SELECT ld.id AS node_id, ld.load_id, f.id AS fuse_id, f.label, f.phases, l.name AS location
                            FROM connection_nodes ld JOIN connection_nodes p ON p.id=ld.parent_id AND p.node_type='fuse'
                            JOIN fuses f ON f.id=p.fuse_id JOIN locations l ON l.id=f.location_id
                            WHERE ld.node_type='load' ORDER BY ld.id"""):
        links.setdefault(row["load_id"], []).append(row)
    return links


def connect_load_to_fuse(db: sqlite3.Connection, fuse_id: int, load_id: int | None = None, name: str = "") -> dict[str, Any]:
    """Hang a Verbraucher (existing, or new by name) directly on a Sicherung: its L comes from the Sicherung.

    A new Verbraucher gets the room of the Sicherung (no cable number). Returns the Verbraucher id and whether it
    is new / was linked.
    """
    fuse = db.execute("SELECT location_id FROM fuses WHERE id=?", (fuse_id,)).fetchone()
    if fuse is None:
        raise ConnectionTreeError("Sicherung not found.")
    created = False
    if load_id is None:
        name = " ".join(name.split())
        if not name:
            raise ConnectionTreeError("Choose an existing Verbraucher or enter a name for a new one.")
        created = db.execute("INSERT OR IGNORE INTO loads(location_id, terminal_number, name) VALUES (?, '', ?)",
                             (fuse[0], name)).rowcount > 0
        load_id = db.execute("SELECT id FROM loads WHERE location_id=? AND terminal_number='' AND name=?",
                             (fuse[0], name)).fetchone()[0]
    elif not db.execute("SELECT 1 FROM loads WHERE id=?", (load_id,)).fetchone():
        raise ConnectionTreeError("Verbraucher not found.")
    parent = ensure_fuse_node(db, fuse_id)
    linked = _find_node(db, parent, "load", {"load_id": load_id}) is None
    if linked:
        _insert_node(db, parent, "load", {"load_id": load_id})
    finish(db)
    return {"load_id": load_id, "created": created, "already_exists": not linked}


def connect_load_to_conductor(db: sqlite3.Connection, load_id: int, value: str) -> dict[str, Any]:
    """Connect a Verbraucher directly (no Klemme in between) to N, the neutral of an FI ("fi:<id>") or PE.

    Which neutral is never guessed: the caller names it. Returns the conductor code it is on (N, N4, PE).
    """
    if not db.execute("SELECT 1 FROM loads WHERE id=?", (load_id,)).fetchone():
        raise ConnectionTreeError("Verbraucher not found.")
    if value.startswith("fi:"):
        fi_id = int(value.removeprefix("fi:"))
        if not db.execute("SELECT 1 FROM fi_devices WHERE id=?", (fi_id,)).fetchone():
            raise ConnectionTreeError("FI not found.")
        # An FI without Sicherungen may not be in the tree yet; it is fed from N.
        parent = _find_node(db, None, "fi", {"fi_id": fi_id}) or _insert_node(db, phase_node(db, "N"), "fi", {"fi_id": fi_id})
    elif value in ("N", "PE"):
        parent = phase_node(db, value)
    else:
        raise ConnectionTreeError("Choose N, the neutral of an FI or PE.")
    linked = _find_node(db, parent, "load", {"load_id": load_id}) is None
    if linked:
        _insert_node(db, parent, "load", {"load_id": load_id})
    finish(db)
    code = next(link["code"] for link in direct_conductor_links(db)[load_id] if _node(db, link["node_id"])["parent_id"] == parent)
    return {"load_id": load_id, "created": False, "already_exists": not linked, "conductor": code}


def delete_terminal(db: sqlite3.Connection, terminal_id: int) -> None:
    """Delete a Klemme with its nodes.

    Klemmen wired on behind it start a chain of their own (without the Klemme above they carry no conductor);
    Kanäle below it move up to its place where the rules allow it. Verbraucher that only it fed are removed.
    """
    if not db.execute("SELECT 1 FROM terminals WHERE id=?", (terminal_id,)).fetchone():
        raise ConnectionTreeError("Klemme not found.")
    # Only the Verbraucher this Klemme fed may go with it (others without a Klemme stay for review).
    fed = {link["load_id"] for link in _load_links(db) if link["terminal_id"] == terminal_id}
    for node_id, below_id in db.execute(
        """SELECT n.id, n.terminal_id FROM connection_nodes n JOIN connection_nodes p ON p.id=n.parent_id
           WHERE n.node_type='terminal' AND p.node_type='terminal' AND p.terminal_id=? ORDER BY n.id""",
        (terminal_id,),
    ).fetchall():
        _terminal_node_to_root(db, node_id, below_id)
    # The nodes go with the Klemme (foreign key); the splice trigger keeps what hangs below them.
    db.execute("DELETE FROM terminals WHERE id=?", (terminal_id,))
    for load_id in fed:
        _drop_unfed_loads(db, load_id)
    finish(db)


def delete_load(db: sqlite3.Connection, load_id: int) -> None:
    """Delete a Verbraucher with all its links."""
    if not db.execute("DELETE FROM loads WHERE id=?", (load_id,)).rowcount:
        raise ConnectionTreeError("Verbraucher not found.")
    finish(db)


def _load_name(description: str | None) -> str:
    """Name of a Verbraucher: the first line of the Klemme description."""
    return next((line.strip() for line in (description or "").splitlines() if line.strip()), "")


def _chain_end(db: sqlite3.Connection, terminal_id: int) -> int:
    """Last Klemme of the Klemmen wired on behind a Klemme (itself when nothing follows).

    A Klemme followed by several Klemmen is a branch point; the chain ends there.
    """
    current, seen = terminal_id, {terminal_id}
    while True:
        followers = [row[0] for row in db.execute(
            """SELECT DISTINCT n.terminal_id FROM connection_nodes n JOIN connection_nodes p ON p.id=n.parent_id
               WHERE n.node_type='terminal' AND p.node_type='terminal' AND p.terminal_id=?""", (current,))]
        if len(followers) != 1 or followers[0] in seen:
            return current
        current = followers[0]
        seen.add(current)


def _load_anchor(db: sqlite3.Connection, terminal_id: int, at_end: bool = True) -> int:
    """Node a Verbraucher hangs on: the oldest node of the last Klemme of the chain (created when missing); of the
    Klemme itself when not `at_end`."""
    end = _chain_end(db, terminal_id) if at_end else terminal_id
    anchor = db.execute("SELECT MIN(id) FROM connection_nodes WHERE node_type='terminal' AND terminal_id=?",
                        (end,)).fetchone()[0]
    return anchor if anchor is not None else _insert_node(db, None, "terminal", {"terminal_id": end})


def _load_linked(db: sqlite3.Connection, terminal_id: int, load_id: int) -> bool:
    """Whether the Klemme feeds the Verbraucher: directly or through a Gerät-Kanal behind it (nearest Klemme)."""
    return db.execute(
        f"""SELECT 1 FROM connection_nodes ld JOIN connection_nodes p ON p.id=ld.parent_id
            WHERE ld.node_type='load' AND ld.load_id=? AND {LOAD_FEEDER_SQL}=?""",
        (load_id, terminal_id),
    ).fetchone() is not None


def _link_load(db: sqlite3.Connection, terminal_id: int, load_id: int, fixed: bool = False) -> bool:
    """Hang the Verbraucher behind the Klemme (after the last Klemme wired on); False when already linked there.

    `fixed`: the user chose this very Klemme (16/o as N, although 31/mi is wired on behind it): the link stays
    there, marked `FIXED_LOAD_NOTE`, and is not moved to the end of the chain.
    """
    end = _chain_end(db, terminal_id)
    at_end = not fixed or end == terminal_id
    target = end if at_end else terminal_id
    # A link fixed at the Klemme itself counts too.
    fixed_here = db.execute(
        """SELECT 1 FROM connection_nodes ld JOIN connection_nodes p ON p.id=ld.parent_id
           WHERE ld.node_type='load' AND ld.load_id=? AND ld.note=? AND p.node_type='terminal' AND p.terminal_id=?""",
        (load_id, FIXED_LOAD_NOTE, terminal_id)).fetchone()
    if fixed_here or _load_linked(db, target, load_id):
        return False
    _insert_node(db, _load_anchor(db, terminal_id, at_end), "load", {"load_id": load_id, "note": "" if at_end else FIXED_LOAD_NOTE})
    return True


def _move_loads_to_chain_ends(db: sqlite3.Connection) -> int:
    """Verbraucher are the end point after the last Klemme: move those hanging before a following Klemme; returns the count.

    A Verbraucher placed on a Gerät-Kanal stays there, and so does one the user fixed to its Klemme (`_link_load`).
    """
    moved = 0
    for node_id, terminal_id, load_id in db.execute(
        """SELECT ld.id, tn.terminal_id, ld.load_id FROM connection_nodes ld
           JOIN connection_nodes tn ON tn.id=ld.parent_id WHERE ld.node_type='load' AND tn.node_type='terminal'
           AND ld.note<>?
           -- A Verbraucher with Klemmen behind its output stays where it is (they would lose their place).
           AND NOT EXISTS (SELECT 1 FROM connection_nodes behind WHERE behind.parent_id=ld.id) ORDER BY ld.id""", (FIXED_LOAD_NOTE,)
    ).fetchall():
        if _chain_end(db, terminal_id) == terminal_id:
            continue
        # Link at the end (no-op when another Klemme of the cable did already), then drop the early node.
        _link_load(db, terminal_id, load_id)
        db.execute("DELETE FROM connection_nodes WHERE id=?", (node_id,))
        moved += 1
    return moved


def _channel_feeder(db: sqlite3.Connection, node_id: int) -> int:
    """The nearest Klemme above a Gerät-Kanal node (it feeds what hangs on the Kanal)."""
    node = _node(db, node_id)
    if node["node_type"] != "channel":
        raise ConnectionTreeError("A Verbraucher can only be connected to a Klemme or a Gerät-Kanal.")
    row = db.execute(f"SELECT ({NEAREST_TERMINAL_SQL}) FROM connection_nodes p WHERE p.id=?", (node_id,)).fetchone()
    row = row if row and row[0] is not None else None
    if row is None:
        raise ConnectionTreeError("A Verbraucher below a Gerät-Kanal needs a Klemme above that Kanal.")
    return int(row[0])


def connect_load(db: sqlite3.Connection, terminal_id: int | None, load_id: int | None = None, name: str = "",
                 node_id: int | None = None, fixed: bool = False) -> dict[str, Any]:
    """Connect a Klemme to a Verbraucher: an existing one (`load_id`) or one created by name.

    A new Verbraucher gets the location and Klemmen number of the Klemme (its cable). The link is a `load`
    node behind the last Klemme wired on behind it (`fixed`: at this very Klemme), or below the Gerät-Kanal
    `node_id`: then the Klemme above that Kanal feeds it, and an existing link of that Klemme to the same
    Verbraucher moves to the Kanal. Returns the Verbraucher id and whether it is new / was linked.
    """
    if node_id is not None:
        terminal_id = _channel_feeder(db, node_id)
    terminal = db.execute("SELECT location_id, terminal_number FROM terminals WHERE id=?", (terminal_id,)).fetchone()
    if terminal is None:
        raise ConnectionTreeError("Klemme not found.")
    created = False
    if load_id is not None:
        if not db.execute("SELECT 1 FROM loads WHERE id=?", (load_id,)).fetchone():
            raise ConnectionTreeError("Verbraucher not found.")
    else:
        name = " ".join(name.split())
        if not name:
            raise ConnectionTreeError("Choose an existing Verbraucher or enter a name for a new one.")
        created = db.execute("INSERT OR IGNORE INTO loads(location_id, terminal_number, name) VALUES (?,?,?)",
                             (terminal[0], terminal[1] or "", name)).rowcount > 0
        load_id = db.execute("SELECT id FROM loads WHERE location_id=? AND terminal_number=? AND name=?",
                             (terminal[0], terminal[1] or "", name)).fetchone()[0]
    if node_id is None:
        linked = _link_load(db, terminal_id, load_id, fixed)
    else:
        linked = _find_node(db, node_id, "load", {"load_id": load_id}) is None
        if linked:
            # One L feed per Klemme: the link moves from the Klemme (or another of its Kanäle) to this Kanal.
            db.execute(f"DELETE FROM connection_nodes WHERE id IN (SELECT id FROM ({LOAD_LINKS_SQL}) WHERE load_id=? AND terminal_id=?)",
                       (load_id, terminal_id))
            _insert_node(db, node_id, "load", {"load_id": load_id})
    finish(db)
    return {"load_id": load_id, "created": created, "already_exists": not linked}


def connect_load_feed(db: sqlite3.Connection, terminal_id: int, load_id: int, role: str) -> dict[str, Any]:
    """Connect a missing L, N or PE feed with a Klemme. Its connection decides, never its wire colour:

    a Klemme already on a conductor must carry the role (N/Nx for N, PE for PE, a Sicherung for L);
    an unconnected one becomes the PE feed at PE (there is only one PE). N or an FI's neutral is chosen by hand.
    """
    if not db.execute("SELECT 1 FROM terminals WHERE id=?", (terminal_id,)).fetchone():
        raise ConnectionTreeError("Klemme not found.")
    code = terminal_wire_phase(db, terminal_id)
    if code and effective_role(code, "") != role:
        raise ConnectionTreeError(f"This Klemme is on {code}; it cannot be the {role} feed.")
    if role == "PE" and not code and conductor_options(db, terminal_id):
        connect_conductor(db, terminal_id, "PE")
    # The user chose this Klemme: the Verbraucher hangs on it, also when further Klemmen are wired on behind it.
    return {**connect_load(db, terminal_id, load_id, fixed=True), "conductor": terminal_wire_phase(db, terminal_id)}


def _load_links(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every Verbraucher node: id, load_id, parent (type, device, channel key) and the feeding Klemme."""
    return _rows(db, f"{LOAD_LINKS_SQL} ORDER BY ld.id")


def _load_rows(db: sqlite3.Connection) -> list[tuple[int, int]]:
    """All Verbraucher links as (feeding Klemme, Verbraucher), each once."""
    return list(dict.fromkeys((link["terminal_id"], link["load_id"]) for link in _load_links(db)
                              if link["terminal_id"] is not None))


def assign_loads(db: sqlite3.Connection) -> dict[str, Any]:
    """Create the Verbraucher of every cable (idempotent) and hang them behind their L, N and PE Klemmen.

    A Verbraucher is the end point after the last Klemme: behind Klemmen wired on it hangs on the last of them.

    A cable is a location + Klemmen number. One L description: one Verbraucher fed by all its Klemmen.
    Several L descriptions: one Verbraucher per L; that cable's N and PE stay unlinked for review.
    """
    report: dict[str, Any] = {"created": [], "linked": 0, "moved": 0, "removed": 0}
    cables: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for terminal in _rows(
        db, "SELECT id, location_id, terminal_number, wire_color, description FROM terminals WHERE terminal_number<>'' ORDER BY id"
    ):
        cables.setdefault((terminal["location_id"], terminal["terminal_number"]), []).append(terminal)
    # The conductor decides a Klemme's role (a black core on N is a neutral), else its colour.
    codes = wire_phase_map(db)
    role = lambda member: effective_role(codes.get(member["id"]), member["wire_color"])
    for (location_id, number), members in cables.items():
        lines = [member for member in members if role(member) == "L"]
        names = list(dict.fromkeys(name for member in lines if (name := _load_name(member["description"]))))
        shared = [member for member in members if role(member) in ("N", "PE")] if len(names) == 1 else []
        for name in names:
            created = db.execute(
                "INSERT OR IGNORE INTO loads(location_id, terminal_number, name) VALUES (?,?,?)", (location_id, number, name)
            ).rowcount
            load_id = db.execute("SELECT id FROM loads WHERE location_id=? AND terminal_number=? AND name=?",
                                 (location_id, number, name)).fetchone()[0]
            if created:
                report["created"].append(name)
            feeders = [member for member in lines if _load_name(member["description"]) == name] + shared
            report["linked"] += sum(_link_load(db, member["id"], load_id) for member in feeders)
    report["moved"] = _move_loads_to_chain_ends(db)
    # A Verbraucher that no Klemme feeds any more is gone.
    report["removed"] = _drop_unfed_loads(db)
    return report


def wire_phase_map(db: sqlite3.Connection) -> dict[int, str]:
    """Conductor of every connected Klemme: 'PE', 'N', 'N4' (neutral of FI 4) or the phases of its Sicherung."""
    codes: dict[int, str] = {}
    for terminal_id, code, fi_number in db.execute(
        """SELECT n.terminal_id, w.code, fi.number FROM connection_nodes n JOIN connection_nodes p ON p.id=n.parent_id
           LEFT JOIN wire_phases w ON w.id=p.wire_phase_id LEFT JOIN fi_devices fi ON fi.id=p.fi_id
           WHERE n.node_type='terminal' AND p.node_type IN ('phase', 'fi') ORDER BY n.id"""
    ):
        codes.setdefault(terminal_id, code if code else neutral_code(fi_number))
    for terminal_id, phases in db.execute(
        "SELECT tf.terminal_id, f.phases FROM terminal_fuse tf JOIN fuses f ON f.id=tf.fuse_id"
    ):
        codes.setdefault(terminal_id, phases.replace(",", "/") or "L")
    # A Klemme wired on behind another one carries its conductor; follow such chains until nothing changes.
    followers = follower_rows(db)
    changed = True
    while changed:
        changed = False
        for above, below in followers:
            if above in codes and below not in codes:
                codes[below] = codes[above]
                changed = True
    return codes


def terminal_loads(db: sqlite3.Connection, terminal_ids: list[int] | None = None,
                   fuse_ids: list[int] | None = None, load_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """Verbraucher hanging behind the given Klemmen, directly on the given Sicherungen or with the given ids (all of
    them when none is given), each with all Klemmen that feed it and their conductor, plus its direct feeds
    (Sicherung, N, PE)."""
    codes = wire_phase_map(db)
    links = _load_rows(db)
    # The Verbraucher node of each Klemme feed: changing or cutting it touches only this Verbraucher.
    feed_nodes: dict[tuple[int, int], int] = {}
    for link in _load_links(db):
        if link["terminal_id"] is not None:
            feed_nodes.setdefault((link["terminal_id"], link["load_id"]), link["id"])
    direct = direct_conductor_links(db)
    on_fuses = direct_fuse_links(db)
    everything = terminal_ids is None and fuse_ids is None and load_ids is None
    terminal_ids = list(terminal_ids) if terminal_ids is not None else [terminal_id for terminal_id, _ in links] if everything else []
    fuse_ids = set(fuse_ids or [])
    wanted = {load_id for terminal_id, load_id in links if terminal_id in set(terminal_ids)}
    wanted |= {load_id for load_id, rows in on_fuses.items() if everything or any(row["fuse_id"] in fuse_ids for row in rows)}
    # Also those connected only directly to N or PE (no Klemme, no Sicherung).
    wanted |= set(direct) if everything else set()
    wanted |= {row[0] for row in db.execute("SELECT id FROM loads")} & set(load_ids or [])
    if not wanted:
        return []
    terminals = {row["id"]: row for row in _rows(
        db, "SELECT t.id, t.label, t.wire_color, l.name AS location FROM terminals t JOIN locations l ON l.id=t.location_id")}
    loads = {row["id"]: row for row in _rows(
        db, f"""SELECT ld.id, ld.name, ld.description, ld.terminal_number, {_no_pe_sql(db)} AS no_pe, l.name AS location
                FROM loads ld JOIN locations l ON l.id=ld.location_id""")}
    result = []
    for load_id in sorted(wanted, key=lambda key: natural_key(loads[key]["name"])):
        feeds = []
        for terminal_id, linked in links:
            if linked != load_id:
                continue
            terminal = terminals[terminal_id]
            code = codes.get(terminal_id, "")
            feeds.append({"terminal_id": terminal_id, "node_id": feed_nodes.get((terminal_id, load_id)),
                          "label": terminal["label"], "location": terminal["location"],
                          "wire_color": terminal["wire_color"], "wire_phase": code,
                          "role": effective_role(code, terminal["wire_color"])})
        # Hanging directly on a Sicherung: its L without Klemme (`node_id` is the link, for cutting it).
        for row in on_fuses.get(load_id, []):
            feeds.append({"terminal_id": None, "node_id": row["node_id"], "fuse_id": row["fuse_id"], "label": row["label"],
                          "location": row["location"], "wire_color": "", "wire_phase": (row["phases"] or "").replace(",", "/") or "L",
                          "role": "L", "direct": True})
        # Connected directly to N, an FI's neutral or PE: a feed without Klemme (`node_id` is the link, for cutting it).
        for link in direct.get(load_id, []):
            feeds.append({"terminal_id": None, "node_id": link["node_id"], "label": "direkt", "location": "",
                          "wire_color": PE_COLOR if link["code"] == "PE" else NEUTRAL_COLOR, "wire_phase": link["code"],
                          "role": effective_role(link["code"], ""), "direct": True})
        result.append({"id": load_id, "name": loads[load_id]["name"], "description": loads[load_id]["description"],
                       "location": loads[load_id]["location"],
                       "terminal_number": loads[load_id]["terminal_number"],
                       "no_pe": bool(loads[load_id]["no_pe"]),
                       "anchor_terminal_ids": sorted({feed["terminal_id"] for feed in feeds} & set(terminal_ids)),
                       "feeds": sorted(feeds, key=lambda feed: ("L", "N", "PE", "").index(feed["role"]))})
    return result


def disconnect_load(db: sqlite3.Connection, node_id: int) -> bool:
    """Cut one Verbraucher link (its `load` node): the Klemme or Kanal above no longer feeds it.

    A Verbraucher that nothing feeds any more is removed, as `assign_loads` does. Returns whether it was removed.
    """
    node = _node(db, node_id)
    if node["node_type"] != "load":
        raise ConnectionTreeError("Only a Verbraucher link can be cut here.")
    db.execute("DELETE FROM connection_nodes WHERE id=?", (node_id,))
    removed = _drop_unfed_loads(db, node["load_id"]) > 0
    finish(db)
    return removed


def terminal_wire_phase(db: sqlite3.Connection, terminal_id: int) -> str:
    """Conductor of one Klemme ('' when it is on none)."""
    return wire_phase_map(db).get(terminal_id, "")


def review_items(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Prüfliste, computed live: N/PE without conductor, 0-Schiene notes that disagree, Verbraucher without N or PE."""
    codes = wire_phase_map(db)
    following = following_terminals(db)
    fed_ends = {terminal_id for terminal_id, _ in _load_rows(db)}
    items: list[dict[str, Any]] = []

    def fed(terminal_id: int) -> bool:
        """A Klemme feeds a Verbraucher when one hangs behind the last Klemme wired on behind it."""
        return _chain_end(db, terminal_id) in fed_ends

    def add(kind: str, terminal: dict[str, Any], message: str) -> None:
        items.append({"kind": kind, "entity_type": "terminal", "entity_id": str(terminal["id"]),
                      "label": terminal["label"], "location": terminal["location"], "message": message})

    terminals = _rows(db, """SELECT t.id, t.label, t.wire_color, t.description, l.name AS location
                             FROM terminals t JOIN locations l ON l.id=t.location_id""")
    for terminal in terminals:
        code = codes.get(terminal["id"])
        role = effective_role(code, terminal["wire_color"])
        if role == "N" and code is None:
            kind, _ = _neutral_source(db, terminal["id"])
            add("neutral", terminal, "N nicht zugeordnet: die L-Klemmen dieses Kabels liegen an verschiedenen FI."
                if kind == "conflict" else "N nicht zugeordnet: keine L-Klemme dieses Kabels hat eine Sicherung.")
        if role == "N" and code and (bar := ZERO_BAR.search(terminal["description"] or "")) and code != f"N{bar.group(1)}":
            add("mismatch", terminal, f"Beschreibung nennt 0-Schiene {bar.group(1)}, angeschlossen an {code}.")
        if role == "PE" and code is None:
            add("pe", terminal, "PE nicht angeschlossen.")
        if role in ("N", "PE") and not fed(terminal["id"]) and terminal["id"] not in following:
            add("unfed", terminal, f"{role}-Klemme ohne Verbraucher.")
    # A Verbraucher needs its L, N and PE (no PE when it has no PE connection at all, `no_pe`).
    by_id = {terminal["id"]: terminal for terminal in terminals}
    feeds: dict[int, list[dict[str, Any]]] = {}
    for terminal_id, load_id in _load_rows(db):
        feeds.setdefault(load_id, []).append(by_id[terminal_id])
    names = dict(db.execute("SELECT id, name FROM loads").fetchall())
    without_pe = {row[0] for row in db.execute(f"SELECT ld.id FROM loads ld WHERE {_no_pe_sql(db)}=1")}
    direct = direct_conductor_links(db)
    on_fuses = direct_fuse_links(db)
    for load_id in set(feeds) | set(on_fuses):
        members = feeds.get(load_id, [])
        # A Verbraucher connected directly to N or PE has it without a Klemme.
        member_role = lambda member: effective_role(codes.get(member["id"]), member["wire_color"])
        roles = {member_role(member) for member in members} | {effective_role(link["code"], "") for link in direct.get(load_id, [])}
        missing = [role for role in ("N", "PE") if role not in roles and not (role == "PE" and load_id in without_pe)]
        if not missing:
            continue
        message = f"Verbraucher „{names[load_id]}“ ohne {' und '.join(missing)}."
        anchor = next((member for member in members if member_role(member) == "L"), members[0] if members else None)
        if anchor:
            add("load", anchor, message)
        else:
            # Fed only directly by a Sicherung: the entry opens that Sicherung.
            fuse = on_fuses[load_id][0]
            items.append({"kind": "load", "entity_type": "fuse", "entity_id": str(fuse["fuse_id"]),
                          "label": fuse["label"], "location": fuse["location"], "message": message})
    order = {"mismatch": 0, "neutral": 1, "pe": 2, "load": 3, "unfed": 4}
    return sorted(items, key=lambda item: (order[item["kind"]], natural_key(item["location"]), natural_key(item["label"])))


def phase_balance(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Number of Sicherungen per phase, per FI (and without FI)."""
    groups: dict[str, dict[str, int]] = {}
    for fi_number, phases in db.execute(
        "SELECT fi.number, f.phases FROM fuses f LEFT JOIN fuse_fi ff ON ff.fuse_id=f.id LEFT JOIN fi_devices fi ON fi.id=ff.fi_id"
    ):
        counts = groups.setdefault(f"FI {fi_number}" if fi_number else "ohne FI", dict.fromkeys(LINE_PHASES, 0))
        for phase in (phases or "").split(","):
            if phase in counts:
                counts[phase] += 1
    names = sorted(groups, key=lambda name: (name == "ohne FI", natural_key(name)))
    return [{"group": name, **groups[name]} for name in names]


# ---------------------------------------------------------------- Klemmen wired on behind a Klemme

def _terminal_anchor(db: sqlite3.Connection, terminal_id: int) -> int:
    """The oldest node of a Klemme (a root node is created when it has none)."""
    anchor = db.execute("SELECT MIN(id) FROM connection_nodes WHERE node_type='terminal' AND terminal_id=?",
                        (terminal_id,)).fetchone()[0]
    return anchor if anchor is not None else _insert_node(db, None, "terminal", {"terminal_id": terminal_id})


def _follows(db: sqlite3.Connection, above_id: int, below_id: int) -> bool:
    """Whether Klemme `below_id` is wired on behind Klemme `above_id` (below any of its nodes)."""
    return db.execute(
        """SELECT 1 FROM connection_nodes n JOIN connection_nodes p ON p.id=n.parent_id
           WHERE n.node_type='terminal' AND n.terminal_id=? AND p.node_type='terminal' AND p.terminal_id=?""",
        (below_id, above_id),
    ).fetchone() is not None


def _hang_behind(db: sqlite3.Connection, anchor: int, below_id: int) -> int:
    """Hang Klemme `below_id` below the Klemme node `anchor` without checks; returns its node there.

    A loose node of the lower Klemme moves there with what hangs below it, so nothing is duplicated: a root
    node, or one placed directly on a conductor (N, PE, FI) by colour, since behind a Klemme the path decides.
    Otherwise a new node is added.
    """
    existing = _find_node(db, anchor, "terminal", {"terminal_id": below_id})
    if existing is not None:
        return existing
    loose = db.execute(
        """SELECT n.id FROM connection_nodes n LEFT JOIN connection_nodes p ON p.id=n.parent_id
           WHERE n.node_type='terminal' AND n.terminal_id=? AND (p.id IS NULL OR p.node_type IN ('phase', 'fi'))
           ORDER BY p.id IS NOT NULL, n.id""", (below_id,)).fetchone()
    if loose and int(loose[0]) != anchor:
        _move_raw(db, int(loose[0]), anchor)
        return int(loose[0])
    return _insert_node(db, anchor, "terminal", {"terminal_id": below_id})


def add_follower(db: sqlite3.Connection, above_id: int, below_id: int) -> bool:
    """Wire Klemme `below_id` on behind Klemme `above_id` (at the end of the path); False when it already is."""
    if above_id == below_id:
        raise ConnectionTreeError("A Klemme cannot follow itself.")
    if _follows(db, above_id, below_id):
        return False
    _hang_behind(db, _terminal_anchor(db, above_id), below_id)
    # The Verbraucher stay the end point: those of the upper Klemme move behind the new one.
    _move_loads_to_chain_ends(db)
    finish(db)
    return True


def _keeps_followers(function):
    """Decorator: Klemmen wired on behind a Klemme stay there when the function rebuilds that Klemme's nodes.

    The tree is validated (`finish`) once they are back.
    """
    @functools.wraps(function)
    def wrapper(db: sqlite3.Connection, *args: Any, **kwargs: Any) -> Any:
        # Each following node with the Klemme above it.
        saved = db.execute(
            """SELECT n.id, p.terminal_id, n.terminal_id FROM connection_nodes n JOIN connection_nodes p ON p.id=n.parent_id
               WHERE n.node_type='terminal' AND p.node_type='terminal' ORDER BY n.id""").fetchall()
        result = function(db, *args, **kwargs)
        for node_id, above_id, below_id in saved:
            row = db.execute(
                "SELECT p.node_type, p.terminal_id FROM connection_nodes n LEFT JOIN connection_nodes p ON p.id=n.parent_id WHERE n.id=?",
                (node_id,)).fetchone()
            if row is None:
                # The node went away with the rebuild: wire the Klemme on again.
                if not _follows(db, above_id, below_id):
                    _hang_behind(db, _terminal_anchor(db, above_id), below_id)
            elif tuple(row) != ("terminal", above_id):
                # Deleting the upper Klemme spliced this node up to where that one was: it goes back behind it.
                # (`tuple`: a `sqlite3.Row` never equals a tuple, every follower would count as spliced.)
                anchor = _terminal_anchor(db, above_id)
                twin = _find_node(db, anchor, "terminal", {"terminal_id": below_id})
                if twin == node_id:
                    continue
                if twin is None:
                    _move_raw(db, node_id, anchor)
                else:
                    db.execute("UPDATE OR IGNORE connection_nodes SET parent_id=? WHERE parent_id=?", (twin, node_id))
                    db.execute("DELETE FROM connection_nodes WHERE id=?", (node_id,))
        finish(db)
        return result
    return wrapper


def _keeps_side_nodes(function):
    """Decorator: Verbraucher survive a rebuild of the Klemme nodes that carried them."""
    @functools.wraps(function)
    def wrapper(db: sqlite3.Connection, *args: Any, **kwargs: Any) -> Any:
        saved_loads = _load_rows(db)
        # Links the user fixed to a Klemme (`FIXED_LOAD_NOTE`) stay fixed there.
        fixed = {tuple(row) for row in db.execute(
            """SELECT p.terminal_id, ld.load_id FROM connection_nodes ld JOIN connection_nodes p ON p.id=ld.parent_id
               WHERE ld.node_type='load' AND ld.note=? AND p.node_type='terminal'""", (FIXED_LOAD_NOTE,)).fetchall()}
        on_channels = [link for link in _load_links(db) if link["parent_type"] == "channel"]
        # Klemmen behind a Verbraucher's output (UPS -> Steckdose): their node, Verbraucher, its feed and place.
        behind = _rows(db, f"""SELECT n.id, n.terminal_id, link.id AS load_node, link.load_id, link.terminal_id AS feeder, link.parent_id
            FROM connection_nodes n JOIN ({LOAD_LINKS_SQL}) link ON link.id=n.parent_id WHERE n.node_type='terminal' ORDER BY n.id""")
        result = function(db, *args, **kwargs)
        # A Kanal that lost its Klemme above cannot feed a Verbraucher any more.
        db.execute(f"DELETE FROM connection_nodes WHERE id IN (SELECT id FROM ({LOAD_LINKS_SQL}) WHERE parent_type='channel' AND terminal_id IS NULL)")
        # Verbraucher on a Gerät-Kanal go back onto the same Kanal behind the same Klemme, if it is still there.
        for link in on_channels:
            if _load_linked(db, link["terminal_id"], link["load_id"]):
                continue
            channel = db.execute(
                f"""SELECT p.id FROM connection_nodes p WHERE p.node_type='channel' AND p.device_id=? AND p.channel_key=?
                    AND ({NEAREST_TERMINAL_SQL})=? ORDER BY p.id LIMIT 1""",
                (link["device_id"], link["channel_key"], link["terminal_id"])).fetchone()
            if channel:
                _insert_node(db, channel[0], "load", {"load_id": link["load_id"]})
        # Everything else (and a Kanal that is gone) hangs on the Klemme again.
        for terminal_id, load_id in saved_loads:
            _link_load(db, terminal_id, load_id, (terminal_id, load_id) in fixed)
        if _restore_behind_loads(db, behind):
            finish(db)
        return result
    return wrapper


def _restore_behind_loads(db: sqlite3.Connection, behind: list[dict[str, Any]]) -> bool:
    """Klemmen that hung behind a Verbraucher's output go back there after a rebuild re-created its node; returns
    whether something moved.

    Only when the Verbraucher's node went away, and only a Klemme whose node went too or was spliced up to where
    the Verbraucher hung (or to the roots); one moved on purpose elsewhere stays where it is.
    """
    changed = False
    for item in behind:
        # The Verbraucher's node is still there: whatever happened to the Klemme was meant (e.g. its wire was cut).
        # Checked by its content too, since SQLite may give a new node the id of a deleted one.
        if db.execute("SELECT 1 FROM connection_nodes WHERE id=? AND node_type='load' AND load_id=? AND parent_id IS ?",
                      (item["load_node"], item["load_id"], item["parent_id"])).fetchone():
            continue
        # The Verbraucher's node now: behind the same Klemme (or on the same Sicherung/Kanal without one).
        anchor = db.execute(
            f"SELECT id FROM ({LOAD_LINKS_SQL}) WHERE load_id=? AND " + ("terminal_id=?" if item["feeder"] is not None else "parent_id=?")
            + " ORDER BY id LIMIT 1", (item["load_id"], item["feeder"] if item["feeder"] is not None else item["parent_id"])).fetchone()
        if anchor is None:
            continue
        anchor = int(anchor[0])
        node = db.execute("SELECT parent_id FROM connection_nodes WHERE id=?", (item["id"],)).fetchone()
        # Spliced up (to the Verbraucher's old place, or to the roots when that node went away too).
        if node is not None and (node[0] == anchor or node[0] not in (item["parent_id"], None)):
            continue
        twin = _find_node(db, anchor, "terminal", {"terminal_id": item["terminal_id"]})
        if node is None:
            if twin is None:
                _hang_behind(db, anchor, item["terminal_id"])
        elif twin is None:
            _move_raw(db, item["id"], anchor)
        else:
            db.execute("UPDATE OR IGNORE connection_nodes SET parent_id=? WHERE parent_id=?", (twin, item["id"]))
            db.execute("DELETE FROM connection_nodes WHERE id=?", (item["id"],))
        changed = True
    return changed


def rebuild_node_table(db: sqlite3.Connection) -> None:
    """Recreate `connection_nodes` with the current schema; node ids, order and references are kept.

    Used by the generation migration; every node must fit the current schema (no route nodes left).
    """
    old_columns = {row[1] for row in db.execute("PRAGMA table_info(connection_nodes)")}
    kept = ["id", "parent_id", "sort_order", "node_type", *(name for name in NODE_COLUMNS if name in old_columns)]
    old_nodes = db.execute(f"SELECT {', '.join(kept)} FROM connection_nodes ORDER BY id").fetchall()
    for (view,) in db.execute(
        """SELECT name FROM sqlite_master WHERE type='view' AND name IN ('channel_terminals', 'terminal_channel_links',
           'connection_channel_pairs', 'terminal_fuse', 'fuse_fi', 'terminal_fuse_links', 'connection_paths',
           'connection_ancestors', 'terminal_routes')"""
    ).fetchall():
        db.execute(f"DROP VIEW {view}")
    db.execute("DROP TRIGGER IF EXISTS connection_nodes_splice")
    db.execute("DROP TABLE connection_nodes")
    db.executescript(CONNECTION_SCHEMA + CONDUCTOR_INDEXES)
    db.execute("PRAGMA defer_foreign_keys=ON")
    db.executemany(
        f"INSERT INTO connection_nodes({', '.join(kept)}) VALUES ({','.join('?' * len(kept))})", old_nodes
    )


# ---------------------------------------------------------------- building chains

def ensure_fuse_node(db: sqlite3.Connection, fuse_id: int, fi_id: int | None = None) -> int:
    """Return the fuse's node; with an FI the fuse is placed below that FI."""
    node_id = _find_node(db, None, "fuse", {"fuse_id": fuse_id})
    if fi_id is None:
        return node_id or _insert_node(db, None, "fuse", {"fuse_id": fuse_id})
    # An FI is fed from N (FI and Sicherung are unique tree-wide, so an existing FI is found anywhere).
    fi_node = _find_node(db, None, "fi", {"fi_id": fi_id}) or _insert_node(
        db, phase_node(db, "N"), "fi", {"fi_id": fi_id})
    if node_id is None:
        return _insert_node(db, fi_node, "fuse", {"fuse_id": fuse_id})
    current = _node(db, node_id)
    if current["parent_id"] != fi_node:
        parent_type = _node(db, current["parent_id"])["node_type"] if current["parent_id"] else None
        if parent_type in ("channel", "terminal"):
            raise ConnectionTreeError("This Sicherung is placed below a Kanal or Klemme and cannot get an FI.")
        _move_raw(db, node_id, fi_node)
    return node_id


def ensure_terminal_chain(
    db: sqlite3.Connection, terminal_id: int, fuse_id: int | None, specs: list[dict[str, Any]]
) -> None:
    """Create [Sicherung ->] [Kanal ->] Klemme in the default order, merging shared prefixes."""
    parent = ensure_fuse_node(db, fuse_id) if fuse_id else None
    if specs:
        for spec in specs:
            carrier = _get_or_create(db, parent, "channel", spec)
            _get_or_create(db, carrier, "terminal", {"terminal_id": terminal_id})
    elif parent is not None:
        _get_or_create(db, parent, "terminal", {"terminal_id": terminal_id})


def set_fuse_fi(db: sqlite3.Connection, fuse_id: int, fi_id: int | None) -> None:
    """Place a Sicherung below an FI, or detach it (it then becomes a root)."""
    if fi_id is not None:
        ensure_fuse_node(db, fuse_id, fi_id)
    else:
        node_id = _find_node(db, None, "fuse", {"fuse_id": fuse_id})
        if node_id is not None:
            parent = _node(db, node_id)["parent_id"]
            if parent is not None and _node(db, parent)["node_type"] == "fi":
                _move_raw(db, node_id, None)
    finish(db)


@_keeps_side_nodes
@_keeps_followers
def set_terminal_fuse(db: sqlite3.Connection, terminal_id: int, fuse_id: int | None) -> None:
    """Connect a Klemme to a Sicherung (or none); an unchanged fuse keeps the user's order."""
    if fuse_id and never_fused(db, terminal_id):
        raise ConnectionTreeError("A Klemme on N, an FI's neutral or PE cannot be connected to a Sicherung.")
    wanted = {fuse_id} if fuse_id else set()
    if related_fuse_ids(db, terminal_id) != wanted:
        specs = terminal_channel_specs(db, terminal_id)
        carriers = [row[0] for row in db.execute(
            "SELECT DISTINCT channel_node_id FROM connection_channel_pairs WHERE terminal_id=?", (terminal_id,)
        )]
        db.execute("DELETE FROM connection_nodes WHERE node_type='terminal' AND terminal_id=?", (terminal_id,))
        for carrier in carriers:
            if not _has_children(db, carrier):
                db.execute("DELETE FROM connection_nodes WHERE id=?", (carrier,))
        ensure_terminal_chain(db, terminal_id, fuse_id, specs)
    # `finish` runs in `_keeps_followers`, once the Klemmen wired on behind this one are back.


def _existing_pair(db: sqlite3.Connection, terminal_id: int, spec: dict[str, Any]) -> int | None:
    """Id of the pair that already connects the Klemme with the (normalized) channel, if any."""
    row = db.execute(
        """SELECT pair.id FROM connection_channel_pairs pair JOIN connection_nodes channel ON channel.id=pair.channel_node_id
           WHERE pair.terminal_id=? AND channel.device_id=? AND channel.channel_key=?
             AND channel.channel_id IS ? AND channel.note=?""",
        (terminal_id, spec["device_id"], spec["channel_key"], spec["channel_id"], spec["note"]),
    ).fetchone()
    return int(row[0]) if row else None


def add_channel_link(db: sqlite3.Connection, terminal_id: int, spec: dict[str, Any]) -> int:
    """Connect a Klemme to a channel; returns the id of the pair (its deeper node)."""
    spec = _channel_spec(spec)
    fuses = related_fuse_ids(db, terminal_id)
    if len(fuses) > 1:
        raise ConnectionTreeError("This Klemme is connected to several Sicherungen.")
    parent = ensure_fuse_node(db, next(iter(fuses))) if fuses else None
    existing = _existing_pair(db, terminal_id, spec)
    if existing:
        return existing
    carrier = _get_or_create(db, parent, "channel", spec)
    bare = db.execute(
        f"""SELECT id FROM connection_nodes WHERE node_type='terminal' AND terminal_id=? AND parent_id IS ?
            AND NOT EXISTS (SELECT 1 FROM connection_nodes child
                            WHERE child.parent_id=connection_nodes.id AND {STRUCTURAL_CHILD_SQL})""",
        (terminal_id, parent),
    ).fetchone()
    if bare:
        _move_raw(db, int(bare[0]), carrier)
        pair_id = int(bare[0])
    else:
        pair_id = _get_or_create(db, carrier, "terminal", {"terminal_id": terminal_id})
    finish(db)
    return pair_id


def append_channel_link(db: sqlite3.Connection, terminal_id: int, spec: dict[str, Any]) -> int:
    """Connect a Klemme to a channel at the end of its path (the Kanal hangs below the Klemme).

    Where that would break the chain rules (the path already holds a Kanal) the default placement
    of `add_channel_link` is used. Returns the id of the pair (its deeper node).
    """
    spec = _channel_spec(spec)
    existing = _existing_pair(db, terminal_id, spec)
    if existing:
        return existing
    # A Klemme node without followers (Verbraucher do not count) whose path holds no Kanal yet.
    leaf = db.execute(
        f"""SELECT node.id FROM connection_nodes node
           WHERE node.node_type='terminal' AND node.terminal_id=?
             AND NOT EXISTS (SELECT 1 FROM connection_nodes child WHERE child.parent_id=node.id
                             AND {STRUCTURAL_CHILD_SQL})
             AND NOT EXISTS (SELECT 1 FROM connection_ancestors a JOIN connection_nodes p ON p.id=a.ancestor_id
                             WHERE a.node_id=node.id AND p.node_type='channel')
           ORDER BY node.id""",
        (terminal_id,),
    ).fetchone()
    if leaf is None:
        if db.execute("SELECT 1 FROM connection_nodes WHERE node_type='terminal' AND terminal_id=?", (terminal_id,)).fetchone():
            return add_channel_link(db, terminal_id, spec)
        leaf_id = _insert_node(db, None, "terminal", {"terminal_id": terminal_id})
    else:
        leaf_id = int(leaf[0])
    pair_id = _get_or_create(db, leaf_id, "channel", spec)
    finish(db)
    return pair_id


def fused_above(db: sqlite3.Connection, node_id: int) -> bool:
    """True when a Sicherung is above the node (or is the node): whatever hangs below it is fused."""
    return db.execute(
        """SELECT 1 FROM connection_nodes n WHERE n.id=? AND n.node_type='fuse'
           UNION SELECT 1 FROM connection_ancestors a JOIN connection_nodes p ON p.id=a.ancestor_id
           WHERE a.node_id=? AND p.node_type='fuse'""", (node_id, node_id)
    ).fetchone() is not None


def _conductor_return(db: sqlite3.Connection, channel_node_id: int, terminal_id: int) -> bool:
    """True when a Klemme on N/PE (by its connection, not its colour) below this Kanal would get the Sicherung above it."""
    return never_fused(db, terminal_id) and fused_above(db, channel_node_id)


def link_conductor_to_path(db: sqlite3.Connection, node_id: int, terminal_id: int) -> int:
    """A Klemme on N, an FI's neutral or PE put into a fused wire: it is the neutral/PE of that device and Verbraucher.

    It stays at its conductor; the nearest Kanal at or above the node hangs below it, and it feeds every
    Verbraucher below the node. Returns the Klemme's conductor node.
    """
    node = _node(db, node_id)
    channel = node_id if node["node_type"] == "channel" else next(
        (row[0] for row in db.execute(
            """SELECT p.id FROM connection_ancestors a JOIN connection_nodes p ON p.id=a.ancestor_id
               WHERE a.node_id=? AND p.node_type='channel'""", (node_id,))), None)
    # Verbraucher in the part below the node (or the node itself).
    loads = [row[0] for row in db.execute(
        """SELECT DISTINCT n.load_id FROM connection_nodes n WHERE n.node_type='load'
           AND (n.id=? OR n.id IN (SELECT node_id FROM connection_ancestors WHERE ancestor_id=?))""", (node_id, node_id))]
    if channel is None and not loads:
        raise ConnectionTreeError("A Klemme on N or PE cannot be put into a fused path; there is no Kanal or Verbraucher here to connect it to.")
    if channel is not None:
        link_conductor_to_channel(db, channel, terminal_id)
    for load_id in loads:
        connect_load(db, terminal_id, load_id)
    return int(db.execute(
        """SELECT n.id FROM connection_nodes n JOIN connection_nodes p ON p.id=n.parent_id
           WHERE n.node_type='terminal' AND n.terminal_id=? AND p.node_type IN ('phase', 'fi') ORDER BY n.id""",
        (terminal_id,)).fetchone()[0])


def link_conductor_to_channel(db: sqlite3.Connection, channel_node_id: int, terminal_id: int) -> int:
    """Link a Klemme on N, an FI's neutral or PE to a fused device channel without fusing it.

    The Klemme stays at its conductor and the same Kanal hangs below it, e.g. N -> 20/o -> 1.1.5 Kanal 1.
    Returns the Kanal node below the Klemme.
    """
    channel = _node(db, channel_node_id)
    spec = {name: channel[name] for name in ("device_id", "channel_key", "channel_id", "note")}
    # Prefer the node at the conductor, then a loose root node; otherwise the Klemme starts a chain of its own.
    anchor = db.execute(
        """SELECT n.id FROM connection_nodes n LEFT JOIN connection_nodes p ON p.id=n.parent_id
           WHERE n.node_type='terminal' AND n.terminal_id=? AND (p.id IS NULL OR p.node_type IN ('phase', 'fi'))
           ORDER BY p.id IS NULL, n.id LIMIT 1""",
        (terminal_id,),
    ).fetchone()
    anchor_id = int(anchor[0]) if anchor else _insert_node(db, None, "terminal", {"terminal_id": terminal_id})
    new_id = _get_or_create(db, anchor_id, "channel", spec)
    finish(db)
    return new_id


def add_branch(db: sqlite3.Connection, node_id: int, kind: str, ref: dict[str, Any]) -> int:
    """Hang a Klemme or Kanal below a node as another follower, next to the ones it already has.

    `ref` is a channel spec (kind 'channel') or {'terminal_id': ...} (kind 'terminal'). A Klemme that
    only exists as a bare root node is moved here instead of being duplicated. Returns the new node's id.
    Behind a Verbraucher (its output, e.g. Steckdose 3 beside Steckdose 2 at a UPS) only a Klemme can be added.
    """
    if _node(db, node_id)["node_type"] == "load":
        if kind != "terminal":
            raise ConnectionTreeError("Only a Klemme can be placed behind a Verbraucher.")
        new_id = _hang_behind(db, node_id, ref["terminal_id"])
        finish(db)
        return new_id
    parent = _structural_node(db, node_id)
    if kind not in TYPE_CHILDREN[parent["node_type"]] & {"channel", "terminal"}:
        raise ConnectionTreeError(f"A {kind} cannot be placed below a {parent['node_type']}.")
    if kind == "channel":
        # A Kanal only links something with a Klemme on its path.
        if parent["node_type"] != "terminal" and not db.execute(
            """SELECT 1 FROM connection_ancestors a JOIN connection_nodes p ON p.id=a.ancestor_id
               WHERE a.node_id=? AND p.node_type='terminal'""", (node_id,)
        ).fetchone():
            raise ConnectionTreeError("A Kanal needs a Klemme above it.")
        new_id = _get_or_create(db, node_id, "channel", ref)
    elif parent["node_type"] == "channel" and _conductor_return(db, node_id, ref["terminal_id"]):
        # The N/PE of a fused device channel: the Kanal is linked below the Klemme's own conductor node.
        return link_conductor_to_channel(db, node_id, ref["terminal_id"])
    else:
        new_id = _find_node(db, node_id, "terminal", ref)
        if new_id is None:
            # A Klemme behind a Klemme takes over its loose node (with its Verbraucher), as `add_follower` does.
            if parent["node_type"] == "terminal":
                if parent["terminal_id"] == ref["terminal_id"]:
                    raise ConnectionTreeError("A Klemme cannot follow itself.")
                new_id = _hang_behind(db, node_id, ref["terminal_id"])
                _move_loads_to_chain_ends(db)
                finish(db)
                return new_id
            bare = db.execute(
                f"""SELECT id FROM connection_nodes WHERE node_type='terminal' AND terminal_id=? AND parent_id IS NULL
                    AND NOT EXISTS (SELECT 1 FROM connection_nodes child
                                    WHERE child.parent_id=connection_nodes.id AND {STRUCTURAL_CHILD_SQL})""",
                (ref["terminal_id"],),
            ).fetchone()
            if bare:
                new_id = int(bare[0])
                _move_raw(db, new_id, node_id)
            else:
                new_id = _insert_node(db, node_id, "terminal", ref)
    finish(db)
    return new_id


def insert_terminal(db: sqlite3.Connection, node_id: int, terminal_id: int) -> int:
    """Put a Klemme into the wire above `node_id` (Kanal, Klemme or Verbraucher): parent -> Klemme -> node.

    The Klemme takes the node's place among its siblings: also in front of the first entry of a chain (a root, or
    a Klemme at its conductor N/PE: the new Klemme then starts the chain or sits at the conductor). A Klemme that
    only exists as a bare root node is moved in (with its Verbraucher), one already below the same parent is
    reused; the chain rules decide the rest. Returns the Klemme's node id.
    """
    node = _node(db, node_id)
    if node["node_type"] not in ("channel", "terminal", "load"):
        raise ConnectionTreeError("A Klemme can only be inserted above a Kanal, a Klemme or a Verbraucher.")
    parent_id = node["parent_id"]
    if parent_id is None and node["node_type"] == "load":
        raise ConnectionTreeError("There is no wire above this entry.")
    if node["node_type"] == "terminal" and node["terminal_id"] == terminal_id:
        raise ConnectionTreeError("A Klemme cannot follow itself.")
    # A Klemme on N/PE cannot lie in a fused wire: it becomes the neutral/PE of the device and Verbraucher there.
    if parent_id is not None and never_fused(db, terminal_id) and fused_above(db, parent_id):
        return link_conductor_to_path(db, node_id, terminal_id)
    siblings = _children(db, parent_id)
    new_id = _find_node(db, parent_id, "terminal", {"terminal_id": terminal_id})
    if new_id is None:
        bare = db.execute(
            f"""SELECT id FROM connection_nodes WHERE node_type='terminal' AND terminal_id=? AND parent_id IS NULL
                AND NOT EXISTS (SELECT 1 FROM connection_nodes child
                                WHERE child.parent_id=connection_nodes.id AND {STRUCTURAL_CHILD_SQL})""",
            (terminal_id,),
        ).fetchone()
        if bare:
            new_id = int(bare[0])
            _move_raw(db, new_id, parent_id)
        else:
            new_id = _insert_node(db, parent_id, "terminal", {"terminal_id": terminal_id})
    _move_raw(db, node_id, new_id)
    # The Klemme stands where the node stood (Verbraucher are side nodes without an order).
    if node_id in siblings:
        order = [new_id if sibling == node_id else sibling for sibling in siblings if sibling != new_id]
        _write_order(db, order + [child for child in _children(db, parent_id) if child not in order])
    _move_loads_to_chain_ends(db)
    finish(db)
    return new_id


def _unlink_pair(db: sqlite3.Connection, pair_id: int) -> None:
    """Cut the Kanal/Klemme relation identified by its deeper node; each side stays connected elsewhere."""
    node = _node(db, pair_id)
    parent = _node(db, node["parent_id"]) if node["parent_id"] else None
    if parent is None or {node["node_type"], parent["node_type"]} != {"channel", "terminal"}:
        raise ConnectionTreeError("Only a directly adjacent Kanal and Klemme can be disconnected; reorder them first.")
    if node["node_type"] == "channel":
        # Klemme -> Kanal: dropping the channel node keeps the Klemme in place.
        if _has_children(db, pair_id):
            raise ConnectionTreeError("Move the entries below this Kanal first.")
        db.execute("DELETE FROM connection_nodes WHERE id=?", (pair_id,))
        return
    carrier_parent = parent["parent_id"]
    carrier_parent_type = _node(db, carrier_parent)["node_type"] if carrier_parent else None
    duplicate = carrier_parent_type == "fuse" and _find_node(db, carrier_parent, "terminal", {"terminal_id": node["terminal_id"]})
    if carrier_parent_type == "fuse" and not duplicate:
        _move_raw(db, pair_id, carrier_parent)
    elif _has_children(db, pair_id):
        raise ConnectionTreeError("Move the entries below this Klemme first.")
    else:
        db.execute("DELETE FROM connection_nodes WHERE id=?", (pair_id,))
    if not _has_children(db, parent["id"]):
        db.execute("DELETE FROM connection_nodes WHERE id=?", (parent["id"],))


@_keeps_side_nodes
def remove_assignment(db: sqlite3.Connection, assignment_id: int) -> None:
    """Disconnect one Klemme from one ETS object, addressed by its `channel_terminals` id."""
    row = db.execute("SELECT channel_id, terminal_id FROM channel_terminals WHERE id=?", (assignment_id,)).fetchone()
    if row is None:
        return
    pair_ids = [pair[0] for pair in db.execute(
        """SELECT pair.id FROM connection_channel_pairs pair JOIN connection_nodes channel ON channel.id=pair.channel_node_id
           WHERE channel.channel_id=? AND pair.terminal_id=?""", (row[0], row[1]))]
    for pair_id in pair_ids:
        if db.execute("SELECT 1 FROM connection_nodes WHERE id=?", (pair_id,)).fetchone():
            _unlink_pair(db, pair_id)
    finish(db)


@_keeps_side_nodes
def remove_channel_link(db: sqlite3.Connection, terminal_id: int, device_id: str, key: str) -> tuple[int, int]:
    """Disconnect a Klemme from one physical channel; returns (workbook links, object assignments) removed."""
    pairs = _rows(
        db,
        """SELECT pair.id, channel.channel_id FROM connection_channel_pairs pair
           JOIN connection_nodes channel ON channel.id=pair.channel_node_id
           WHERE pair.terminal_id=? AND channel.device_id=? AND channel.channel_key=?""",
        (terminal_id, device_id, key),
    )
    for pair in pairs:
        if db.execute("SELECT 1 FROM connection_nodes WHERE id=?", (pair["id"],)).fetchone():
            _unlink_pair(db, pair["id"])
    finish(db)
    return (sum(pair["channel_id"] is None for pair in pairs), sum(pair["channel_id"] is not None for pair in pairs))


@_keeps_side_nodes
def rename_channel_key(db: sqlite3.Connection, device_id: str, old_key: str, new_key: str) -> None:
    """Follow a renamed physical channel; nodes that would collide with an existing one are dropped."""
    db.execute(
        "UPDATE OR IGNORE connection_nodes SET channel_key=? WHERE node_type='channel' AND device_id=? AND channel_key=?",
        (new_key, device_id, old_key),
    )
    db.execute("DELETE FROM connection_nodes WHERE node_type='channel' AND device_id=? AND channel_key=?", (device_id, old_key))
    finish(db)


@_keeps_side_nodes
def remove_channel_nodes(db: sqlite3.Connection, device_id: str, key: str) -> None:
    """Remove a channel from every chain; connected Klemmen stay where they are."""
    db.execute("DELETE FROM connection_nodes WHERE node_type='channel' AND device_id=? AND channel_key=?", (device_id, key))
    finish(db)


@_keeps_side_nodes
def rebuild_nodes(
    db: sqlite3.Connection,
    channel_specs: dict[int, list[dict[str, Any]]],
    fuse_fi: dict[int, int],
    terminal_fuse: dict[int, int],
    followers: list[tuple[int, int]] = (),
) -> None:
    """Rebuild the whole tree from the given fuse->FI and Klemme->fuse maps and channel references per Klemme.

    `followers` lists (Klemme above, Klemme below) wires to keep: they are wired on again before the
    conductors and Verbraucher are assigned, so both see the whole path.
    """
    db.execute("DELETE FROM connection_nodes")
    for fuse_id, fi_id in sorted(fuse_fi.items()):
        ensure_fuse_node(db, fuse_id, fi_id)
    for (terminal_id,) in db.execute("SELECT id FROM terminals ORDER BY id").fetchall():
        # N and PE Klemmen of a Sicherung row are never fused; `assign_wire_phases` places them at their conductor.
        fuse_id = None if is_unfused_conductor(db, terminal_id) else terminal_fuse.get(terminal_id)
        ensure_terminal_chain(db, terminal_id, fuse_id, channel_specs.get(terminal_id, []))
    for above_id, below_id in followers:
        if above_id != below_id and not _follows(db, above_id, below_id):
            _hang_behind(db, _terminal_anchor(db, above_id), below_id)
    apply_default_order(db)
    # Blue Klemmen of a Sicherung row move to their neutral; then every cable gets its Verbraucher.
    assign_wire_phases(db)
    assign_loads(db)
    finish(db)


# ---------------------------------------------------------------- labels and default order

def node_labels(db: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    """Display data for every node (title, subtitle, entity reference for navigation)."""
    labels: dict[int, dict[str, Any]] = {}
    for row in _rows(
        db,
        f"""SELECT n.id, n.node_type, n.channel_key, n.device_id, n.channel_id, n.fi_id, n.fuse_id, n.terminal_id,
                  fi.number AS fi_number, fi.label AS fi_label, fil.name AS fi_location,
                  f.label AS fuse_label, f.rating AS fuse_rating, f.phases AS fuse_phases, fl.name AS fuse_location,
                  t.label AS terminal_label, t.wire_color, t.used, tl.name AS terminal_location,
                  d.manual_name, d.is_manual, d.product,
                  w.code AS phase_code, w.name AS phase_name, w.wire_color AS phase_color,
                  ld.name AS load_name, ldl.name AS load_location, ld.terminal_number AS load_number, {_no_pe_sql(db)} AS load_no_pe,
                  (SELECT c.object_number FROM channels c WHERE c.device_id=n.device_id
                      AND (n.channel_id=c.id OR (n.channel_id IS NULL AND c.physical_channel=n.channel_key))
                      ORDER BY c.object_number LIMIT 1) AS object_number,
                  -- Meaning of a channel: the name of the first group address it sends on (direction S).
                  (SELECT COALESCE(NULLIF(TRIM(g.name), ''), g.address) FROM channels c
                      JOIN channel_group_addresses a ON a.channel_id=c.id JOIN group_addresses g ON g.address=a.address
                      WHERE c.device_id=n.device_id
                        AND (n.channel_id=c.id OR (n.channel_id IS NULL
                             AND (c.physical_channel=n.channel_key OR 'object:' || c.object_number=n.channel_key)))
                        AND instr('/' || COALESCE(a.direction, '') || '/', '/S/') > 0
                      ORDER BY CAST(c.object_number AS INTEGER), c.object_number, g.address LIMIT 1) AS send_group
           FROM connection_nodes n
           LEFT JOIN fi_devices fi ON fi.id=n.fi_id LEFT JOIN locations fil ON fil.id=fi.location_id
           LEFT JOIN fuses f ON f.id=n.fuse_id LEFT JOIN locations fl ON fl.id=f.location_id
           LEFT JOIN terminals t ON t.id=n.terminal_id LEFT JOIN locations tl ON tl.id=t.location_id
           LEFT JOIN devices d ON d.device_id=n.device_id
           LEFT JOIN wire_phases w ON w.id=n.wire_phase_id
           LEFT JOIN loads ld ON ld.id=n.load_id LEFT JOIN locations ldl ON ldl.id=ld.location_id""",
    ):
        kind = row["node_type"]
        if kind == "phase":
            labels[row["id"]] = {"title": row["phase_code"], "subtitle": row["phase_name"], "location": "",
                                 "entity_type": "", "entity_id": "", "code": row["phase_code"],
                                 "wire_color": row["phase_color"]}
        elif kind == "load":
            labels[row["id"]] = {"title": row["load_name"], "subtitle": f"Kabel {row['load_number']}" if row["load_number"] else "",
                                 "location": row["load_location"], "entity_type": "", "entity_id": "",
                                 "no_pe": bool(row["load_no_pe"])}
        elif kind == "fi":
            title = f"FI {row['fi_number']}" if row["fi_number"] else (row["fi_label"] or "FI")
            # A 4-pole FI starts its own neutral, named after its number.
            labels[row["id"]] = {"title": title, "subtitle": row["fi_label"] if row["fi_number"] else "",
                                 "location": row["fi_location"], "entity_type": "fi", "entity_id": str(row["fi_id"]),
                                 "neutral": neutral_code(row["fi_number"])}
        elif kind == "fuse":
            # Phases are stored as 'L1,L3'; the list feeds the phase labels on the graph wires.
            labels[row["id"]] = {"title": row["fuse_label"], "subtitle": row["fuse_rating"] or "",
                                 "location": row["fuse_location"], "entity_type": "fuse", "entity_id": str(row["fuse_id"]),
                                 "phases": [phase for phase in (row["fuse_phases"] or "").split(",") if phase]}
        elif kind == "terminal":
            labels[row["id"]] = {"title": f"Klemme {row['terminal_label']}", "subtitle": "", "wire_color": row["wire_color"],
                                 "used": bool(row["used"]), "location": row["terminal_location"],
                                 "entity_type": "terminal", "entity_id": str(row["terminal_id"])}
        else:
            device = row["manual_name"] if row["is_manual"] else row["device_id"]
            key = row["channel_key"]
            channel = f"Kanal {key}" if not key.startswith("object:") else f"Objekt {key.removeprefix('object:')}"
            if row["channel_id"] is not None and not key.startswith("object:"):
                channel += f" · Obj. {row['object_number']}"
            entity_id = f"{row['device_id']}:{row['object_number']}" if row["object_number"] is not None else ""
            labels[row["id"]] = {"title": f"{device} {channel}", "subtitle": "" if row["is_manual"] else (row["product"] or ""),
                                 "location": "", "entity_type": "channel" if entity_id else "", "entity_id": entity_id,
                                 "meaning": row["send_group"] or ""}
    return labels


def apply_default_order(db: sqlite3.Connection) -> None:
    """Order siblings by type, then natural label order (used for freshly built trees)."""
    labels = node_labels(db)
    groups: dict[int | None, list[tuple[int, str]]] = {}
    for node_id, parent_id, node_type in db.execute(
        f"SELECT id, parent_id, node_type FROM connection_nodes WHERE node_type NOT IN {SIDE_TYPES_SQL}"
    ):
        groups.setdefault(parent_id, []).append((node_id, node_type))
    for members in groups.values():
        members.sort(key=lambda item: (
            TYPE_RANK[item[1]], natural_key(str(labels[item[0]]["location"] or "")),
            natural_key(str(labels[item[0]]["title"] or "")), item[0],
        ))
        _write_order(db, [node_id for node_id, _ in members])


# ---------------------------------------------------------------- reading trees

def _subtree(db: sqlite3.Connection, root_id: int) -> list[int]:
    return [row[0] for row in db.execute(
        """WITH RECURSIVE sub(id) AS (SELECT ?1 UNION SELECT n.id FROM connection_nodes n JOIN sub ON n.parent_id=sub.id)
           SELECT id FROM sub""", (root_id,))]


def _root_of(db: sqlite3.Connection, node_id: int) -> int:
    row = db.execute(
        """SELECT COALESCE((SELECT a.ancestor_id FROM connection_ancestors a JOIN connection_nodes r ON r.id=a.ancestor_id
                            WHERE a.node_id=?1 AND r.parent_id IS NULL), ?1)""", (node_id,)
    ).fetchone()
    return int(row[0])


def _chain_root(db: sqlite3.Connection, node_id: int) -> int:
    """Top of a node's own chain: the node directly below a conductor start node, else the real root.

    Pages of one FI, Sicherung or Klemme show that chain, not everything hanging on the same conductor.
    """
    current = node_id
    while True:
        parent = db.execute(
            "SELECT p.id, p.node_type FROM connection_nodes n JOIN connection_nodes p ON p.id=n.parent_id WHERE n.id=?",
            (current,),
        ).fetchone()
        if parent is None or parent[1] == "phase" or current == parent[0]:
            return current
        current = int(parent[0])


def trees_for_entity(db: sqlite3.Connection, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
    """All chains (as nested dicts) that contain the given FI, Sicherung, Klemme or channel."""
    if entity_type == "fi" and entity_id.isdigit():
        node_ids = [row[0] for row in db.execute("SELECT id FROM connection_nodes WHERE node_type='fi' AND fi_id=?", (int(entity_id),))]
    elif entity_type == "fuse" and entity_id.isdigit():
        node_ids = [row[0] for row in db.execute("SELECT id FROM connection_nodes WHERE node_type='fuse' AND fuse_id=?", (int(entity_id),))]
    elif entity_type == "terminal" and entity_id.isdigit():
        node_ids = [row[0] for row in db.execute("SELECT id FROM connection_nodes WHERE node_type='terminal' AND terminal_id=?", (int(entity_id),))]
    elif entity_type == "channel":
        device_id, _, object_number = entity_id.rpartition(":")
        channel = db.execute(
            "SELECT id, physical_channel FROM channels WHERE device_id=? AND object_number=?", (device_id, object_number)
        ).fetchone()
        if channel is None:
            return []
        node_ids = [row[0] for row in db.execute(
            """SELECT id FROM connection_nodes WHERE node_type='channel' AND device_id=?
               AND (channel_id=? OR (channel_id IS NULL AND channel_key=?))""",
            (device_id, channel[0], channel_key(channel[1], object_number)),
        )]
    else:
        raise ConnectionTreeError("Unknown record type for connections.")
    roots = sorted({_chain_root(db, node_id) for node_id in node_ids}, key=lambda root: (
        db.execute("SELECT sort_order FROM connection_nodes WHERE id=?", (root,)).fetchone()[0], root))
    labels = node_labels(db)
    return [tree_json(db, root, labels) for root in roots]


def tree_json(db: sqlite3.Connection, root_id: int, labels: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Nested node dict with the allowed ordering actions for each node (Verbraucher left out)."""
    ids = _subtree(db, root_id)
    rows = {row["id"]: row for row in _rows(
        db,
        f"SELECT id, parent_id, sort_order, node_type FROM connection_nodes WHERE {STRUCTURAL_SQL} AND id IN (%s)"
        % ",".join("?" * len(ids)),
        tuple(ids),
    )}
    children: dict[int | None, list[int]] = {}
    for node_id, row in sorted(rows.items(), key=lambda item: (item[1]["sort_order"], item[0])):
        children.setdefault(row["parent_id"], []).append(node_id)

    def build(node_id: int) -> dict[str, Any]:
        row = rows[node_id]
        siblings = children.get(row["parent_id"], []) if node_id != root_id else [node_id]
        position = siblings.index(node_id)
        kids = children.get(node_id, [])
        return {
            "id": node_id, "type": row["node_type"], "parent_id": row["parent_id"], "index": position,
            **labels[node_id],
            "can_move_up": position > 0, "can_move_down": position < len(siblings) - 1,
            "swappable_children": [child for child in kids if _swap_is_valid(rows, children, node_id, child)],
            "children": [build(child) for child in kids],
        }

    return build(root_id)


def _swap_is_valid(rows: dict[int, dict[str, Any]], children: dict[int | None, list[int]], node_id: int,
                   child_id: int | None = None) -> bool:
    """Predict whether swapping a node with a follower (default: its only one) keeps the chain rules."""
    followers = children[node_id]
    if child_id is None:
        if len(followers) != 1:
            return False
        child_id = followers[0]
    parent_id = rows[node_id]["parent_id"]
    node_type, child_type = rows[node_id]["node_type"], rows[child_id]["node_type"]
    # The top of a chain below a conductor start node stays there (the conductor decides what it carries).
    if parent_id is not None and parent_id not in rows:
        return False
    parent_type = rows[parent_id]["node_type"] if parent_id else None
    # A node with other followers is split in two; FI and Sicherung exist only once in the tree, a Verbraucher's
    # output is not split either.
    if len(followers) > 1 and node_type in ("fi", "fuse", "load"):
        return False
    # After the swap: child sits below the old parent, node below the child, grandchildren below the node.
    return (
        parent_type in ALLOWED_PARENTS[child_type]
        and child_type in ALLOWED_PARENTS[node_type]
        and all(rows[grandchild]["node_type"] in TYPE_CHILDREN[node_type] for grandchild in children.get(child_id, []))
    )


def _load_swap_is_valid(rows: dict[int, dict[str, Any]], terminal_node_id: int) -> bool:
    """Whether a Verbraucher at the end of a path can swap with the Klemme it hangs on (UPS -> Steckdose, for its L,
    N or PE): the Verbraucher takes the Klemme's place, which must be behind a Klemme, Gerät-Kanal or Sicherung."""
    row = rows.get(terminal_node_id)
    if row is None or row["node_type"] != "terminal":
        return False
    parent = rows.get(row["parent_id"]) if row["parent_id"] is not None else None
    return parent is not None and parent["node_type"] in ("terminal", "channel", "fuse")


def _tree_context(db: sqlite3.Connection, node_ids: list[int]) -> tuple[dict[int, dict[str, Any]], dict[int | None, list[int]]]:
    """Rows and ordered child lists of every tree that contains one of the given nodes."""
    ids = sorted({member for root in {_chain_root(db, node_id) for node_id in node_ids} for member in _subtree(db, root)})
    rows = {row["id"]: row for row in _rows(
        db,
        "SELECT id, parent_id, sort_order, node_type, terminal_id, device_id, channel_key, load_id FROM connection_nodes "
        f"WHERE {STRUCTURAL_SQL} AND id IN (%s)" % ",".join("?" * len(ids)),
        tuple(ids),
    )}
    children: dict[int | None, list[int]] = {}
    for node_id, row in sorted(rows.items(), key=lambda item: (item[1]["sort_order"], item[0])):
        children.setdefault(row["parent_id"], []).append(node_id)
    return rows, children


def _paths_through(rows: dict[int, dict[str, Any]], children: dict[int | None, list[int]], node_id: int) -> list[list[int]]:
    """Every root-to-leaf path (node ids) that runs through the node, in tree order."""
    above: list[int] = []
    parent_id = rows[node_id]["parent_id"]
    # The chain ends at its conductor start node, which is outside the loaded rows.
    while parent_id is not None and parent_id in rows:
        above.insert(0, parent_id)
        parent_id = rows[parent_id]["parent_id"]
    paths: list[list[int]] = []

    def walk(current: int, path: list[int]) -> None:
        path = path + [current]
        if not children.get(current):
            paths.append(path)
        for child in children.get(current, []):
            walk(child, path)

    walk(node_id, above)
    return paths


def _merge_paths(rows: dict[int, dict[str, Any]], children: dict[int | None, list[int]], paths: list[list[int]],
                 down: set[int] = frozenset()) -> list[dict[str, Any]]:
    """One chain from several paths: each node once, first-seen order, FI nodes left out.

    A chain entry carries `swap_node_id` (the node above it) and `swap_child_id` (the entry itself)
    when swapping the two keeps the chain rules, and `drop_node_id` (the entry itself) when it hangs
    directly below the node above, i.e. the wire between them can be cut. `branch_node_id` and
    `branch_type` name that node above, where another branch can be added.

    The node above is the previous entry, except for entries below a node of `down` (the Klemme and
    everything that follows it): those are branches, so their real parent is the node above and is
    reported as `down_parent_id`. Siblings of such a branch share it.
    """
    chain: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for path in paths:
        for node_id in path:
            row = rows[node_id]
            identity = (row["node_type"], row["terminal_id"], row["device_id"], row["channel_key"], row.get("load_id"))
            if row["node_type"] == "fi" or identity in seen:
                continue
            seen.add(identity)
            branch = row["parent_id"] in down
            above = row["parent_id"] if branch else (chain[-1]["node_id"] if chain else None)
            attached = above is not None and row["parent_id"] == above
            swappable = attached and _swap_is_valid(rows, children, above, node_id)
            chain.append({"type": row["node_type"], "node_id": node_id, "load_id": row.get("load_id"), "terminal_id": row["terminal_id"],
                          "device_id": row["device_id"], "channel_key": row["channel_key"],
                          "swap_node_id": above if swappable else None,
                          "swap_child_id": node_id if swappable else None,
                          "drop_node_id": node_id if attached else None,
                          # The node above can take another follower (a branch) next to this entry.
                          "branch_node_id": above if attached else None,
                          "branch_type": rows[above]["node_type"] if attached else None,
                          "down_parent_id": row["parent_id"] if branch else None})
    return chain


def fuse_graph(db: sqlite3.Connection, fuse_id: int) -> dict[str, Any] | None:
    """Tree for the Sicherung page: the path above the Sicherung (FI left out), then its whole subtree."""
    fuse_node = db.execute("SELECT id FROM connection_nodes WHERE node_type='fuse' AND fuse_id=?", (fuse_id,)).fetchone()
    if fuse_node is None:
        return None

    def descend(node: dict[str, Any]) -> dict[str, Any] | None:
        """Keep only the branch that leads to the Sicherung; its own subtree stays complete."""
        if node["id"] == fuse_node[0]:
            return node
        for child in node["children"]:
            if descend(child) is not None:
                node["children"] = [child]
                return node
        return None

    root = descend(tree_json(db, _chain_root(db, fuse_node[0]), node_labels(db)))
    if root["type"] == "fi":
        root = root["children"][0]
    # Channel nodes carry device and channel key so the UI can look up their ETS details.
    details = {row["id"]: row for row in _rows(db, "SELECT id, device_id, channel_key FROM connection_nodes")}

    def annotate(node: dict[str, Any]) -> None:
        if node["type"] == "channel":
            node["device_id"], node["channel_key"] = details[node["id"]]["device_id"], details[node["id"]]["channel_key"]
        for child in node["children"]:
            annotate(child)

    annotate(root)
    return root


def full_graph(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """The whole connection forest as nested dicts in tree order (Verbraucher as leaves), read-only.

    Conductor start nodes come first (L1, L2, L3, N, PE). Klemmen carry their conductor (`wire_phase`);
    a Verbraucher links to the Klemme it hangs on.
    """
    labels = node_labels(db)
    codes = wire_phase_map(db)
    rows = _rows(
        db,
        """SELECT n.id, n.parent_id, n.node_type, n.terminal_id, n.load_id, w.sort_order AS phase_order
           FROM connection_nodes n LEFT JOIN wire_phases w ON w.id=n.wire_phase_id ORDER BY n.sort_order, n.id""",
    )
    # Klemme that feeds each Verbraucher node (its parent, or the Klemme above its Gerät-Kanal).
    load_links = _load_links(db)
    feeders = {link["id"]: link["terminal_id"] for link in load_links}
    # Where a Verbraucher card opens when its own node has no Klemme (directly on a Sicherung, N or PE): a Klemme
    # feeding the Verbraucher elsewhere, else the Sicherung it hangs on (its page lists it for editing).
    load_pages: dict[int, tuple[str, str]] = {}
    for load_id, fuse_links in direct_fuse_links(db).items():
        load_pages[load_id] = ("fuse", str(fuse_links[0]["fuse_id"]))
    for link in load_links:
        if link["terminal_id"] is not None:
            load_pages[link["load_id"]] = ("terminal", str(link["terminal_id"]))
    children: dict[int | None, list[dict[str, Any]]] = {}
    for row in rows:
        children.setdefault(row["parent_id"], []).append(row)

    def build(row: dict[str, Any]) -> dict[str, Any]:
        label = labels[row["id"]]
        kind = row["node_type"]
        # A channel shows what it does (its first sending group address), the product only as fallback.
        node = {"id": row["id"], "type": kind, "title": label["title"], "subtitle": label.get("meaning") or label.get("subtitle", ""),
                "location": label.get("location", ""), "entity_type": label.get("entity_type", ""),
                "entity_id": label.get("entity_id", ""), "wire_color": label.get("wire_color", ""),
                "used": label.get("used", True), "phases": label.get("phases", []),
                "code": label.get("code", ""), "neutral": label.get("neutral", ""),
                "wire_phase": codes.get(row["terminal_id"], "") if kind == "terminal" else "",
                "load_id": row["load_id"], "no_pe": label.get("no_pe", False),
                "children": [build(child) for child in children.get(row["id"], [])]}
        if kind == "load" and feeders.get(row["id"]) is not None:
            node["entity_type"], node["entity_id"] = "terminal", str(feeders[row["id"]])
        elif kind == "load":
            # Without a Klemme or Sicherung page: the Verbraucher's own page.
            node["entity_type"], node["entity_id"] = load_pages.get(row["load_id"], ("load", str(row["load_id"])))
        return node

    roots = sorted(children.get(None, []), key=lambda root: (root["node_type"] != "phase", root["phase_order"] or 0))
    return [build(root) for root in roots]


def graph_payload(db: sqlite3.Connection) -> dict[str, Any]:
    """Everything the Gesamtgraph shows: the forest, the Prüfliste and the phase balance."""
    return {"roots": full_graph(db), "review": review_items(db), "phase_balance": phase_balance(db),
            "loads": db.execute("SELECT COUNT(*) FROM loads").fetchone()[0]}


def terminal_chain(db: sqlite3.Connection, terminal_id: int) -> list[dict[str, Any]]:
    """The chain of one Klemme (Sicherung, Kanäle) in tree order; empty when it is in no chain."""
    node_ids = [row[0] for row in db.execute(
        "SELECT id FROM connection_nodes WHERE node_type='terminal' AND terminal_id=? ORDER BY id", (terminal_id,))]
    if not node_ids:
        return []
    rows, children = _tree_context(db, node_ids)
    # The Klemme nodes and all nodes below them; entries below these are branches of the path.
    down: set[int] = set()
    pending = list(node_ids)
    while pending:
        current = pending.pop()
        down.add(current)
        pending.extend(children.get(current, []))
    chain = _merge_paths(rows, children, [path for node_id in node_ids for path in _paths_through(rows, children, node_id)], down)
    # Verbraucher below the Klemme or one of its followers, as branches of their real parent (Klemme or Kanal).
    listed = {entry["node_id"] for entry in chain}
    for node_id, parent_id, load_id in db.execute(
        "SELECT id, parent_id, load_id FROM connection_nodes WHERE node_type='load' AND parent_id IN (%s) ORDER BY sort_order, id"
        % ",".join("?" * len(down)), tuple(down),
    ).fetchall():
        if parent_id in listed and node_id not in listed:
            # A Verbraucher can swap with its Klemme: the Klemme then hangs behind the Verbraucher's output.
            swappable = _load_swap_is_valid(rows, parent_id)
            chain.append({"type": "load", "node_id": node_id, "load_id": load_id, "terminal_id": None, "device_id": None,
                          "channel_key": None, "swap_node_id": parent_id if swappable else None,
                          "swap_child_id": node_id if swappable else None, "drop_node_id": None,
                          "branch_node_id": None, "branch_type": None, "down_parent_id": parent_id})
    return chain


# ---------------------------------------------------------------- reordering

def reorder_children(db: sqlite3.Connection, parent_id: int | None, ordered_ids: list[int]) -> None:
    """Set the exact order of all children of a node (or of the roots)."""
    if sorted(ordered_ids) != sorted(_children(db, parent_id)) or len(set(ordered_ids)) != len(ordered_ids):
        raise ConnectionTreeError("The new order must list every sibling exactly once.")
    _write_order(db, ordered_ids)


def move_sibling(db: sqlite3.Connection, node_id: int, delta: int) -> None:
    """Move a node one position up (-1) or down (+1) among its siblings."""
    node = _structural_node(db, node_id)
    siblings = _children(db, node["parent_id"])
    index = siblings.index(node_id)
    target = index + delta
    if not 0 <= target < len(siblings):
        raise ConnectionTreeError("The node is already at the end of its list.")
    siblings[index], siblings[target] = siblings[target], siblings[index]
    _write_order(db, siblings)


@_keeps_side_nodes
def move_node(db: sqlite3.Connection, node_id: int, new_parent_id: int | None, index: int | None = None) -> None:
    """Move a node with its subtree below another node (or to the roots) at a position."""
    node = _structural_node(db, node_id)
    if new_parent_id is not None:
        # A conductor start node can take followers; a Verbraucher only Klemmen (behind its output).
        if _node(db, new_parent_id)["node_type"] in SIDE_TYPES and node["node_type"] != "terminal":
            raise ConnectionTreeError("Only a Klemme can be placed behind a Verbraucher.")
        if new_parent_id in _subtree(db, node_id):
            raise ConnectionTreeError("A node cannot be moved below itself.")
    siblings = [child for child in _children(db, new_parent_id) if child != node_id]
    position = len(siblings) if index is None else max(0, min(index, len(siblings)))
    siblings.insert(position, node_id)
    try:
        db.execute("UPDATE connection_nodes SET parent_id=? WHERE id=?", (new_parent_id, node_id))
    except sqlite3.IntegrityError as error:
        raise ConnectionTreeError("The same entry already exists at that position.") from error
    _write_order(db, siblings)
    _renumber(db, node["parent_id"])
    finish(db)


def _swap_with_load(db: sqlite3.Connection, upper_id: int, lower_id: int) -> None:
    """Swap a Klemme and a Verbraucher directly below it (or back): 200/17 -> Steckdose 2 -> UPS becomes
    200/17 -> UPS -> Steckdose 2, the Steckdose then hangs on the UPS output.

    The lower node takes the upper one's place; what hangs behind the lower node's output (Klemmen) moves to the
    upper one, the upper one keeps its own Verbraucher. The rules decide the rest (`finish`).
    """
    upper, lower = _node(db, upper_id), _node(db, lower_id)
    if lower["parent_id"] != upper_id:
        raise ConnectionTreeError("Only a node and its direct follower can be swapped.")
    if {upper["node_type"], lower["node_type"]} != {"terminal", "load"}:
        raise ConnectionTreeError("A Verbraucher can only be swapped with a Klemme.")
    if upper["parent_id"] is None:
        raise ConnectionTreeError("There is no wire above this entry.")
    if upper["node_type"] == "load" and db.execute(
            "SELECT 1 FROM connection_nodes WHERE parent_id=? AND node_type='terminal' AND id<>?", (upper_id, lower_id)).fetchone():
        raise ConnectionTreeError("Several Klemmen hang behind this Verbraucher; choose one by removing the others first.")
    try:
        # Klemmen behind the lower node's output go behind the upper node.
        db.execute(f"UPDATE connection_nodes SET parent_id=? WHERE parent_id=? AND node_type NOT IN {SIDE_TYPES_SQL}",
                   (upper_id, lower_id))
        db.execute("UPDATE connection_nodes SET parent_id=?, sort_order=? WHERE id=?",
                   (upper["parent_id"], upper["sort_order"], lower_id))
        db.execute("UPDATE connection_nodes SET parent_id=?, sort_order=0 WHERE id=?", (lower_id, upper_id))
    except sqlite3.IntegrityError as error:
        raise ConnectionTreeError("The same entry already exists at that position.") from error
    finish(db)


def swap_with_child(db: sqlite3.Connection, node_id: int, child_id: int | None = None) -> None:
    """Swap a node with one follower (default: its only one); the part of the chain below the follower stays below.

    A node with several followers is split: the other followers stay below the original node,
    the swapped path gets its own copy of the node. If the follower already exists beside the node
    (e.g. when a split is swapped back), the swapped path is merged into that one.
    """
    if child_id is not None and "load" in (_node(db, node_id)["node_type"], _node(db, child_id)["node_type"]):
        _swap_with_load(db, node_id, child_id)
        return
    node = _structural_node(db, node_id)
    kids = _children(db, node_id)
    if child_id is None and len(kids) == 1:
        child_id = kids[0]
    if child_id not in kids:
        raise ConnectionTreeError("Choose the follower to swap with when a node has several.")
    child = _node(db, child_id)
    grandchildren = _children(db, child_id)
    siblings = _children(db, node["parent_id"])
    # An equal Kanal/Klemme beside the node (FI and Sicherung are unique, so they never have a twin).
    twin = None
    if child["node_type"] in ("channel", "terminal"):
        twin = _find_node(db, node["parent_id"], child["node_type"], child)
    try:
        upper_id = node_id
        if len(kids) > 1:
            # Copy of the node below the follower; the original keeps the other followers.
            upper_id = _insert_node(db, child_id, node["node_type"], node)
        # A Verbraucher stays the end point of the path: it moves down with the follower's other children onto the
        # node that is now last (a Klemme, or a Kanal with the Klemme above it). Only a Sicherung cannot carry it;
        # then it stays on its Klemme.
        if node["node_type"] not in ("terminal", "channel"):
            db.execute(f"UPDATE connection_nodes SET parent_id=? WHERE parent_id=? AND node_type NOT IN {SIDE_TYPES_SQL} AND id<>?",
                       (upper_id, child_id, upper_id))
        else:
            db.execute("UPDATE connection_nodes SET parent_id=? WHERE parent_id=? AND id<>?", (upper_id, child_id, upper_id))
        if twin is None:
            db.execute("UPDATE connection_nodes SET parent_id=? WHERE id=?", (child_id, upper_id))
            db.execute("UPDATE connection_nodes SET parent_id=? WHERE id=?", (node["parent_id"], child_id))
        else:
            # Merge: the node continues below the existing twin; the Verbraucher of the follower move there too.
            db.execute(f"UPDATE OR IGNORE connection_nodes SET parent_id=? WHERE parent_id=? AND node_type IN {SIDE_TYPES_SQL}",
                       (twin, child_id))
            db.execute("UPDATE connection_nodes SET parent_id=?, sort_order=? WHERE id=?",
                       (twin, _next_order(db, twin), upper_id))
            db.execute("DELETE FROM connection_nodes WHERE id=?", (child_id,))
    except sqlite3.IntegrityError as error:
        raise ConnectionTreeError("The same entry already exists at that position.") from error
    if upper_id != node_id:
        _renumber(db, node_id)
    if twin is not None:
        _renumber(db, node["parent_id"])
        _renumber(db, twin)
    elif upper_id == node_id:
        _write_order(db, [child_id if sibling == node_id else sibling for sibling in siblings])
    else:
        # The follower sits right behind the original node.
        position = siblings.index(node_id) + 1
        _write_order(db, siblings[:position] + [child_id] + siblings[position:])
    if twin is None:
        _write_order(db, [upper_id])
    _write_order(db, grandchildren)
    finish(db)


@_keeps_side_nodes
def drop_edge(db: sqlite3.Connection, node_id: int) -> None:
    """Cut the wire between a node and the node above it.

    A Kanal/Klemme wire is cut like a disconnected pair (the Klemme stays at its Sicherung).
    Otherwise the node leaves with everything below it and becomes a chain of its own.
    """
    node = _structural_node(db, node_id)
    if node["parent_id"] is None:
        raise ConnectionTreeError("This entry has no connection above it.")
    parent = _node(db, node["parent_id"])
    if {node["node_type"], parent["node_type"]} == {"channel", "terminal"}:
        _unlink_pair(db, node_id)
    else:
        try:
            _move_raw(db, node_id, None)
        except sqlite3.IntegrityError as error:
            raise ConnectionTreeError("The same entry already exists at the top level.") from error
        # A Klemme left without any wire (not even a Verbraucher or a following Klemme) needs no node.
        db.execute(
            """DELETE FROM connection_nodes WHERE id=? AND node_type='terminal'
               AND NOT EXISTS (SELECT 1 FROM connection_nodes child WHERE child.parent_id=connection_nodes.id)""",
            (node_id,),
        )
    finish(db)


def apply_action(db: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Run one reordering request: up, down, swap (with the only follower), move, order or drop."""
    action = str(payload.get("action", ""))

    def int_value(name: str, required: bool = True) -> int | None:
        value = payload.get(name)
        if value is None and not required:
            return None
        if isinstance(value, bool) or not str(value).lstrip("-").isdigit():
            raise ConnectionTreeError(f"Invalid {name}.")
        return int(value)

    if action in ("up", "down"):
        move_sibling(db, int_value("node_id"), -1 if action == "up" else 1)
    elif action == "swap":
        swap_with_child(db, int_value("node_id"), int_value("child_id", False))
    elif action == "drop":
        drop_edge(db, int_value("node_id"))
    elif action == "move":
        move_node(db, int_value("node_id"), int_value("parent_id", False), int_value("index", False))
    elif action == "order":
        ids = payload.get("ordered_ids")
        if not isinstance(ids, list) or not all(isinstance(item, int) and not isinstance(item, bool) for item in ids):
            raise ConnectionTreeError("Invalid ordered_ids.")
        reorder_children(db, int_value("parent_id", False), ids)
    else:
        raise ConnectionTreeError("Unknown connection action.")
    finish(db)
