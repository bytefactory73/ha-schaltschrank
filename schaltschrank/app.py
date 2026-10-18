from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sqlite3
import sys
import threading
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import connections
import home_assistant
from ets_import_common import (
    PHASES_COLUMN,
    SCHEMA,
    VALID_PHASES,
    backup_database,
    drop_legacy_schema,
    drop_terminal_fi_label,
    ensure_device_metadata_schema,
    join_phases,
    merge_fuse_phases,
    migrate_optional_fi_labels,
    normalize_fuse_label,
    upsert_fi_device,
)


APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR / "web"
DB_PATH = Path(os.environ.get("SCHALTSCHRANK_DB", APP_DIR / "schaltschrank8.sqlite3"))
# Set by `POST /api/restart`: after the server loop stops, `main` starts the app again.
RESTART_REQUESTED = threading.Event()
# Home Assistant add-on (`--ingress`): the server listens on all interfaces of the container, but answers only
# the Supervisor's ingress proxy (Home Assistant handles the login). None: localhost only, nothing to check.
INGRESS_CLIENTS = {"172.30.32.2"}
ALLOWED_CLIENTS: set[str] | None = None
# Options of the add-on ("Konfiguration" tab in Home Assistant), written by the Supervisor as JSON.
OPTIONS_PATH = Path(os.environ.get("SCHALTSCHRANK_OPTIONS", "/data/options.json"))
DEFAULT_TITLE = ""
DEVICE_ID = re.compile(r"\d+\.\d+\.\d+\Z")
WIRE_COLORS = {"grge", "br", "bk", "bl", "gr", "wh", "rd", "gn", "ye", "og", "vi"}
WIRE_COLOR_EXCEL_STYLES = {
    "grge": "A3C900",
    "br": "875437",
    "bk": "252A29",
    "bl": "3278BD",
    "gr": "929997",
    "wh": "FFFFFF",
    "rd": "CF3B35",
    "gn": "25804A",
    "ye": "E5CB2B",
    "og": "E88730",
    "vi": "754C9D",
}
MANUFACTURER_TRANSLATIONS = {
    "M-0001": "Siemens",
    "M-0006": "Berker",
    "M-0007": "Busch-Jaeger Elektro",
    "M-0008": "GIRA Giersiepen",
    "M-0009": "Hager Electro",
    "M-000C": "Merten",
    "M-004E": "Somfy",
    "M-0083": "MDT technologies",
    "M-00C8": "B.E.G.",
    "M-00E1": "Lingg & Janke",
}
POSITION_CODE = re.compile(r"[\w.-]{1,20}\Z")
PENDING_IMPORTS: dict[str, list[dict[str, object]]] = {}
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_UPLOAD_BYTES = 14 * 1024 * 1024
# Select column: the Klemmen of a channel (alias c) with their Sicherung and FI, derived from the connection tree.
TERMINALS_JSON_SQL = f"""COALESCE((SELECT json_group_array(json_object('assignment_id', a.id, 'terminal_id', t.id, 'terminal', t.label, 'terminal_number', t.terminal_number, 'position', t.position, 'wire_color', t.wire_color, 'used', t.used, 'location', l.name, 'fuse_id', f.id, 'fuse', f.label, 'fuse_rating', f.rating, 'fi_id', fi.id, 'fi_number', fi.number, 'fi_label', fi.label, 'fi_location', fil.name))
    FROM channel_terminals a JOIN terminals t ON t.id=a.terminal_id
    JOIN locations l ON l.id=t.location_id
    {connections.terminal_fuse_join('t', 'f')}
    {connections.fuse_fi_join('f', 'fi')} LEFT JOIN locations fil ON fil.id=fi.location_id
    WHERE a.channel_id=c.id), '[]') AS terminals_json"""


def normalize_search(value: object) -> str:
    text = "" if value is None else str(value)
    # Runs of spaces count as one (source texts like "Heizung  (Ofen)" carry double spaces).
    text = " ".join(text.split())
    return text.casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")


def compact_label(value: str) -> str:
    """A Klemmen label without "/" and spaces, for searching: "16/u" -> "16u"."""
    return value.replace("/", "").replace(" ", "")


# The same in SQL, on a Klemme's normalized label `t.label`.
COMPACT_LABEL_SQL = "replace(replace(search_normalize(t.label), '/', ''), ' ', '')"


# Words that mean the same in the sources (normalized, as stems): the data writes "Leuchte", "Beleuchtung" or
# "Lampe" where one searches "Licht". A search word containing one of them also finds the others of its group.
SEARCH_SYNONYMS = (
    ("licht", "leucht", "lampe", "strahler", "spot"),
    ("rollo", "rollad", "rolllad", "jalousie", "beschattung"),
    ("heizung", "heizkoerper", "heizkreis"),
)


def search_alternatives(word: str) -> list[str]:
    """The word itself and, when it contains a term of a synonym group, the other terms of that group."""
    group = next((group for group in SEARCH_SYNONYMS if any(term in word for term in group)), ())
    return list(dict.fromkeys((word, *group)))


def ha_search_result(item: dict[str, object]) -> dict[str, object]:
    """A Home Assistant entity as search hit: it opens its page (`home_assistant.target`, none when unmatched)."""
    place = " · ".join(filter(None, (item.get("device_label"), item.get("channel_label"),
                                     home_assistant.STATUS_LABELS[item["status"]] if not item["target_type"] else "")))
    return {"type": "homeassistant", "id": item["entity_id"], "title": item["name"],
            "subtitle": " · ".join(filter(None, (item["entity_id"], place))),
            "target_type": item["target_type"], "target_id": item["target_id"]}


def words_match(fields: list[str], words: list[str], compact_fields: tuple[str, ...] = ()) -> tuple[str, list[str]]:
    """SQL condition of the main search: every word of the query appears in one of the fields, in any order
    ("bad licht" finds "Bad : Leuchte Decke"; synonyms: `SEARCH_SYNONYMS`); `compact_fields` compare the word
    without "/" and spaces ("16u" finds the Klemme 16/u). Returns the condition and its parameters."""
    clauses: list[str] = []
    params: list[str] = []
    for word in words:
        alternatives = search_alternatives(word)
        clauses.append("(" + " OR ".join([f"{field} LIKE ?" for field in fields for _ in alternatives]
                                         + [f"{field} LIKE ?" for field in compact_fields]) + ")")
        params += [f"%{term}%" for _ in fields for term in alternatives] + [f"%{compact_label(word)}%"] * len(compact_fields)
    return " AND ".join(clauses) or "1=1", params


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.create_collation("NATURAL_SORT", natural_compare)
    db.create_function("search_normalize", 1, normalize_search)
    db.create_function("manufacturer_display", 1, manufacturer_display)
    return db


def rows_as_dicts(rows: list[sqlite3.Row]) -> list[dict[str, object]]:
    return [dict(row) for row in rows]


def manufacturer_display(value: str | None) -> str:
    code = (value or "").strip()
    return MANUFACTURER_TRANSLATIONS.get(code.upper(), code)


def natural_compare(left: str, right: str) -> int:
    left_key = connections.natural_key(left)
    right_key = connections.natural_key(right)
    if left_key != right_key:
        return (left_key > right_key) - (left_key < right_key)
    return (left.casefold() > right.casefold()) - (left.casefold() < right.casefold())


def make_terminal_label(number: str, position: str) -> str:
    return f"{number}/{position}" if position else number


def payload_id(payload: dict[str, object], name: str, label: str) -> int:
    """Integer id from a request payload; ValueError (HTTP 400) when it is missing or not a number."""
    value = payload.get(name)
    if isinstance(value, bool) or not str(value if value is not None else "").strip().isdigit():
        raise ValueError(f"Invalid {label} ID.")
    return int(str(value).strip())


def clean_text(value: object, label: str, limit: int = 500) -> str:
    """Form text with runs of whitespace collapsed; ValueError when it is longer than `limit`."""
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        raise ValueError(f"{label} must be {limit} characters or fewer.")
    return text


def payload_flag(payload: dict[str, object], key: str) -> bool:
    """A checkbox or boolean of a form payload (true, "on", "1", "true")."""
    value = payload.get(key)
    return value is True or str(value or "").strip().lower() in ("1", "true", "on", "yes")


def location_id_for(db: sqlite3.Connection, name: str) -> int:
    """Id of a location by name, created when it does not exist yet."""
    db.execute("INSERT INTO locations(name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,))
    return int(db.execute("SELECT id FROM locations WHERE name=?", (name,)).fetchone()[0])


def resolve_fuse_id(db: sqlite3.Connection, fuse_id: object, label: object, location_id: int | None = None) -> int | None:
    """The existing Sicherung of a Klemme form or import row; None when none is given.

    A Klemme may sit in another room than its Sicherung, so it is picked from all Sicherungen: by id, else by
    designation (the one in the Klemme's room wins, else it must be unique). Sicherungen are only created in
    their own dialog (with their room), never here, and their data (rating, ...) is left unchanged.
    """
    if str(fuse_id or "").strip().isdigit():
        row = db.execute("SELECT id FROM fuses WHERE id=?", (int(str(fuse_id).strip()),)).fetchone()
        if not row:
            raise ValueError("Sicherung not found.")
        return int(row[0])
    label = normalize_fuse_label(label or "")
    if not label:
        return None
    rows = db.execute("SELECT id, location_id FROM fuses WHERE label=?", (label,)).fetchall()
    here = [row[0] for row in rows if row[1] == location_id]
    if len(here) == 1:
        return int(here[0])
    if len(rows) == 1:
        return int(rows[0][0])
    if rows:
        raise ValueError(f"Sicherung {label} exists in several rooms; pick it from the list.")
    raise ValueError(f"Sicherung {label} does not exist; create it first (Sicherungen → Neue Sicherung).")


def phases_display(alias: str) -> str:
    """SQL expression showing the stored phases of a fuse (alias) as 'L1, L2'."""
    return f"REPLACE({alias}.phases, ',', ', ')"


def addon_option(name: str) -> object:
    """One option of the add-on ("Konfiguration" tab); None without options (locally) or when unset."""
    try:
        return json.loads(OPTIONS_PATH.read_text(encoding="utf-8")).get(name)
    except (OSError, ValueError, AttributeError):
        return None


def app_title() -> str:
    """The title line of the page: the add-on option `titel`, else SCHALTSCHRANK_TITLE, else none (the page then
    shows only "Schaltschrank")."""
    title = str(addon_option("titel") or "").strip()
    return title or os.environ.get("SCHALTSCHRANK_TITLE", "").strip() or DEFAULT_TITLE


def prune_backups() -> list[Path]:
    """Keeps only the newest backups when the add-on option `sicherungen` sets a number (the oldest go, by their
    timestamped names). Without the option nothing is removed: locally the backups are history."""
    keep = addon_option("sicherungen")
    if not isinstance(keep, int) or keep < 1:
        return []
    backups = sorted((DB_PATH.parent / "backups").glob(f"{DB_PATH.stem}.backup-*{DB_PATH.suffix}"))
    removed = backups[:-keep]
    for path in removed:
        path.unlink(missing_ok=True)
    return removed


def backup_and_prune() -> Path | None:
    """A verified backup of the database, then the old ones beyond the set number go (`prune_backups`)."""
    backup = backup_database(DB_PATH)
    prune_backups()
    return backup


def generation_error(db: sqlite3.Connection, name: object) -> str | None:
    """Why a database is not generation 8 (older generations are history, never migrated implicitly), else None."""
    if connections.has_legacy_link_tables(db) or connections.has_legacy_columns(db) \
            or not connections.has_wire_phase_schema(db):
        return (f"{name} is older than generation 7 and is kept as history only. "
                "Start the app on schaltschrank8.sqlite3.")
    if connections.has_routes(db):
        return (f"{name} still stores Klemme -> Klemme routes (generation 7). "
                "Run migrate_remove_routes.py to create the migrated copy and start the app on that.")
    return None


# Tables an imported database must have (a cabinet database of generation 8), and the ones counted for the answer.
IMPORT_TABLES = ("connection_nodes", "wire_phases", "locations", "terminals", "fuses", "fi_devices", "devices", "channels", "loads")
IMPORT_COUNTS = ("terminals", "fuses", "devices", "loads")


def check_import_database(path: Path) -> dict[str, int]:
    """Checks an uploaded database read-only: intact, generation 8, the cabinet tables. Returns a few counts;
    raises ValueError with the reason otherwise."""
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Die Datenbank ist beschädigt (integrity_check).")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [table for table in IMPORT_TABLES if table not in tables]
            if missing:
                raise ValueError(f"Keine Schaltschrank-Datenbank: es fehlen {', '.join(missing)}.")
            error = generation_error(db, "Die Datei")
            if error:
                raise ValueError(error)
            return {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in IMPORT_COUNTS}
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"Die Datei lässt sich nicht als Datenbank öffnen: {exc}") from exc


def create_empty_database(path: Path) -> None:
    """A new database with the schema and the five conductors, nothing else (first start of the add-on; the
    data then comes in with "Datenbank importieren")."""
    with closing(sqlite3.connect(path)) as db:
        db.executescript(SCHEMA)
        ensure_device_metadata_schema(db)
        connections.ensure_schema(db)
        db.commit()


def initialize_database() -> None:
    with sqlite3.connect(DB_PATH) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # Never migrate implicitly: a generation change writes a separate verified copy.
        error = generation_error(db, DB_PATH)
        if error:
            raise SystemExit(error)
        migrate_optional_fi_labels(db)
        if "switchboards" in tables and "locations" not in tables:
            db.execute("ALTER TABLE switchboards RENAME TO locations")
            tables.add("locations")
        if "terminals" in tables:
            columns = {row[1] for row in db.execute("PRAGMA table_info(terminals)")}
            if "switchboard_id" in columns and "location_id" not in columns:
                db.execute("ALTER TABLE terminals RENAME COLUMN switchboard_id TO location_id")
        db.executescript(
            f"""CREATE TABLE IF NOT EXISTS locations (
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
               CREATE INDEX IF NOT EXISTS idx_channels_device_physical ON channels(device_id, physical_channel, object_number);"""
        )
        ensure_device_metadata_schema(db)
        merge_fuse_phases(db)
        connections.ensure_schema(db)
        drop_legacy_schema(db)
        location_columns = {row[1] for row in db.execute("PRAGMA table_info(locations)")}
        if "description" not in location_columns:
            db.execute("ALTER TABLE locations ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        terminal_columns = {row[1] for row in db.execute("PRAGMA table_info(terminals)")}
        if "terminal_number" not in terminal_columns:
            db.execute("ALTER TABLE terminals ADD COLUMN terminal_number TEXT NOT NULL DEFAULT ''")
        if "position" not in terminal_columns:
            db.execute("ALTER TABLE terminals ADD COLUMN position TEXT NOT NULL DEFAULT ''")
        if "wire_color" not in terminal_columns:
            db.execute("ALTER TABLE terminals ADD COLUMN wire_color TEXT NOT NULL DEFAULT ''")
        drop_terminal_fi_label(db)
        if "used" not in terminal_columns:
            db.execute("ALTER TABLE terminals ADD COLUMN used INTEGER NOT NULL DEFAULT 1 CHECK (used IN (0, 1))")
        if "channels" in tables:
            channel_columns = {row[1] for row in db.execute("PRAGMA table_info(channels)")}
            if "physical_channel" not in channel_columns:
                db.execute("ALTER TABLE channels ADD COLUMN physical_channel TEXT NOT NULL DEFAULT ''")
        for table in ("devices", "channels", "terminals", "fuses", "fi_devices"):
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if "reviewed_at" not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN reviewed_at TEXT")
        # Only additive schema changes here; existing data is never rewritten on start.


class Handler(BaseHTTPRequestHandler):
    server_version = "SchaltschrankBrowser/1.0"

    def send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_UPLOAD_BYTES:
            raise ValueError("Request is larger than 10 MB")
        return json.loads(self.rfile.read(length) or b"{}")

    def client_allowed(self) -> bool:
        """In the add-on only the ingress proxy may talk to the server; everybody else gets 403."""
        if ALLOWED_CLIENTS is None or self.client_address[0] in ALLOWED_CLIENTS:
            return True
        self.send_json({"error": "Forbidden"}, 403)
        return False

    def do_GET(self) -> None:
        if not self.client_allowed():
            return
        # Every failure answers with JSON (like do_POST), never with a dropped connection.
        try:
            self.handle_get()
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def handle_get(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/api/stats":
            with connect() as db:
                counts = {
                    table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("devices", "channels", "group_addresses", "channel_group_addresses", "locations", "terminals", "fuses", "loads")
                }
                counts["assignments"] = db.execute("SELECT COUNT(*) FROM channel_terminals").fetchone()[0]
            # Only when Home Assistant files are there: the start page shows the overview card then.
            if home_assistant.available():
                counts["ha_entities"] = len(home_assistant.panel_entities())
            self.send_json(counts)
            return
        if parsed.path == "/api/locations":
            with connect() as db:
                locations = rows_as_dicts(db.execute("SELECT name FROM locations ORDER BY name").fetchall())
            self.send_json(locations)
            return
        if parsed.path == "/api/terminals":
            number = params.get("number", [""])[0].strip()
            location = params.get("location", [""])[0].strip()
            with connect() as db:
                terminals = rows_as_dicts(db.execute(
                    """SELECT t.id, t.label, t.terminal_number, t.position, t.wire_color, t.used, l.name AS location
                       FROM terminals t JOIN locations l ON l.id=t.location_id
                              WHERE t.terminal_number LIKE ? AND (?='' OR l.name=?)
                              ORDER BY CASE WHEN t.terminal_number=? THEN 0 ELSE 1 END,
                                          l.name COLLATE NATURAL_SORT,
                                t.position COLLATE NATURAL_SORT,
                                t.label COLLATE NATURAL_SORT LIMIT 50""",
                          (f"{number}%", location, location, number),
                ).fetchall()) if number else []
            self.send_json(terminals)
            return
        if parsed.path == "/api/channels":
            self.channel_options(params.get("q", [""])[0])
            return
        if parsed.path == "/api/loads":
            self.load_options(params.get("q", [""])[0])
            return
        if parsed.path == "/api/overview":
            self.overview(params.get("category", [""])[0])
            return
        if parsed.path == "/api/fuses":
            location = params.get("location", [""])[0].strip()
            with connect() as db:
                fuses = rows_as_dicts(db.execute(
                        f"""SELECT f.id, f.label, f.rating, l.name AS location,
                                  fi.number AS fi_number, fi.label AS fi_label
                              FROM fuses f
                       JOIN locations l ON l.id=f.location_id
                              {connections.fuse_fi_join('f', 'fi')}
                       WHERE ? = '' OR l.name=? ORDER BY l.name, f.label""",
                    (location, location),
                ).fetchall())
            self.send_json(fuses)
            return
        if parsed.path == "/api/neutrals":
            # N and the FI neutrals, for connecting something directly to a neutral (chosen, never guessed).
            with connect() as db:
                self.send_json(connections.neutral_options(db))
            return
        if parsed.path == "/api/fi-devices":
            location_id = params.get("location_id", [""])[0].strip()
            location_name = params.get("location", [""])[0].strip()
            if not location_id.isdigit() and not location_name:
                self.send_json({"error": "A valid location_id or location is required."}, 400)
                return
            with connect() as db:
                if location_id.isdigit():
                    fi_rows = db.execute(
                        """SELECT id, number, label FROM fi_devices
                           WHERE location_id=? ORDER BY label, number""",
                        (int(location_id),),
                    )
                else:
                    fi_rows = db.execute(
                        """SELECT fi.id, fi.number, fi.label FROM fi_devices fi
                           JOIN locations l ON l.id=fi.location_id
                           WHERE l.name=? ORDER BY fi.label, fi.number""",
                        (location_name,),
                    )
                fi_devices = rows_as_dicts(fi_rows.fetchall())
            self.send_json(fi_devices)
            return
        if parsed.path == "/api/search":
            self.search(params.get("q", [""])[0])
            return
        if parsed.path == "/api/record":
            self.record(params.get("type", [""])[0], params.get("id", [""])[0])
            return
        if parsed.path == "/api/connections":
            self.connections_for_record(params.get("type", [""])[0], params.get("id", [""])[0])
            return
        if parsed.path == "/api/graph":
            with connect() as db:
                self.send_json(connections.graph_payload(db))
            return
        if parsed.path == "/api/import/template":
            self.template()
            return
        if parsed.path == "/api/export/klemmen":
            self.export_terminals()
            return
        if parsed.path == "/api/database/export":
            self.export_database()
            return
        if parsed.path == "/api/homeassistant/target":
            # Where a Home Assistant entity opens (the Schaltschrank symbol in its dialog): channel, else device.
            entity_id = params.get("entity_id", [""])[0]
            with connect() as db:
                item = next((item for item in home_assistant.mapped_entities(db) if item["entity_id"] == entity_id), None)
            if not item:
                self.send_json({"error": f"{entity_id} ist keine KNX-Entität aus dem KNX-Panel."}, 404)
                return
            self.send_json(ha_search_result(item))
            return
        if parsed.path == "/":
            self.send_static("index.html")
            return
        if parsed.path in ("/app.js", "/styles.css"):
            self.send_static(parsed.path.lstrip("/"))
            return
        self.send_json({"error": "Not found"}, 404)

    def load_options(self, query: str) -> None:
        """Verbraucher lookup for the path-add dialog: name, location and cable (Klemmen number)."""
        normalized_query = normalize_search(query.strip())
        if not normalized_query:
            self.send_json([])
            return
        with connect() as db:
            rows = rows_as_dicts(db.execute(
                """SELECT ld.id, ld.name, ld.terminal_number, l.name AS location FROM loads ld
                   JOIN locations l ON l.id=ld.location_id
                   WHERE search_normalize(ld.name || ' ' || l.name || ' ' || ld.terminal_number) LIKE ?
                   ORDER BY ld.name COLLATE NATURAL_SORT LIMIT 30""",
                (f"%{normalized_query}%",),
            ).fetchall())
        self.send_json(rows)

    def channel_options(self, query: str) -> None:
        """Compact channel lookup (one entry per physical channel) for pickers."""
        normalized_query = normalize_search(query.strip())
        if not normalized_query:
            self.send_json([])
            return
        like = f"%{normalized_query}%"
        with connect() as db:
            rows = db.execute(
                """WITH matching AS (
                       SELECT c.device_id, c.object_number, c.physical_channel, c.name, c.function,
                              COALESCE(NULLIF(d.manual_name, ''), c.device_id) AS device,
                              d.product,
                              ROW_NUMBER() OVER (
                                  PARTITION BY c.device_id, COALESCE(NULLIF(c.physical_channel, ''), 'object:' || c.object_number)
                                  ORDER BY c.object_number COLLATE NATURAL_SORT
                              ) AS channel_rank
                       FROM channels c JOIN devices d ON d.device_id=c.device_id
                       WHERE search_normalize(c.device_id) LIKE ? OR search_normalize(d.manual_name) LIKE ?
                          OR search_normalize(c.physical_channel) LIKE ? OR search_normalize(c.name) LIKE ?
                          OR search_normalize(d.product) LIKE ?
                   )
                   SELECT * FROM matching WHERE channel_rank=1
                   ORDER BY device COLLATE NATURAL_SORT, object_number COLLATE NATURAL_SORT LIMIT 30""",
                (like, like, like, like, like),
            ).fetchall()
        self.send_json([{
            "device_id": row["device_id"],
            "object_number": row["object_number"],
            "title": f"{row['device']}  /  " + (f"Kanal {row['physical_channel']}" if row["physical_channel"] else f"ETS-Objekt {row['object_number']}"),
            "subtitle": " · ".join(filter(None, (row["name"], row["function"], row["product"]))),
        } for row in rows])

    def search(self, query: str) -> None:
        query = query.strip()
        normalized_query = normalize_search(query)
        not_reviewed_term = re.compile(r"(?<!\w)nicht\s+ueberprueft(?!\w)")
        reviewed_term = re.compile(r"(?<!\w)ueberprueft(?!\w)")
        review_filter: bool | None = None
        if not_reviewed_term.search(normalized_query):
            review_filter = False
            normalized_query = reviewed_term.sub(" ", not_reviewed_term.sub(" ", normalized_query))
        elif reviewed_term.search(normalized_query):
            review_filter = True
            normalized_query = reviewed_term.sub(" ", normalized_query)
        normalized_query = " ".join(normalized_query.split())
        if not normalized_query and review_filter is None:
            self.send_json([])
            return
        complete_search = review_filter is not None or len(normalized_query) >= 3
        device_limit = "" if complete_search else "LIMIT 20"
        channel_limit = "" if complete_search else "LIMIT 25"
        terminal_limit = "" if complete_search else "LIMIT 20"
        fuse_limit = "" if complete_search else "LIMIT 20"
        fi_limit = "" if complete_search else "LIMIT 20"
        other_limit = "" if complete_search else "LIMIT 20"
        like = f"%{normalized_query}%"
        prefix = f"{normalized_query}%"
        # A Klemme is also found typed without "/" and spaces ("16u" finds 16/u).
        compact_query = compact_label(normalized_query)
        # Every word must be found, in any order and in any field of the entry.
        words = normalized_query.split()
        results: list[dict[str, object]] = []
        with connect() as db:
            where, params = words_match(["search_normalize(device_id)", "search_normalize(manual_name)",
                                         "search_normalize(product)", "search_normalize(manufacturer)",
                                         "search_normalize(manufacturer_display(manufacturer))",
                                         "search_normalize(description)", "search_normalize(location)"], words)
            for row in db.execute(
                     f"""SELECT device_id AS id,
                                  CASE WHEN COALESCE(is_manual, 0)=1 THEN manual_name ELSE device_id END AS title,
                                  product, manufacturer, location,
                                  CASE WHEN COALESCE(is_manual, 0)=1 THEN 'Manuell' ELSE '' END AS manual_subtitle
                   FROM devices
                   WHERE {where} AND (? IS NULL OR (reviewed_at IS NOT NULL)=?)
                   ORDER BY CASE WHEN search_normalize(device_id)=? OR search_normalize(manual_name)=? THEN 0
                                 WHEN search_normalize(device_id) LIKE ? OR search_normalize(manual_name) LIKE ? THEN 1 ELSE 2 END, title
                   {device_limit}""",
                (*params, review_filter, review_filter, normalized_query, normalized_query, prefix, prefix),
            ):
                item = dict(row)
                item["subtitle"] = " · ".join(filter(None, (
                    item.pop("manual_subtitle"),
                    item.pop("product"),
                    manufacturer_display(item.pop("manufacturer")),
                    item.pop("location"),
                )))
                results.append({"type": "device", **item})
            where, params = words_match(["search_normalize(address)", "search_normalize(name)"], words)
            for row in db.execute(
                f"""SELECT address AS id, address AS title,
                          TRIM(COALESCE(name, '') || CASE WHEN length <> '' THEN '  ' || length ELSE '' END) AS subtitle
                   FROM group_addresses WHERE {where} AND ? IS NULL
                   ORDER BY CASE WHEN search_normalize(address)=? THEN 0
                                 WHEN search_normalize(address) LIKE ? THEN 1 ELSE 2 END, address
                   {other_limit}""",
                (*params, review_filter, normalized_query, prefix),
            ):
                results.append({"type": "group", **dict(row)})
            # A Kanal is also found by its device's name and the name of its sending group address.
            channel_where, channel_params = words_match(
                ["search_normalize(c.device_id)", "search_normalize(d.manual_name)", "search_normalize(c.object_number)",
                 "search_normalize(c.physical_channel)", "search_normalize(c.name)", "search_normalize(c.function)",
                 "search_normalize(d.product)", "search_normalize(first_send_groups.name)"], words)
            for row in db.execute(
                f"""WITH first_send_groups AS (
                       SELECT c.device_id,
                              COALESCE(NULLIF(c.physical_channel, ''), 'object:' || c.object_number) AS channel_key,
                              g.name,
                              ROW_NUMBER() OVER (
                                  PARTITION BY c.device_id, COALESCE(NULLIF(c.physical_channel, ''), 'object:' || c.object_number)
                                  ORDER BY g.address COLLATE NATURAL_SORT, c.object_number COLLATE NATURAL_SORT
                              ) AS send_group_rank
                       FROM channels c
                       JOIN channel_group_addresses x ON x.channel_id=c.id
                       JOIN group_addresses g ON g.address=x.address
                       WHERE instr('/' || x.direction || '/', '/S/') > 0
                         AND trim(g.name) <> ''
                   ), matching_channels AS (
                       SELECT CAST(c.device_id || ':' || c.object_number AS TEXT) AS id,
                              COALESCE(NULLIF(d.manual_name, ''), c.device_id) || '  /  ' || CASE
                                  WHEN c.physical_channel <> '' THEN 'Kanal ' || c.physical_channel
                                  ELSE 'ETS-Objekt ' || c.object_number
                              END AS title,
                              COALESCE(first_send_groups.name,
                                       TRIM(COALESCE(c.name, '') || CASE WHEN c.function <> '' THEN '  ' || c.function ELSE '' END)) AS subtitle,
                              c.device_id, c.object_number,
                              ROW_NUMBER() OVER (
                                  PARTITION BY c.device_id, COALESCE(NULLIF(c.physical_channel, ''), 'object:' || c.object_number)
                                  ORDER BY c.object_number COLLATE NATURAL_SORT
                              ) AS channel_rank
                       FROM channels c JOIN devices d ON d.device_id=c.device_id
                       LEFT JOIN first_send_groups
                         ON first_send_groups.device_id=c.device_id
                        AND first_send_groups.channel_key=COALESCE(NULLIF(c.physical_channel, ''), 'object:' || c.object_number)
                        AND first_send_groups.send_group_rank=1
                       WHERE {channel_where} AND (? IS NULL OR (c.reviewed_at IS NOT NULL)=?)
                   )
                   SELECT id, title, subtitle FROM matching_channels
                   WHERE channel_rank=1
                   ORDER BY device_id COLLATE NATURAL_SORT, object_number COLLATE NATURAL_SORT
                   {channel_limit}""",
                (*channel_params, review_filter, review_filter),
            ):
                results.append({"type": "channel", **dict(row)})
            where, params = words_match(["search_normalize(t.label)", "search_normalize(t.description)",
                                         "search_normalize(l.name)"], words, (COMPACT_LABEL_SQL,))
            for row in db.execute(
                f"""SELECT CAST(t.id AS TEXT) AS id, t.label AS title, t.wire_color AS wire_color, t.used AS used,
                          l.name || CASE WHEN t.description <> '' THEN '  ' || t.description ELSE '' END AS subtitle
                   FROM terminals t JOIN locations l ON l.id=t.location_id
                   WHERE {where} AND (? IS NULL OR (t.reviewed_at IS NOT NULL)=?)
                   ORDER BY CASE WHEN search_normalize(t.label)=? OR {COMPACT_LABEL_SQL}=? THEN 0
                                 WHEN search_normalize(t.label) LIKE ? THEN 1
                                 WHEN search_normalize(t.label) LIKE ? THEN 2 ELSE 3 END,
                            l.name, t.label {terminal_limit}""",
                (*params, review_filter, review_filter, normalized_query, compact_query, prefix, like),
            ):
                results.append({"type": "terminal", **dict(row)})
            fi_text = ["search_normalize(fi.number)", "search_normalize(fi.label)",
                       "search_normalize('FI ' || COALESCE(fi.number, '') || ' ' || COALESCE(fi.label, ''))"]
            fuse_where, fuse_params = words_match(["search_normalize(f.label)", "search_normalize(f.rating)",
                                                   "search_normalize(f.description)", "search_normalize(l.name)", *fi_text], words)
            for row in db.execute(
                f"""SELECT CAST(f.id AS TEXT) AS id, f.label AS title,
                         TRIM(l.name || CASE WHEN f.rating <> '' THEN ' · ' || f.rating ELSE '' END
                             || CASE WHEN fi.number IS NOT NULL AND fi.number<>'' THEN ' · FI ' || fi.number ELSE '' END
                             || CASE WHEN fi.label <> '' THEN ' · ' || fi.label ELSE '' END) AS subtitle
                   FROM fuses f JOIN locations l ON l.id=f.location_id
                   {connections.fuse_fi_join('f', 'fi')}
                   WHERE {fuse_where} AND (? IS NULL OR (f.reviewed_at IS NOT NULL)=?)
                    ORDER BY CASE WHEN search_normalize(f.label) LIKE ?
                                 OR search_normalize('FI ' || COALESCE(fi.number, '') || ' ' || COALESCE(fi.label, '')) LIKE ?
                                 OR search_normalize('FI ' || COALESCE(fi.label, '') || ' ' || COALESCE(fi.number, '')) LIKE ? THEN 0 ELSE 1 END,
                            l.name COLLATE NATURAL_SORT, f.label COLLATE NATURAL_SORT {fuse_limit}""",
                (*fuse_params, review_filter, review_filter, like, like, like),
            ):
                results.append({"type": "fuse", **dict(row)})
            fi_where, fi_params = words_match([*fi_text, "search_normalize(l.name)"], words)
            for row in db.execute(
                f"""SELECT CAST(fi.id AS TEXT) AS id,
                          CASE WHEN fi.number IS NOT NULL AND fi.number<>'' THEN 'FI ' || fi.number ELSE 'FI' END AS title,
                          TRIM(fi.label || ' · ' || l.name || ' · ' || COUNT(DISTINCT f.id) || ' Sicherungen') AS subtitle
                   FROM fi_devices fi
                   JOIN locations l ON l.id=fi.location_id
                   LEFT JOIN fuse_fi fl ON fl.fi_id=fi.id
                   LEFT JOIN fuses f ON f.id=fl.fuse_id
                   WHERE {fi_where} AND (? IS NULL OR (fi.reviewed_at IS NOT NULL)=?)
                   GROUP BY fi.id
                 ORDER BY CASE WHEN search_normalize(fi.number) LIKE ? OR search_normalize(fi.label) LIKE ?
                           OR search_normalize('FI ' || COALESCE(fi.number, '') || ' ' || COALESCE(fi.label, '')) LIKE ?
                           OR search_normalize('FI ' || COALESCE(fi.label, '') || ' ' || COALESCE(fi.number, '')) LIKE ? THEN 0 ELSE 1 END,
                            l.name COLLATE NATURAL_SORT, fi.number COLLATE NATURAL_SORT, fi.label COLLATE NATURAL_SORT
                   {fi_limit}""",
                (*fi_params, review_filter, review_filter, like, like, like, like),
            ):
                results.append({"type": "fi", **dict(row)})
            where, params = words_match(["search_normalize(l.name)", "search_normalize(l.description)"], words)
            for row in db.execute(
                f"""SELECT CAST(l.id AS TEXT) AS id, l.name AS title,
                          CAST(COUNT(DISTINCT t.id) AS TEXT) || ' Klemmen' AS subtitle
                   FROM locations l LEFT JOIN terminals t ON t.location_id=l.id
                   WHERE {where} AND ? IS NULL
                   GROUP BY l.id ORDER BY l.name {other_limit}""",
                (*params, review_filter),
            ):
                results.append({"type": "location", **dict(row)})
            # Verbraucher: by name, description, room and cable (Klemmen number); they open on their own page.
            where, params = words_match(["search_normalize(ld.name)", "search_normalize(ld.description)",
                                         "search_normalize(l.name)", "search_normalize('Kabel ' || ld.terminal_number)"], words)
            for row in db.execute(
                f"""SELECT CAST(ld.id AS TEXT) AS id, ld.name AS title,
                          l.name || CASE WHEN ld.terminal_number <> '' THEN ' · Kabel ' || ld.terminal_number ELSE '' END AS subtitle
                   FROM loads ld JOIN locations l ON l.id=ld.location_id
                   WHERE {where} AND (? IS NULL OR (ld.reviewed_at IS NOT NULL)=?)
                   ORDER BY CASE WHEN search_normalize(ld.name) LIKE ? THEN 0 ELSE 1 END,
                            ld.name COLLATE NATURAL_SORT {other_limit}""",
                (*params, review_filter, review_filter, prefix),
            ):
                results.append({"type": "load", **dict(row)})
            # Home Assistant entities by name and entity_id; a hit opens the channel or device they belong to.
            if review_filter is None and home_assistant.available():
                for item in home_assistant.mapped_entities(db):
                    text = normalize_search(f"{item['name']} {item['entity_id']}")
                    if words and all(any(term in text for term in search_alternatives(word)) for word in words):
                        results.append(ha_search_result(item))
        category_types = [
            ("device",),
            ("channel",),
            ("group",),
            ("terminal",),
            ("load",),
            ("fuse",),
            ("fi",),
            ("location",),
            ("homeassistant",),
        ]
        buckets = [
            [item for item in results if item["type"] in types]
            for types in category_types
        ]
        balanced_results: list[dict[str, object]] = []
        round_count = max((len(bucket) for bucket in buckets), default=0) if complete_search else 10
        for index in range(round_count):
            for bucket in buckets:
                if index < len(bucket):
                    balanced_results.append(bucket[index])
        self.send_json(balanced_results if complete_search else balanced_results[:70])

    def overview(self, category: str) -> None:
        if category == "loads":
            self.send_json(self.load_overview())
            return
        if category == "homeassistant":
            # KNX entities of Home Assistant with their place in the cabinet and, in the add-on, their state.
            with connect() as db:
                self.send_json([{"type": "homeassistant", **item} for item in home_assistant.mapped_entities(db, states=True)])
            return
        queries = {
                                     "devices": ("device", """SELECT device_id AS id,
                                                 CASE WHEN COALESCE(is_manual, 0)=1 THEN manual_name ELSE device_id END AS title,
                                                 CASE WHEN COALESCE(is_manual, 0)=1 THEN 'Manuell' ELSE '' END AS subtitle,
                                                 manual_name, is_manual, product, manufacturer, location, reviewed_at
                                             FROM devices ORDER BY title COLLATE NATURAL_SORT"""),
                  "channels": ("channel", """SELECT id, device_id, object_number, physical_channel, reviewed_at, product, manual_name, is_manual
                             FROM (
                                 SELECT CAST(c.device_id || ':' || c.object_number AS TEXT) AS id,
                                 c.device_id, c.object_number, c.physical_channel, c.reviewed_at, d.product, d.manual_name, d.is_manual,
                                        ROW_NUMBER() OVER (
                                            PARTITION BY c.device_id, COALESCE(NULLIF(c.physical_channel, ''), 'object:' || c.object_number)
                                            ORDER BY c.object_number COLLATE NATURAL_SORT
                                        ) AS channel_rank
                                 FROM channels c JOIN devices d ON d.device_id=c.device_id
                             )
                             WHERE channel_rank=1
                             ORDER BY device_id COLLATE NATURAL_SORT, object_number COLLATE NATURAL_SORT"""),
            "groups": ("group", """SELECT address AS id, address AS title,
                               TRIM(COALESCE(name, '') || CASE WHEN length <> '' THEN ' · ' || length ELSE '' END) AS subtitle
                        FROM group_addresses ORDER BY address COLLATE NATURAL_SORT"""),
            "terminals": ("terminal", f"""SELECT CAST(t.id AS TEXT) AS id, 'Klemme ' || t.label AS title,
                              l.name AS subtitle, t.terminal_number, t.position, t.label AS terminal_label,
                                  t.description AS element_description, t.used,
                              t.wire_color AS wire_color, t.reviewed_at, f.id AS fuse_id, f.label AS fuse_label,
                              COALESCE((
                                  SELECT json_group_array(json_object(
                                      'device_id', channel.device_id,
                                      'device_label', channel.device_label,
                                      'physical_channel', channel.physical_channel,
                                      'object_number', channel.object_number
                                  ))
                                  FROM (
                                       SELECT link.device_id, link.physical_channel,
                                           COALESCE(NULLIF(d.manual_name, ''), d.device_id) AS device_label,
                                             (SELECT c.object_number FROM channels c
                                              WHERE c.device_id=link.device_id AND c.physical_channel=link.physical_channel
                                              ORDER BY CAST(c.object_number AS INTEGER), c.object_number LIMIT 1) AS object_number
                                      FROM terminal_channel_links link JOIN devices d ON d.device_id=link.device_id
                                      WHERE link.terminal_id=t.id
                                      GROUP BY link.device_id, link.physical_channel
                                  ) channel
                              ), '[]') AS device_channels_json
                          FROM terminals t JOIN locations l ON l.id=t.location_id
                          {connections.terminal_fuse_join('t', 'f')}
                           ORDER BY l.name COLLATE NATURAL_SORT, t.terminal_number COLLATE NATURAL_SORT,
                                    t.position COLLATE NATURAL_SORT, t.label COLLATE NATURAL_SORT"""),
            "locations": ("location", """SELECT CAST(l.id AS TEXT) AS id, l.name AS title, l.description,
                                  TRIM(COALESCE(l.description, '') || CASE WHEN COUNT(DISTINCT t.id) > 0 THEN ' · ' || COUNT(DISTINCT t.id) || ' Klemmen' ELSE '' END) AS subtitle
                           FROM locations l LEFT JOIN terminals t ON t.location_id=l.id
                           GROUP BY l.id ORDER BY l.name COLLATE NATURAL_SORT"""),
            "fuses": ("fuse", f"""SELECT CAST(f.id AS TEXT) AS id, f.label AS title,
                                                            l.name AS location, f.rating AS rating,
                                                            fi.id AS fi_id, f.reviewed_at,
                                                            NULLIF({phases_display('f')}, '') AS phases,
                                                            TRIM(l.name || CASE WHEN f.rating <> '' THEN ' · ' || f.rating ELSE '' END
                                                                     || CASE WHEN fi.number IS NOT NULL AND fi.number<>'' THEN ' · FI ' || fi.number ELSE '' END
                                                                || CASE WHEN fi.label IS NOT NULL AND fi.label<>'' THEN ' · ' || fi.label ELSE '' END
                                                                     || CASE WHEN f.phases<>'' THEN ' · ' || {phases_display('f')} ELSE '' END) AS subtitle
                                             FROM fuses f JOIN locations l ON l.id=f.location_id
                                             {connections.fuse_fi_join('f', 'fi')}
                       ORDER BY l.name COLLATE NATURAL_SORT, f.label COLLATE NATURAL_SORT"""),
        }
        if category not in queries:
            self.send_json({"error": "Unknown overview category."}, 400)
            return
        record_type, query = queries[category]
        with connect() as db:
            items = rows_as_dicts(db.execute(query).fetchall())
        if category == "devices":
            for item in items:
                item["title"] = item.get("manual_name") or item["title"]
                item["subtitle"] = " · ".join(filter(None, (
                    "Manuell" if item.get("is_manual") else "",
                    item.pop("product"),
                    manufacturer_display(item.pop("manufacturer")),
                    item.pop("location"),
                )))
        elif category == "channels":
            with connect() as db:
                self.attach_send_group_names(db, items)
            for item in items:
                channel_label = f"Kanal {item['physical_channel']}" if item["physical_channel"] else f"ETS-Objekt {item['object_number']}"
                device_label = item.get("manual_name") or item["device_id"]
                item["title"] = f"{device_label} · {channel_label}"
                item["subtitle"] = " · ".join(filter(None, (
                    item.get("send_group_name"),
                    item.get("product"),
                    "Manuell" if item.get("is_manual") else "",
                )))
        elif category == "terminals":
            with connect() as db:
                device_channels = []
                for item in items:
                    item["device_channels"] = json.loads(item.pop("device_channels_json") or "[]")
                    device_channels.extend(item["device_channels"])
                self.attach_send_group_names(db, device_channels, linked_only=True)
        self.send_json([{"type": record_type, **item} for item in items])

    def record(self, record_type: str, record_id: str) -> None:
        with connect() as db:
            if record_type == "device":
                device = db.execute("SELECT * FROM devices WHERE device_id=?", (record_id,)).fetchone()
                if not device:
                    self.send_json({"error": "Device not found"}, 404)
                    return
                device_record = dict(device)
                device_record["manufacturer_display"] = manufacturer_display(device_record.get("manufacturer"))
                channels = db.execute(
                    f"""SELECT c.id, c.device_id, c.object_number, c.physical_channel, c.reviewed_at, c.name, c.function, c.description, c.data_type, c.priority, c.flags,
                              d.is_manual AS manual_channel,
                              COALESCE((SELECT json_group_array(json_object('address', g.address, 'name', g.name, 'direction', x.direction))
                                        FROM channel_group_addresses x JOIN group_addresses g ON g.address=x.address
                                        WHERE x.channel_id=c.id), '[]') AS groups_json,
                              {TERMINALS_JSON_SQL}
                       FROM channels c JOIN devices d ON d.device_id=c.device_id
                       WHERE c.device_id=? ORDER BY CAST(c.object_number AS INTEGER), c.object_number""",
                    (record_id,),
                ).fetchall()
                channels = self.decode_channel_rows(channels)
                self.attach_physical_channels(db, channels)
                self.attach_send_group_names(db, channels)
                self.send_json({"type": record_type, "record": device_record, "channels": channels,
                                "home_assistant": home_assistant.mapped_entities(db, states=True, device_id=record_id)})
                return

            if record_type == "group":
                group = db.execute("SELECT * FROM group_addresses WHERE address=?", (record_id,)).fetchone()
                if not group:
                    self.send_json({"error": "Group address not found"}, 404)
                    return
                channels = db.execute(
                    f"""SELECT c.id, c.device_id, c.object_number, c.physical_channel, c.name, c.function, c.data_type,
                              d.product, x.direction,
                              {TERMINALS_JSON_SQL}
                       FROM channel_group_addresses x JOIN channels c ON c.id=x.channel_id
                       JOIN devices d ON d.device_id=c.device_id WHERE x.address=?
                       ORDER BY c.device_id, CAST(c.object_number AS INTEGER)""",
                    (record_id,),
                ).fetchall()
                channels = self.decode_channel_rows(channels)
                self.attach_physical_channels(db, channels)
                self.attach_send_group_names(db, channels)
                self.send_json({"type": record_type, "record": dict(group), "channels": channels})
                return

            if record_type == "channel":
                parts = record_id.rsplit(":", 1)
                if len(parts) != 2:
                    self.send_json({"error": "Invalid channel ID"}, 400)
                    return
                channel = db.execute(
                    """SELECT c.*, d.product, d.manufacturer, d.location, d.manual_name, d.is_manual AS manual_channel
                       FROM channels c JOIN devices d ON d.device_id=c.device_id
                       WHERE c.device_id=? AND c.object_number=?""",
                    parts,
                ).fetchone()
                if not channel:
                    self.send_json({"error": "Channel not found"}, 404)
                    return
                channel_record = dict(channel)
                self.attach_send_group_names(db, [channel_record])
                if channel["physical_channel"]:
                    channel_filter = "c.device_id=? AND c.physical_channel=?"
                    channel_params = (channel["device_id"], channel["physical_channel"])
                else:
                    channel_filter = "c.id=?"
                    channel_params = (channel["id"],)
                channel_rows = db.execute(
                    f"""SELECT c.id, c.device_id, c.object_number, c.physical_channel, c.name, c.function,
                               d.is_manual AS manual_channel,
                               c.description, c.data_type, c.priority, c.flags,
                               COALESCE((SELECT json_group_array(json_object('address', g.address, 'name', g.name, 'direction', x.direction))
                                         FROM channel_group_addresses x JOIN group_addresses g ON g.address=x.address
                                         WHERE x.channel_id=c.id), '[]') AS groups_json,
                               {TERMINALS_JSON_SQL}
                        FROM channels c JOIN devices d ON d.device_id=c.device_id WHERE {channel_filter}
                        ORDER BY CAST(c.object_number AS INTEGER), c.object_number""",
                    channel_params,
                ).fetchall()
                channels = self.decode_channel_rows(channel_rows)
                self.attach_physical_channels(db, channels)
                self.attach_send_group_names(db, channels)
                selected_channel = next(item for item in channels if item["id"] == channel["id"])
                # Home Assistant entities of this channel (device-level ones are listed on the device page).
                key = connections.channel_key(channel["physical_channel"], channel["object_number"])
                ha_entities = [item for item in home_assistant.mapped_entities(db, states=True, device_id=channel["device_id"])
                               if item.get("channel_key") == key]
                self.send_json({
                    "home_assistant": ha_entities,
                    "type": record_type,
                    "record": channel_record,
                    "groups": selected_channel["groups"],
                    "terminals": selected_channel["terminals"],
                    "channels": channels,
                })
                return

            if record_type == "terminal":
                if not record_id.isdigit():
                    self.send_json({"error": "Invalid terminal ID"}, 400)
                    return
                terminal = db.execute(
                                  f"""SELECT t.*, l.name AS location, f.id AS fuse_id, f.label AS fuse, f.rating AS fuse_rating,
                                             fi.id AS fuse_fi_id, fi.number AS fuse_fi_number,
                                             fi.label AS fuse_fi_label, fil.name AS fuse_fi_location
                                      FROM terminals t JOIN locations l ON l.id=t.location_id
                                      {connections.terminal_fuse_join('t', 'f')}
                                      {connections.fuse_fi_join('f', 'fi')}
                                      LEFT JOIN locations fil ON fil.id=fi.location_id
                                      WHERE t.id=?""",
                    (int(record_id),),
                ).fetchone()
                if not terminal:
                    self.send_json({"error": "Klemme not found"}, 404)
                    return
                # The path of the Klemme in tree order; it can run through further Klemmen above and below it.
                chain = connections.terminal_chain(db, int(record_id))
                connected_terminal_ids = {int(record_id)} | {ref["terminal_id"] for ref in chain if ref["type"] == "terminal"}
                terminal_placeholders = ",".join("?" for _ in connected_terminal_ids)
                terminal_params = tuple(sorted(connected_terminal_ids))
                channels = rows_as_dicts(db.execute(
                    f"""SELECT a.id AS assignment_id, a.terminal_id, c.device_id, c.object_number, c.physical_channel, c.name, c.function,
                              d.product, COALESCE(NULLIF(d.manual_name, ''), d.device_id) AS device_label, a.note
                       FROM channel_terminals a JOIN channels c ON c.id=a.channel_id
                       JOIN devices d ON d.device_id=c.device_id WHERE a.terminal_id IN ({terminal_placeholders})
                       ORDER BY a.terminal_id, c.device_id, CAST(c.object_number AS INTEGER)""",
                    terminal_params,
                ).fetchall())
                self.attach_send_group_names(db, channels)
                device_channels = rows_as_dicts(db.execute(
                    f"""SELECT link.terminal_id, link.device_id, link.physical_channel, d.product,
                              COALESCE(NULLIF(d.manual_name, ''), d.device_id) AS device_label,
                              (SELECT candidate.object_number FROM channels candidate
                               WHERE candidate.device_id=link.device_id
                                 AND candidate.physical_channel=link.physical_channel
                               ORDER BY CAST(candidate.object_number AS INTEGER), candidate.object_number
                                                             LIMIT 1) AS first_object_number
                       FROM terminal_channel_links link JOIN devices d ON d.device_id=link.device_id
                       LEFT JOIN channels c ON c.device_id=link.device_id
                          AND c.physical_channel=link.physical_channel
                              WHERE link.terminal_id IN ({terminal_placeholders})
                              GROUP BY link.terminal_id,link.device_id,link.physical_channel,d.product,d.manual_name
                              ORDER BY link.terminal_id,link.device_id,link.physical_channel""",
                          terminal_params,
                ).fetchall())
                self.attach_send_group_names(db, device_channels)
                # Every Klemme on the path, so each one can be shown and opened.
                path_terminals = {str(row["id"]): dict(row) for row in db.execute(
                    f"""SELECT t.id, t.label, t.terminal_number, t.position, t.wire_color, t.used, t.description,
                               l.name AS location
                        FROM terminals t JOIN locations l ON l.id=t.location_id WHERE t.id IN ({terminal_placeholders})""",
                    terminal_params,
                )}
                # Conductor of the Klemme (L1, N4, PE, ...) from the connection tree, and the ones it can be
                # connected to by hand (N, an FI's neutral or PE; any Klemme without Sicherung, whatever its colour).
                record = {**dict(terminal), "wire_phase": connections.terminal_wire_phase(db, int(terminal["id"])),
                          "conductor_options": connections.conductor_options(db, int(terminal["id"]))}
                # Verbraucher on the path, with their L/N/PE feeds.
                loads = connections.terminal_loads(db, sorted(connected_terminal_ids))
                self.send_json({"type": record_type, "record": record, "channels": channels,
                                "device_channels": device_channels, "chain": chain,
                                "path_terminals": path_terminals, "loads": loads})
                return

            if record_type == "fi":
                if not record_id.isdigit():
                    self.send_json({"error": "Invalid FI ID"}, 400)
                    return
                fi = db.execute(
                    """SELECT fi.id, fi.number, fi.label, fi.reviewed_at, l.name AS location
                       FROM fi_devices fi JOIN locations l ON l.id=fi.location_id WHERE fi.id=?""",
                    (int(record_id),),
                ).fetchone()
                if not fi:
                    self.send_json({"error": "FI not found"}, 404)
                    return
                fuses = rows_as_dicts(db.execute(
                    """SELECT f.id, f.label, f.rating, l.name AS location
                       FROM fuses f JOIN locations l ON l.id=f.location_id
                       WHERE f.id IN (SELECT fuse_id FROM fuse_fi WHERE fi_id=?)
                       ORDER BY l.name COLLATE NATURAL_SORT, f.label COLLATE NATURAL_SORT""",
                    (int(record_id),),
                ).fetchall())
                # Sicherungen of the same location that can be placed below this FI (review mode).
                assignable = rows_as_dicts(db.execute(
                    f"""SELECT f.id, f.label, f.rating, other.number AS fi_number FROM fuses f
                        {connections.fuse_fi_join('f', 'other')}
                        WHERE f.location_id=(SELECT location_id FROM fi_devices WHERE id=?)
                          AND COALESCE(other.id, 0)<>?
                        ORDER BY f.label COLLATE NATURAL_SORT""",
                    (int(record_id), int(record_id)),
                ).fetchall())
                self.send_json({"type": record_type, "record": dict(fi), "fuses": fuses, "assignable_fuses": assignable})
                return

            if record_type == "fuse":
                if not record_id.isdigit():
                    self.send_json({"error": "Invalid fuse ID"}, 400)
                    return
                fuse = db.execute(
                    f"""SELECT f.*, l.name AS location, fi.id AS fi_id,
                              fi.number AS fi_number, fi.label AS fi_label
                       FROM fuses f JOIN locations l ON l.id=f.location_id
                       {connections.fuse_fi_join('f', 'fi')} WHERE f.id=?""",
                    (int(record_id),),
                ).fetchone()
                if not fuse:
                    self.send_json({"error": "Sicherung not found"}, 404)
                    return
                fuse_record = dict(fuse)
                fuse_record["phases"] = [phase for phase in fuse["phases"].split(",") if phase]
                fuse_terminal_ids = [row[0] for row in db.execute(
                    "SELECT t.id FROM terminals t WHERE t.id IN (SELECT terminal_id FROM terminal_fuse WHERE fuse_id=?) "
                    "ORDER BY t.location_id, t.label", (int(record_id),)
                )]
                # Klemmen wired on behind a Klemme of this Sicherung carry it too, so they are listed here.
                connected_terminal_ids = set(fuse_terminal_ids)
                terminal_placeholders = ",".join("?" for _ in connected_terminal_ids) or "NULL"
                terminal_params = tuple(sorted(connected_terminal_ids))
                manual_terminals = rows_as_dicts(db.execute(
                    f"""SELECT t.id, t.label, t.terminal_number, t.position, t.wire_color, t.used, t.description, t.reviewed_at,
                              l.name AS location, c.device_id, COALESCE(NULLIF(d.manual_name, ''), d.device_id) AS device_label,
                              c.object_number, c.physical_channel, c.reviewed_at AS channel_reviewed_at,
                                                            c.name AS channel_name, c.function, d.product
                       FROM terminals t JOIN locations l ON l.id=t.location_id
                       LEFT JOIN channel_terminals a ON a.terminal_id=t.id
                       LEFT JOIN channels c ON c.id=a.channel_id
                       LEFT JOIN devices d ON d.device_id=c.device_id
                              WHERE t.id IN ({terminal_placeholders}) ORDER BY l.name, t.label, c.device_id, c.object_number""",
                          terminal_params,
                ).fetchall())
                physical_terminals = rows_as_dicts(db.execute(
                    f"""SELECT t.id, t.label, t.terminal_number, t.position, t.wire_color, t.used, t.description, t.reviewed_at,
                                                            l.name AS location, link.device_id, COALESCE(NULLIF(d.manual_name, ''), d.device_id) AS device_label, link.physical_channel,
                                                            c.object_number, c.reviewed_at AS channel_reviewed_at, c.name AS channel_name, c.function, d.product
                       FROM terminals t JOIN locations l ON l.id=t.location_id
                                             JOIN terminal_channel_links link ON link.terminal_id=t.id
                                             JOIN devices d ON d.device_id=link.device_id
                       LEFT JOIN channels c ON c.id=(
                           SELECT candidate.id FROM channels candidate
                                                     WHERE candidate.device_id=link.device_id
                                                         AND candidate.physical_channel=link.physical_channel
                           ORDER BY CAST(candidate.object_number AS INTEGER), candidate.object_number
                           LIMIT 1
                       )
                       WHERE t.id IN ({terminal_placeholders})
                       GROUP BY t.id,t.label,t.terminal_number,t.position,t.wire_color,t.used,t.description,l.name,
                                link.device_id,link.physical_channel,c.id,c.object_number,c.reviewed_at,c.name,c.function,d.product,d.manual_name
                       ORDER BY l.name COLLATE NATURAL_SORT,t.label COLLATE NATURAL_SORT,
                                                                link.device_id,link.physical_channel""",
                    terminal_params,
                ).fetchall())
                self.attach_send_group_names(db, manual_terminals)
                self.attach_send_group_names(db, physical_terminals)
                manual_by_channel = {}
                for terminal in manual_terminals:
                    channel_key = terminal["physical_channel"] or f"object:{terminal['object_number']}"
                    manual_by_channel.setdefault((terminal["id"], terminal["device_id"], channel_key), terminal)
                manual_terminals = list(manual_by_channel.values())
                assigned_terminals = [terminal for terminal in manual_terminals if terminal["device_id"]]
                unassigned_terminals = [terminal for terminal in manual_terminals if not terminal["device_id"]]
                by_physical_assignment = {
                    (terminal["id"], terminal["device_id"], terminal["physical_channel"]): terminal
                    for terminal in assigned_terminals
                }
                for terminal in physical_terminals:
                    key = (terminal["id"], terminal["device_id"], terminal["physical_channel"])
                    existing = by_physical_assignment.get(key)
                    if existing:
                        continue
                    else:
                        assigned_terminals.append(terminal)
                        by_physical_assignment[key] = terminal
                assigned_terminal_ids = {terminal["id"] for terminal in assigned_terminals}
                terminals = assigned_terminals + [
                    terminal for terminal in unassigned_terminals
                    if terminal["id"] not in assigned_terminal_ids
                ]
                # Verbraucher hanging directly on this Sicherung (no Klemme in between), with their feeds.
                self.send_json({"type": record_type, "record": fuse_record, "terminals": terminals,
                                "fuse_terminal_ids": fuse_terminal_ids,
                                "fuse_graph": connections.fuse_graph(db, int(record_id)),
                                "loads": connections.terminal_loads(db, [], [int(record_id)])})
                return

            if record_type == "load":
                # A Verbraucher with all its feeds (its own page: also one without Klemme or Sicherung can be edited).
                found = connections.terminal_loads(db, [], [], [int(record_id)]) if record_id.isdigit() else []
                if not found:
                    self.send_json({"error": "Verbraucher not found"}, 404)
                    return
                load = found[0]
                # The Sicherungen feeding it (through its Klemmen or directly), each with its FI, for the page header.
                terminal_ids = [feed["terminal_id"] for feed in load["feeds"] if feed["terminal_id"]]
                fuse_ids = {feed["fuse_id"] for feed in load["feeds"] if feed.get("fuse_id")}
                if terminal_ids:
                    fuse_ids |= {row[0] for row in db.execute(
                        f"SELECT DISTINCT fuse_id FROM terminal_fuse WHERE terminal_id IN ({','.join('?' * len(terminal_ids))})",
                        terminal_ids) if row[0] is not None}
                load["fuses"] = rows_as_dicts(db.execute(
                    f"""SELECT f.id, f.label, f.rating, fi.id AS fi_id, fi.number AS fi_number, fi.label AS fi_label
                        FROM fuses f {connections.fuse_fi_join('f', 'fi')}
                        WHERE f.id IN ({','.join('?' * len(fuse_ids)) or 'NULL'})
                        ORDER BY f.label COLLATE NATURAL_SORT""", tuple(fuse_ids)).fetchall())
                self.send_json({"type": record_type, "record": load, "loads": found})
                return

        self.send_json({"error": "Unknown record type"}, 400)

    @staticmethod
    def decode_channel_rows(rows: list[sqlite3.Row]) -> list[dict[str, object]]:
        result = []
        for row in rows:
            item = dict(row)
            for field in ("groups_json", "terminals_json"):
                if field in item:
                    item[field.removesuffix("_json")] = json.loads(item.pop(field) or "[]")
            result.append(item)
        return result

    @staticmethod
    def attach_send_group_names(
        db: sqlite3.Connection,
        channels: list[dict[str, object]],
        *,
        linked_only: bool = False,
    ) -> None:
        linked_filter = """
                         AND EXISTS (
                             SELECT 1 FROM terminal_channel_links link
                             WHERE link.device_id=c.device_id
                               AND link.physical_channel=c.physical_channel
                         )""" if linked_only else ""
        names_by_channel = {
            (str(row["device_id"]), str(row["channel_key"])): str(row["name"])
            for row in db.execute(
                f"""WITH ranked_send_groups AS (
                       SELECT c.device_id,
                              COALESCE(NULLIF(c.physical_channel, ''), 'object:' || c.object_number) AS channel_key,
                              g.name,
                              ROW_NUMBER() OVER (
                                  PARTITION BY c.device_id, COALESCE(NULLIF(c.physical_channel, ''), 'object:' || c.object_number)
                                  ORDER BY g.address COLLATE NATURAL_SORT, c.object_number COLLATE NATURAL_SORT
                              ) AS group_rank
                       FROM channels c
                       JOIN channel_group_addresses assignment ON assignment.channel_id=c.id
                       JOIN group_addresses g ON g.address=assignment.address
                       WHERE instr('/' || assignment.direction || '/', '/S/') > 0
                         AND trim(g.name) <> ''
                                                 {linked_filter}
                   )
                   SELECT device_id, channel_key, name
                   FROM ranked_send_groups WHERE group_rank=1"""
            )
        }
        for channel in channels:
            device_id = str(channel.get("device_id") or "")
            physical_channel = str(channel.get("physical_channel") or "")
            object_number = str(channel.get("object_number") or channel.get("first_object_number") or "")
            if not device_id or not (physical_channel or object_number):
                channel["send_group_name"] = ""
                continue
            channel_key = physical_channel or f"object:{object_number}"
            channel["send_group_name"] = names_by_channel.get((device_id, channel_key), "")

    @staticmethod
    def physical_channel_terminals(db: sqlite3.Connection, device_id: str, physical_channel: str) -> list[dict[str, object]]:
        if not physical_channel:
            return []
        return rows_as_dicts(db.execute(
            f"""SELECT NULL AS assignment_id, t.id AS terminal_id, t.label AS terminal, t.terminal_number,
                      t.position, t.wire_color, t.used, t.description, l.name AS location,
                       f.id AS fuse_id, f.label AS fuse, f.rating AS fuse_rating,
                       fi.id AS fi_id, fi.number AS fi_number, fi.label AS fi_label, fil.name AS fi_location
                 FROM terminal_channel_links link JOIN terminals t ON t.id=link.terminal_id
                   JOIN locations l ON l.id=t.location_id
                   {connections.terminal_fuse_join('t', 'f')}
                   {connections.fuse_fi_join('f', 'fi')} LEFT JOIN locations fil ON fil.id=fi.location_id
                 WHERE link.device_id=? AND link.physical_channel=?
                 ORDER BY l.name,t.label""",
            (device_id, physical_channel),
        ).fetchall())

    @classmethod
    def attach_physical_channels(cls, db: sqlite3.Connection, channels: list[dict[str, object]]) -> None:
        for channel in channels:
            terminals = channel.get("terminals", [])
            if not isinstance(terminals, list):
                continue
            assigned_terminal_ids = {terminal["terminal_id"] for terminal in terminals}
            terminals.extend(
                terminal for terminal in cls.physical_channel_terminals(
                    db, str(channel["device_id"]), str(channel["physical_channel"] or "")
                ) if terminal["terminal_id"] not in assigned_terminal_ids
            )

    def do_POST(self) -> None:
        if not self.client_allowed():
            return
        try:
            payload = self.read_json()
            if self.path == "/api/assignments":
                self.create_assignment(payload)
            elif self.path == "/api/restart":
                self.restart_server()
            elif self.path == "/api/database/import":
                self.import_database(payload)
            elif self.path == "/api/terminals/create":
                self.create_terminal(payload)
            elif self.path == "/api/terminals/update":
                self.update_terminal(payload)
            elif self.path == "/api/review":
                self.update_review_status(payload)
            elif self.path == "/api/assignments/delete":
                self.delete_assignment(payload)
            elif self.path == "/api/channel-links/delete":
                self.delete_channel_link(payload)
            elif self.path == "/api/connections/reorder":
                self.reorder_connections(payload)
            elif self.path == "/api/devices":
                self.create_manual_device(payload)
            elif self.path == "/api/device-channels":
                self.create_manual_channel(payload)
            elif self.path == "/api/device-channels/update":
                self.update_manual_channel(payload)
            elif self.path == "/api/device-channels/delete":
                self.delete_manual_channel(payload)
            elif self.path == "/api/fuses/update":
                self.update_fuse(payload)
            elif self.path == "/api/fuses/delete":
                self.delete_fuse(payload)
            elif self.path == "/api/terminals/extend":
                self.extend_terminal_path(payload)
            elif self.path == "/api/terminals/conductor":
                self.connect_terminal_conductor(payload)
            elif self.path == "/api/loads/disconnect":
                self.disconnect_load(payload)
            elif self.path == "/api/loads/feed":
                self.connect_load_feed(payload)
            elif self.path == "/api/loads/create":
                self.create_load(payload)
            elif self.path == "/api/loads/update":
                self.update_load(payload)
            elif self.path == "/api/loads/delete":
                self.delete_load(payload)
            elif self.path == "/api/terminals/delete":
                self.delete_terminal(payload)
            elif self.path == "/api/devices/update":
                self.update_manual_device(payload)
            elif self.path == "/api/devices/delete":
                self.delete_manual_device(payload)
            elif self.path == "/api/fi/update":
                self.update_fi(payload)
            elif self.path == "/api/fi/delete":
                self.delete_fi(payload)
            elif self.path == "/api/fuses/fi":
                self.set_fuse_fi(payload)
            elif self.path == "/api/locations/create":
                self.create_location(payload)
            elif self.path == "/api/locations/update":
                self.update_location(payload)
            elif self.path == "/api/locations/delete":
                self.delete_location(payload)
            elif self.path == "/api/import/preview":
                self.preview_import(payload)
            elif self.path == "/api/import/apply":
                self.apply_import(payload)
            else:
                self.send_json({"error": "Not found"}, 404)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except sqlite3.IntegrityError as exc:
            self.send_json({"error": f"The update conflicts with existing data: {exc}"}, 400)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def restart_server(self) -> None:
        """Restart the app (settings page, e.g. after deploying new files): answer first, then stop the server loop;
        `main` starts the same command again."""
        self.send_json({"restarting": True})
        self.schedule_restart()

    def schedule_restart(self) -> None:
        """Stop the server loop after this answer; `main` then starts the same command again."""
        RESTART_REQUESTED.set()
        # `shutdown` waits for the server loop, so it must run outside this request's thread.
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def export_database(self) -> None:
        """The whole database as download: a consistent copy (SQLite backup API, also while the server writes)."""
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / DB_PATH.name
            with closing(sqlite3.connect(DB_PATH)) as source, closing(sqlite3.connect(copy)) as target:
                source.backup(target)
            body = copy.read_bytes()
        name = f"{DB_PATH.stem}-{datetime.now().strftime('%Y%m%d-%H%M')}{DB_PATH.suffix}"
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.sqlite3")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def import_database(self, payload: dict[str, object]) -> None:
        """Replace the database with an uploaded one ("Datenbank importieren"). The upload is checked first; the
        current database gets a verified backup; then the file is swapped in one step and the server restarts on it
        (so the start checks and the start backup run on the new file)."""
        data = base64.b64decode(str(payload.get("file_base64", "")), validate=True)
        if not data.startswith(b"SQLite format 3\x00"):
            raise ValueError("Die Datei ist keine SQLite-Datenbank.")
        # Next to the database, so the swap is one rename on the same file system.
        descriptor, name = tempfile.mkstemp(prefix=".import-", suffix=DB_PATH.suffix, dir=DB_PATH.parent)
        upload = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            counts = check_import_database(upload)
            backup = backup_and_prune()
            os.replace(upload, DB_PATH)
        finally:
            upload.unlink(missing_ok=True)
        self.send_json({"imported": True, "backup": backup.name if backup else "", "counts": counts})
        self.schedule_restart()

    def connections_for_record(self, record_type: str, record_id: str) -> None:
        """Ordered connection chains that contain the given FI, Sicherung, Klemme or channel."""
        with connect() as db:
            try:
                trees = connections.trees_for_entity(db, record_type, record_id)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            self.send_json({"type": record_type, "id": record_id, "trees": trees})

    def reorder_connections(self, payload: dict[str, object]) -> None:
        with connect() as db:
            connections.apply_action(db, payload)
        self.send_json({"updated": True})

    @staticmethod
    def load_overview() -> list[dict[str, object]]:
        """Every Verbraucher for the start-page overview: name, location, cable and its L/N/PE Klemmen.

        An entry opens the Klemme that feeds its L (else its first Klemme, else its Sicherung, else its own page).
        """
        with connect() as db:
            loads = connections.terminal_loads(db)
        items = []
        for load in loads:
            feeds = load["feeds"]
            # The entry opens the Klemme feeding its L, else its first Klemme; a Verbraucher hanging directly on a
            # Sicherung opens that Sicherung (a direct PE link has nothing to open).
            opens = (next((feed for feed in feeds if feed["role"] == "L" and feed["terminal_id"]), None)
                     or next((feed for feed in feeds if feed["terminal_id"]), None)
                     or next((feed for feed in feeds if feed.get("fuse_id")), None))
            parts = [load["location"], f"Kabel {load['terminal_number']}" if load["terminal_number"] else ""]
            parts += [f"{feed['wire_phase'] or feed['role']} {feed['label']}" for feed in feeds]
            # A Verbraucher without any PE connection does not miss one.
            missing = [role for role in ("L", "N", "PE") if role not in {feed["role"] for feed in feeds}
                       and not (role == "PE" and load["no_pe"])]
            parts += ["ohne PE"] if load["no_pe"] else []
            parts += [f"{role} fehlt" for role in missing]
            target = (("terminal", opens["terminal_id"]) if opens and opens["terminal_id"]
                      else ("fuse", opens["fuse_id"]) if opens else ("load", load["id"]))
            items.append({"type": target[0], "id": str(target[1]), "title": load["name"],
                          "subtitle": " · ".join(filter(None, parts)), "location": load["location"]})
        return sorted(items, key=lambda item: (connections.natural_key(item["location"] or ""),
                                               connections.natural_key(item["title"])))

    def disconnect_load(self, payload: dict[str, object]) -> None:
        """Cut one Verbraucher link (its `load` node in the connection tree)."""
        node_id = payload.get("node_id")
        if not str(node_id or "").isdigit():
            raise ValueError("Invalid Verbraucher link.")
        with connect() as db:
            removed = connections.disconnect_load(db, int(node_id))
        self.send_json({"removed": removed})

    def connect_load_feed(self, payload: dict[str, object]) -> None:
        """Give a Verbraucher its missing L/N/PE feed: an existing Klemme (left unchanged) or a new one."""
        load_id = payload_id(payload, "load_id", "Verbraucher")
        role = str(payload.get("role", "")).strip()
        if role not in ("L", "N", "PE"):
            raise ValueError("Choose L, N or PE.")
        location = str(payload.get("location", "")).strip()
        number = str(payload.get("terminal_number", "")).strip()
        position = str(payload.get("position", "")).strip().lower()
        wire_color = str(payload.get("wire_color", "")).strip().lower()
        if role in ("N", "PE") and not number:
            # N or PE without a Klemme: the Verbraucher is connected directly to the conductor. Which neutral (N or
            # an FI's, "fi:<id>") is named by the user, never guessed.
            conductor = "PE" if role == "PE" else str(payload.get("conductor", "")).strip()
            if not conductor:
                raise ValueError("Choose N or the neutral of an FI, or enter a Klemme.")
            if role == "N" and conductor == "PE":
                raise ValueError("An N feed cannot be PE.")
            with connect() as db:
                result = connections.connect_load_to_conductor(db, load_id, conductor)
                self.drop_replaced_feed(db, payload, load_id, result)
            self.send_json({"terminal_id": None, "terminal_created": False, **result})
            return
        if not location or not number:
            raise ValueError("Enter the location and the number of the Klemme.")
        if position and not POSITION_CODE.fullmatch(position):
            raise ValueError("Position must be a short code using letters, numbers, dots, or hyphens.")
        if wire_color and wire_color not in WIRE_COLORS:
            raise ValueError("Choose a wire color from the available color chips.")
        with connect() as db:
            if not db.execute("SELECT 1 FROM loads WHERE id=?", (load_id,)).fetchone():
                raise ValueError("Verbraucher not found.")
            existing = db.execute(
                "SELECT t.id FROM terminals t JOIN locations l ON l.id=t.location_id WHERE l.name=? AND t.label=?",
                (location, make_terminal_label(number, position)),
            ).fetchone()
            # An existing Klemme keeps its data (also its colour); only a missing one is created.
            terminal_id = int(existing[0]) if existing else self.get_or_create_terminal(db, location, number, position, wire_color)
            result = connections.connect_load_feed(db, terminal_id, load_id, role)
            self.drop_replaced_feed(db, payload, load_id, result)
        self.send_json({"terminal_id": terminal_id, "terminal_created": existing is None, **result})

    @staticmethod
    def drop_replaced_feed(db: sqlite3.Connection, payload: dict[str, object], load_id: int, result: dict[str, object]) -> None:
        """"Ändern" of one feed: the new one is connected, now the old one of this Verbraucher is cut (only its own
        link; other Verbraucher on the same Klemme keep theirs). Nothing is cut when the feed did not change."""
        replace_id = str(payload.get("replace_node_id", "")).strip()
        if not replace_id.isdigit() or result.get("already_exists"):
            return
        old = db.execute("SELECT load_id FROM connection_nodes WHERE id=? AND node_type='load'", (int(replace_id),)).fetchone()
        if not old or old[0] != load_id:
            raise ValueError("The feed to replace does not belong to this Verbraucher.")
        connections.disconnect_load(db, int(replace_id))

    def connect_terminal_conductor(self, payload: dict[str, object]) -> None:
        """Connect a Klemme without Sicherung by hand to N, an FI's neutral ("fi:<id>") or PE; "" disconnects it."""
        terminal_id = payload_id(payload, "terminal_id", "Klemme")
        conductor = str(payload.get("conductor", ""))
        with connect() as db:
            if conductor:
                code = connections.connect_conductor(db, terminal_id, conductor)
            else:
                connections.disconnect_conductor(db, terminal_id)
                code = ""
        self.send_json({"wire_phase": code})

    def create_load(self, payload: dict[str, object]) -> None:
        """A new Verbraucher, fed by an existing Klemme (a Verbraucher exists through the Klemmen feeding it).

        The Klemme is looked up by room and label and never created here, so no Klemme lands in a wrong room.
        A Verbraucher of the same name on that Klemme's cable is reused.
        """
        name = clean_text(payload.get("name"), "The Verbraucher name")
        description = clean_text(payload.get("description"), "The description")
        location = clean_text(payload.get("location"), "The location")
        number = str(payload.get("terminal_number", "")).strip()
        position = str(payload.get("position", "")).strip().lower()
        if not name:
            raise ValueError("Enter a name for the Verbraucher.")
        if payload.get("feed") == "fuse":
            # Directly on an existing Sicherung (any room): its L comes from the Sicherung.
            with connect() as db:
                fuse_id = resolve_fuse_id(db, payload.get("fuse_id"), payload.get("fuse"))
                if fuse_id is None:
                    raise ValueError("Choose the Sicherung that feeds the Verbraucher.")
                result = connections.connect_load_to_fuse(db, fuse_id, None, name)
                self.set_load_details(db, result["load_id"], description, payload_flag(payload, "no_pe"))
            self.send_json({**result, "terminal_id": None, "fuse_id": fuse_id})
            return
        if not location or not number:
            raise ValueError("Enter the room and number of the Klemme that feeds the Verbraucher.")
        label = make_terminal_label(number, position)
        with connect() as db:
            row = db.execute(
                "SELECT t.id FROM terminals t JOIN locations l ON l.id=t.location_id WHERE l.name=? AND t.label=?",
                (location, label),
            ).fetchone()
            if not row:
                raise ValueError(f"Klemme {label} does not exist in {location}; create it first (Klemmen → Klemme hinzufügen).")
            result = connections.connect_load(db, int(row[0]), None, name)
            self.set_load_details(db, result["load_id"], description, payload_flag(payload, "no_pe"))
        self.send_json({**result, "terminal_id": int(row[0])})

    @staticmethod
    def set_load_details(db: sqlite3.Connection, load_id: int, description: str, no_pe: bool) -> None:
        """Details of a Verbraucher given when it is created: its description (when given) and "no PE"."""
        if description:
            db.execute("UPDATE loads SET description=? WHERE id=?", (description, load_id))
        if no_pe:
            db.execute("UPDATE loads SET no_pe=1 WHERE id=?", (load_id,))

    def update_load(self, payload: dict[str, object]) -> None:
        """Rename a Verbraucher, change its description, or mark that it has no PE connection at all."""
        load_id = payload_id(payload, "load_id", "Verbraucher")
        name = clean_text(payload.get("name"), "The Verbraucher name")
        description = clean_text(payload.get("description"), "The description")
        if not name:
            raise ValueError("Enter a name for the Verbraucher.")
        with connect() as db:
            if not db.execute("UPDATE loads SET name=?, description=?, no_pe=? WHERE id=?",
                              (name, description, int(payload_flag(payload, "no_pe")), load_id)).rowcount:
                raise ValueError("Verbraucher not found.")
        self.send_json({"updated": True, "load_id": load_id})

    def delete_load(self, payload: dict[str, object]) -> None:
        """Delete a Verbraucher with all its links; its Klemmen stay."""
        load_id = payload_id(payload, "load_id", "Verbraucher")
        with connect() as db:
            connections.delete_load(db, load_id)
        self.send_json({"deleted": True, "load_id": load_id})

    def delete_terminal(self, payload: dict[str, object]) -> None:
        """Delete a Klemme; Klemmen wired on behind it start a chain of their own."""
        terminal_id = payload_id(payload, "terminal_id", "Klemme")
        with connect() as db:
            connections.delete_terminal(db, terminal_id)
        self.send_json({"deleted": True, "terminal_id": terminal_id})

    @staticmethod
    def manual_device_fields(payload: dict[str, object]) -> tuple[str, str, str, str, str]:
        """Name, manufacturer, product, location and description of a manual device form."""
        fields = tuple(clean_text(payload.get(name), "Device fields")
                       for name in ("name", "manufacturer", "product", "location", "description"))
        if not fields[0]:
            raise ValueError("Enter a device name.")
        return fields

    @staticmethod
    def require_manual_device(db: sqlite3.Connection, device_id: str) -> None:
        """Only manually added devices can be changed; ETS devices come from the KNX project."""
        if not device_id.startswith("manual:") or not db.execute(
            "SELECT 1 FROM devices WHERE device_id=? AND COALESCE(is_manual, 0)=1", (device_id,)
        ).fetchone():
            raise ValueError("Only manually added devices can be changed.")

    def update_manual_device(self, payload: dict[str, object]) -> None:
        device_id = str(payload.get("device_id", "")).strip()
        name, manufacturer, product, location, description = self.manual_device_fields(payload)
        with connect() as db:
            self.require_manual_device(db, device_id)
            db.execute(
                "UPDATE devices SET manual_name=?, manufacturer=?, product=?, location=?, description=? WHERE device_id=?",
                (name, manufacturer, product, location, description, device_id),
            )
        self.send_json({"updated": True, "device_id": device_id})

    def delete_manual_device(self, payload: dict[str, object]) -> None:
        """Delete a manual device with its channels; Klemmen connected to them stay where they are."""
        device_id = str(payload.get("device_id", "")).strip()
        with connect() as db:
            self.require_manual_device(db, device_id)
            for physical_channel, object_number in db.execute(
                "SELECT physical_channel, object_number FROM channels WHERE device_id=?", (device_id,)
            ).fetchall():
                connections.remove_channel_nodes(db, device_id, connections.channel_key(physical_channel, object_number))
            channels = db.execute("DELETE FROM channels WHERE device_id=?", (device_id,)).rowcount
            db.execute("DELETE FROM devices WHERE device_id=?", (device_id,))
            connections.finish(db)
        self.send_json({"deleted": True, "device_id": device_id, "channels_removed": channels})

    def update_fi(self, payload: dict[str, object]) -> None:
        """Change number and label of an FI (applies to every Sicherung below it)."""
        fi_id = payload_id(payload, "fi_id", "FI")
        number = clean_text(payload.get("number"), "The FI number", 20)
        label = clean_text(payload.get("label"), "The FI label")
        if not number and not label:
            raise ValueError("Enter an FI number or a label.")
        with connect() as db:
            fi = db.execute("SELECT location_id FROM fi_devices WHERE id=?", (fi_id,)).fetchone()
            if not fi:
                raise ValueError("FI not found.")
            if number and db.execute("SELECT 1 FROM fi_devices WHERE location_id=? AND number=? AND id<>?",
                                     (fi["location_id"], number, fi_id)).fetchone():
                raise ValueError("Another FI at this location already uses that number.")
            db.execute("UPDATE fi_devices SET number=?, label=? WHERE id=?", (number or None, label, fi_id))
        self.send_json({"updated": True, "fi_id": fi_id})

    def delete_fi(self, payload: dict[str, object]) -> None:
        """Delete an FI: its Sicherungen move to their phase, its blue Klemmen lose their neutral (Prüfliste)."""
        fi_id = payload_id(payload, "fi_id", "FI")
        with connect() as db:
            if not db.execute("DELETE FROM fi_devices WHERE id=?", (fi_id,)).rowcount:
                raise ValueError("FI not found.")
            connections.finish(db)
        self.send_json({"deleted": True, "fi_id": fi_id})

    def set_fuse_fi(self, payload: dict[str, object]) -> None:
        """Place a Sicherung below an FI of its location, or take it off its FI (fi_id null)."""
        fuse_id = payload_id(payload, "fuse_id", "Sicherung")
        fi_id = None if payload.get("fi_id") in (None, "") else payload_id(payload, "fi_id", "FI")
        with connect() as db:
            fuse = db.execute("SELECT location_id FROM fuses WHERE id=?", (fuse_id,)).fetchone()
            if not fuse:
                raise ValueError("Sicherung not found.")
            if fi_id is not None and not db.execute(
                "SELECT 1 FROM fi_devices WHERE id=? AND location_id=?", (fi_id, fuse["location_id"])
            ).fetchone():
                raise ValueError("The FI does not belong to the location of this Sicherung.")
            connections.set_fuse_fi(db, fuse_id, fi_id)
        self.send_json({"updated": True, "fuse_id": fuse_id, "fi_id": fi_id})

    @staticmethod
    def location_fields(payload: dict[str, object]) -> tuple[str, str]:
        name = clean_text(payload.get("name"), "The location name")
        if not name:
            raise ValueError("Enter a location name.")
        return name, clean_text(payload.get("description"), "The description")

    def create_location(self, payload: dict[str, object]) -> None:
        name, description = self.location_fields(payload)
        with connect() as db:
            if db.execute("SELECT 1 FROM locations WHERE name=?", (name,)).fetchone():
                raise ValueError("A location with that name already exists.")
            cursor = db.execute("INSERT INTO locations(name, description) VALUES (?, ?)", (name, description))
        self.send_json({"created": True, "location_id": int(cursor.lastrowid), "name": name})

    def update_location(self, payload: dict[str, object]) -> None:
        location_id = payload_id(payload, "location_id", "location")
        name, description = self.location_fields(payload)
        with connect() as db:
            if db.execute("SELECT 1 FROM locations WHERE name=? AND id<>?", (name, location_id)).fetchone():
                raise ValueError("A location with that name already exists.")
            if not db.execute("UPDATE locations SET name=?, description=? WHERE id=?",
                              (name, description, location_id)).rowcount:
                raise ValueError("Location not found.")
        self.send_json({"updated": True, "location_id": location_id, "name": name})

    def delete_location(self, payload: dict[str, object]) -> None:
        """Delete an empty location; one that still holds Klemmen, Sicherungen, FI or Verbraucher is refused."""
        location_id = payload_id(payload, "location_id", "location")
        with connect() as db:
            used = sum(db.execute(f"SELECT COUNT(*) FROM {table} WHERE location_id=?", (location_id,)).fetchone()[0]
                       for table in ("terminals", "fuses", "fi_devices", "loads"))
            if used:
                raise ValueError("The location still holds Klemmen, Sicherungen, FI or Verbraucher; move or delete them first.")
            if not db.execute("DELETE FROM locations WHERE id=?", (location_id,)).rowcount:
                raise ValueError("Location not found.")
        self.send_json({"deleted": True, "location_id": location_id})

    def create_assignment(self, payload: dict[str, object]) -> None:
        device_id = str(payload.get("device_id", "")).strip()
        object_number = str(payload.get("object_number", "")).strip()
        location_name = str(payload.get("location", payload.get("switchboard", ""))).strip()
        terminal_number = str(payload.get("terminal_number", payload.get("terminal", ""))).strip()
        position = str(payload.get("position", "")).strip().lower()
        terminal_label = make_terminal_label(terminal_number, position)
        wire_color = str(payload.get("wire_color", "")).strip().lower()
        description = str(payload.get("description", "")).strip()
        if not (DEVICE_ID.fullmatch(device_id) or device_id.startswith("manual:")):
            self.send_json({"error": "Choose a valid device."}, 400)
            return
        if not all((object_number, location_name, terminal_label)):
            self.send_json({"error": "Choose a channel and enter the Schaltschrank location and Klemme."}, 400)
            return
        if position and not POSITION_CODE.fullmatch(position):
            self.send_json({"error": "Position must be a short code using letters, numbers, dots, or hyphens."}, 400)
            return
        if wire_color and wire_color not in WIRE_COLORS:
            self.send_json({"error": "Choose a wire color from the available color chips."}, 400)
            return
        with connect() as db:
            channel = db.execute(
                """SELECT c.id, d.is_manual FROM channels c JOIN devices d ON d.device_id=c.device_id
                   WHERE c.device_id=? AND c.object_number=?""",
                (device_id, object_number),
            ).fetchone()
            if not channel:
                self.send_json({"error": "That device/channel does not exist."}, 404)
                return
            if device_id.startswith("manual:") and not channel["is_manual"]:
                self.send_json({"error": "Manual device not found."}, 404)
                return
            location_id = location_id_for(db, location_name)
            fuse_id = resolve_fuse_id(db, payload.get("fuse_id"), payload.get("fuse"), location_id)
            db.execute(
                """INSERT INTO terminals(location_id, label, description, terminal_number, position, wire_color)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(location_id, label) DO UPDATE SET
                     description=CASE WHEN excluded.description <> '' THEN excluded.description ELSE terminals.description END,
                     terminal_number=excluded.terminal_number,
                     position=excluded.position,
                     wire_color=excluded.wire_color""",
                (location_id, terminal_label, description, terminal_number, position, wire_color),
            )
            terminal_id = db.execute(
                "SELECT id FROM terminals WHERE location_id=? AND label=?",
                (location_id, terminal_label),
            ).fetchone()[0]
            cursor_rowcount = 0 if db.execute(
                "SELECT 1 FROM channel_terminals WHERE channel_id=? AND terminal_id=?", (channel[0], terminal_id)
            ).fetchone() else 1
            if fuse_id is not None:
                connections.set_terminal_fuse(db, terminal_id, fuse_id)
            connections.add_channel_link(db, terminal_id, self.channel_spec(db, channel[0]))
            assignment_id = db.execute(
                "SELECT id FROM channel_terminals WHERE channel_id=? AND terminal_id=?",
                (channel[0], terminal_id),
            ).fetchone()[0]
            self.send_json({"assignment_id": assignment_id, "already_assigned": cursor_rowcount == 0})

    @staticmethod
    def channel_spec(db: sqlite3.Connection, channel_id: int) -> dict[str, object]:
        """Connection-tree reference for one ETS object (channels row)."""
        row = db.execute(
            "SELECT device_id, physical_channel, object_number FROM channels WHERE id=?", (channel_id,)
        ).fetchone()
        return {
            "device_id": row["device_id"],
            "channel_key": connections.channel_key(row["physical_channel"], row["object_number"]),
            "channel_id": channel_id,
        }

    def create_manual_device(self, payload: dict[str, object]) -> None:
        name, manufacturer, product, location, description = self.manual_device_fields(payload)
        with connect() as db:
            device_id = self.insert_manual_device(db, name, manufacturer, product, location, description)
        self.send_json({"device_id": device_id, "name": name})

    @staticmethod
    def insert_manual_device(
        db: sqlite3.Connection, name: str, manufacturer: str = "", product: str = "",
        location: str = "", description: str = "",
    ) -> str:
        """Insert a manually added device and return its generated device_id."""
        device_id = f"manual:{uuid.uuid4().hex}"
        db.execute(
            """INSERT INTO devices(device_id, manual_name, is_manual, manufacturer, product, location, description)
               VALUES (?, ?, 1, ?, ?, ?, ?)""",
            (device_id, name, manufacturer, product, location, description),
        )
        return device_id

    @staticmethod
    def insert_manual_channel(db: sqlite3.Connection, device_id: str, physical_channel: str, name: str = "") -> tuple[str, int]:
        """Insert a channel of a manual device; returns (object_number, channels.id)."""
        object_number = f"manual-{uuid.uuid4().hex}"
        cursor = db.execute(
            "INSERT INTO channels(device_id, object_number, physical_channel, name) VALUES (?, ?, ?, ?)",
            (device_id, object_number, physical_channel, name),
        )
        return object_number, int(cursor.lastrowid)

    def create_manual_channel(self, payload: dict[str, object]) -> None:
        device_id = str(payload.get("device_id", "")).strip()
        physical_channel = " ".join(str(payload.get("physical_channel", "")).split())
        name = " ".join(str(payload.get("name", "")).split())
        if not device_id.startswith("manual:"):
            self.send_json({"error": "Choose a manually added device."}, 400)
            return
        if not physical_channel:
            self.send_json({"error": "Enter a channel name or number."}, 400)
            return
        if len(physical_channel) > 100 or len(name) > 500:
            self.send_json({"error": "Channel name must be 100 characters or fewer; description must be 500 or fewer."}, 400)
            return
        with connect() as db:
            device = db.execute(
                "SELECT 1 FROM devices WHERE device_id=? AND COALESCE(is_manual, 0)=1",
                (device_id,),
            ).fetchone()
            if not device:
                self.send_json({"error": "Manual device not found."}, 404)
                return
            duplicate = db.execute(
                "SELECT 1 FROM channels WHERE device_id=? AND physical_channel=?",
                (device_id, physical_channel),
            ).fetchone()
            if duplicate:
                self.send_json({"error": "A channel with that name already exists for this device."}, 409)
                return
            object_number, _ = self.insert_manual_channel(db, device_id, physical_channel, name)
        self.send_json({"device_id": device_id, "object_number": object_number, "physical_channel": physical_channel})

    def update_manual_channel(self, payload: dict[str, object]) -> None:
        device_id = str(payload.get("device_id", "")).strip()
        object_number = str(payload.get("object_number", "")).strip()
        physical_channel = " ".join(str(payload.get("physical_channel", "")).split())
        name = " ".join(str(payload.get("name", "")).split())
        if not device_id.startswith("manual:") or not object_number.startswith("manual-"):
            self.send_json({"error": "Choose a manually added channel."}, 400)
            return
        if not physical_channel:
            self.send_json({"error": "Enter a channel name or number."}, 400)
            return
        if len(physical_channel) > 100 or len(name) > 500:
            self.send_json({"error": "Channel name must be 100 characters or fewer; description must be 500 or fewer."}, 400)
            return
        with connect() as db:
            channel = db.execute(
                """SELECT 1 FROM channels c JOIN devices d ON d.device_id=c.device_id
                   WHERE c.device_id=? AND c.object_number=? AND COALESCE(d.is_manual, 0)=1""",
                (device_id, object_number),
            ).fetchone()
            if not channel:
                self.send_json({"error": "Manual channel not found."}, 404)
                return
            duplicate = db.execute(
                """SELECT 1 FROM channels WHERE device_id=? AND physical_channel=? COLLATE NOCASE
                   AND object_number<>?""",
                (device_id, physical_channel, object_number),
            ).fetchone()
            if duplicate:
                self.send_json({"error": "A channel with that name already exists for this device."}, 409)
                return
            previous = db.execute(
                "SELECT physical_channel FROM channels WHERE device_id=? AND object_number=?",
                (device_id, object_number),
            ).fetchone()[0]
            if previous != physical_channel:
                # Chains follow the renamed channel.
                connections.rename_channel_key(db, device_id, previous, physical_channel)
            db.execute(
                "UPDATE channels SET physical_channel=?, name=? WHERE device_id=? AND object_number=?",
                (physical_channel, name, device_id, object_number),
            )
        self.send_json({"device_id": device_id, "object_number": object_number, "physical_channel": physical_channel})

    def delete_manual_channel(self, payload: dict[str, object]) -> None:
        device_id = str(payload.get("device_id", "")).strip()
        object_number = str(payload.get("object_number", "")).strip()
        if not device_id.startswith("manual:") or not object_number.startswith("manual-"):
            self.send_json({"error": "Choose a manually added channel."}, 400)
            return
        with connect() as db:
            channel = db.execute(
                """SELECT c.physical_channel FROM channels c JOIN devices d ON d.device_id=c.device_id
                   WHERE c.device_id=? AND c.object_number=? AND COALESCE(d.is_manual, 0)=1""",
                (device_id, object_number),
            ).fetchone()
            if not channel:
                self.send_json({"error": "Manual channel not found."}, 404)
                return
            assignments = db.execute(
                "SELECT COUNT(*) FROM channel_terminals WHERE channel_id=(SELECT id FROM channels WHERE device_id=? AND object_number=?)",
                (device_id, object_number),
            ).fetchone()[0]
            connections.remove_channel_nodes(db, device_id, channel["physical_channel"])
            db.execute("DELETE FROM channels WHERE device_id=? AND object_number=?", (device_id, object_number))
        self.send_json({"deleted": True, "device_id": device_id, "assignments_removed": assignments})

    def update_terminal(self, payload: dict[str, object]) -> None:
        terminal_id = str(payload.get("terminal_id", "")).strip()
        location_name = " ".join(str(payload.get("location", "")).split())
        terminal_number = str(payload.get("terminal_number", "")).strip()
        position = str(payload.get("position", "")).strip().lower()
        terminal_label = make_terminal_label(terminal_number, position)
        wire_color = str(payload.get("wire_color", "")).strip().lower()
        description = str(payload.get("description", "")).strip()
        used = payload.get("used", True)
        if not terminal_id.isdigit():
            self.send_json({"error": "Invalid terminal ID."}, 400)
            return
        if not isinstance(used, bool):
            self.send_json({"error": "Used must be true or false."}, 400)
            return
        if not all((location_name, terminal_number, terminal_label)):
            self.send_json({"error": "Enter the location and terminal number."}, 400)
            return
        if position and not POSITION_CODE.fullmatch(position):
            self.send_json({"error": "Position must be a short code using letters, numbers, dots, or hyphens."}, 400)
            return
        if wire_color and wire_color not in WIRE_COLORS:
            self.send_json({"error": "Choose a wire color from the available color chips."}, 400)
            return
        with connect() as db:
            if not db.execute("SELECT 1 FROM terminals WHERE id=?", (int(terminal_id),)).fetchone():
                self.send_json({"error": "Klemme not found."}, 404)
                return
            location_id = location_id_for(db, location_name)
            fuse_id = resolve_fuse_id(db, payload.get("fuse_id"), payload.get("fuse"), location_id)
            db.execute(
                """UPDATE terminals SET location_id=?, label=?, description=?, terminal_number=?,
                          position=?, wire_color=?, used=? WHERE id=?""",
                (location_id, terminal_label, description, terminal_number, position, wire_color, int(used), int(terminal_id)),
            )
            connections.set_terminal_fuse(db, int(terminal_id), fuse_id)
        self.send_json({"updated": True, "terminal_id": int(terminal_id)})

    def create_terminal(self, payload: dict[str, object]) -> None:
        location_name = " ".join(str(payload.get("location", "")).split())
        terminal_number = str(payload.get("terminal_number", "")).strip()
        position = str(payload.get("position", "")).strip().lower()
        terminal_label = make_terminal_label(terminal_number, position)
        wire_color = str(payload.get("wire_color", "")).strip().lower()
        description = str(payload.get("description", "")).strip()
        used = payload.get("used", True)
        if not isinstance(used, bool):
            self.send_json({"error": "Used must be true or false."}, 400)
            return
        if not location_name or not terminal_number:
            self.send_json({"error": "Enter the location and terminal number."}, 400)
            return
        if position and not POSITION_CODE.fullmatch(position):
            self.send_json({"error": "Position must be a short code using letters, numbers, dots, or hyphens."}, 400)
            return
        if wire_color and wire_color not in WIRE_COLORS:
            self.send_json({"error": "Choose a wire color from the available color chips."}, 400)
            return
        with connect() as db:
            # Checked before anything is written (an early response would still commit the transaction).
            existing = db.execute(
                "SELECT t.id FROM terminals t JOIN locations l ON l.id=t.location_id WHERE l.name=? AND t.label=?",
                (location_name, terminal_label),
            ).fetchone()
            if existing:
                self.send_json({"error": f"Klemme {terminal_label} already exists at {location_name}."}, 409)
                return
            location_id = location_id_for(db, location_name)
            cursor = db.execute(
                """INSERT INTO terminals(location_id, label, description, terminal_number, position, wire_color, used)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (location_id, terminal_label, description, terminal_number, position, wire_color, int(used)),
            )
        self.send_json({"created": True, "terminal_id": int(cursor.lastrowid)})

    def update_review_status(self, payload: dict[str, object]) -> None:
        record_type = str(payload.get("type", "")).strip()
        record_id = str(payload.get("id", "")).strip()
        checked = payload.get("checked")
        if record_type not in {"device", "channel", "terminal", "fuse", "fi"}:
            raise ValueError("This record type cannot be marked as reviewed.")
        if not isinstance(checked, bool):
            raise ValueError("Review status must be checked or unchecked.")
        reviewed_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") if checked else None

        with connect() as db:
            if record_type == "device":
                if not DEVICE_ID.fullmatch(record_id) and not record_id.startswith("manual:"):
                    raise ValueError("Invalid device ID.")
                cursor = db.execute(
                    "UPDATE devices SET reviewed_at=? WHERE device_id=?",
                    (reviewed_at, record_id),
                )
            elif record_type == "channel":
                parts = record_id.rsplit(":", 1)
                if len(parts) != 2 or not parts[1] or not (DEVICE_ID.fullmatch(parts[0]) or parts[0].startswith("manual:")):
                    raise ValueError("Invalid channel ID.")
                channel = db.execute(
                    "SELECT physical_channel FROM channels WHERE device_id=? AND object_number=?", parts
                ).fetchone()
                if not channel:
                    self.send_json({"error": "Channel not found."}, 404)
                    return
                if channel[0]:
                    cursor = db.execute(
                        "UPDATE channels SET reviewed_at=? WHERE device_id=? AND physical_channel=?",
                        (reviewed_at, parts[0], channel[0]),
                    )
                else:
                    cursor = db.execute(
                        "UPDATE channels SET reviewed_at=? WHERE device_id=? AND object_number=?",
                        (reviewed_at, parts[0], parts[1]),
                    )
            else:
                if not record_id.isdigit():
                    raise ValueError(f"Invalid {record_type} ID.")
                table_by_type = {"terminal": "terminals", "fuse": "fuses", "fi": "fi_devices"}
                cursor = db.execute(
                    f"UPDATE {table_by_type[record_type]} SET reviewed_at=? WHERE id=?",
                    (reviewed_at, int(record_id)),
                )
            if cursor.rowcount == 0:
                self.send_json({"error": "Record not found."}, 404)
                return
        self.send_json({"type": record_type, "id": record_id, "reviewed_at": reviewed_at})

    def update_fuse(self, payload: dict[str, object]) -> None:
        fuse_id = payload.get("fuse_id")
        label = normalize_fuse_label(payload.get("label", ""))
        rating = str(payload.get("rating", "")).strip()
        description = str(payload.get("description", "")).strip()
        fi_number = " ".join(str(payload.get("fi_number", "")).split())
        fi_label = " ".join(str(payload.get("fi_label", "")).split())
        existing_fi_id = str(payload.get("existing_fi_id", "")).strip()
        location_name = " ".join(str(payload.get("location", "")).split())
        is_new = not str(fuse_id or "").strip()
        phases_value = payload.get("phases", [])
        if not is_new and not str(fuse_id).isdigit():
            raise ValueError("Invalid fuse ID.")
        if not label:
            raise ValueError("Sicherung designation is required.")
        if is_new and not location_name:
            raise ValueError("Location is required for a new fuse.")
        if not isinstance(phases_value, list):
            raise ValueError("Phases must be a list.")
        phases = {str(phase).strip().upper() for phase in phases_value}
        if not phases.issubset(VALID_PHASES):
            raise ValueError("Choose phases L1, L2, and/or L3.")

        with connect() as db:
            if is_new:
                location_id = location_id_for(db, location_name)
            else:
                fuse = db.execute("SELECT location_id FROM fuses WHERE id=?", (int(fuse_id),)).fetchone()
                if not fuse:
                    raise ValueError("Sicherung not found.")
                location_id = int(fuse["location_id"])
            duplicate = db.execute(
                """SELECT 1 FROM fuses WHERE location_id=? AND label=?
                   AND (?=1 OR id<>?)""",
                (location_id, label, int(is_new), int(fuse_id) if not is_new else -1),
            ).fetchone()
            if duplicate:
                raise ValueError("A fuse with that normalized designation already exists at this location.")
            if existing_fi_id:
                if not existing_fi_id.isdigit():
                    raise ValueError("Invalid FI selection.")
                existing_fi = db.execute(
                    "SELECT id FROM fi_devices WHERE id=? AND location_id=?",
                    (int(existing_fi_id), location_id),
                ).fetchone()
                if not existing_fi:
                    raise ValueError("The selected FI does not belong to this location.")
                fi_id = int(existing_fi["id"])
                if fi_number:
                    duplicate_fi_number = db.execute(
                        "SELECT 1 FROM fi_devices WHERE location_id=? AND number=? AND id<>?",
                        (location_id, fi_number, fi_id),
                    ).fetchone()
                    if duplicate_fi_number:
                        raise ValueError("Another FI at this location already uses that number.")
                db.execute(
                    "UPDATE fi_devices SET number=?, label=? WHERE id=?",
                    (fi_number or None, fi_label, fi_id),
                )
            else:
                fi_id = upsert_fi_device(db, location_id, fi_number, fi_label)
            if is_new:
                cursor = db.execute(
                    "INSERT INTO fuses(location_id,label,rating,description,phases) VALUES (?,?,?,?,?)",
                    (location_id, label, rating, description, join_phases(phases)),
                )
                fuse_id = int(cursor.lastrowid)
            else:
                fuse_id = int(fuse_id)
                db.execute(
                    "UPDATE fuses SET label=?, rating=?, description=?, phases=? WHERE id=?",
                    (label, rating, description, join_phases(phases), fuse_id),
                )
            # The FI of a Sicherung is a relation of the connection tree.
            connections.set_fuse_fi(db, fuse_id, fi_id)
        self.send_json({"created": is_new, "updated": not is_new, "id": fuse_id, "label": label, "phases": sorted(phases)})

    def delete_fuse(self, payload: dict[str, object]) -> None:
        fuse_id = str(payload.get("fuse_id", "")).strip()
        expected_connections = payload.get("expected_connections")
        if not fuse_id.isdigit():
            raise ValueError("Invalid fuse ID.")
        if not isinstance(expected_connections, int) or isinstance(expected_connections, bool) or expected_connections < 0:
            raise ValueError("Invalid active connection count.")

        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            fuse = db.execute("SELECT id, label FROM fuses WHERE id=?", (int(fuse_id),)).fetchone()
            if not fuse:
                self.send_json({"error": "Sicherung not found."}, 404)
                return
            active_connections = int(db.execute(
                "SELECT COUNT(*) FROM terminal_fuse WHERE fuse_id=?", (int(fuse_id),)
            ).fetchone()[0])
            if active_connections != expected_connections:
                self.send_json({
                    "error": "The active connections changed. Reopen the confirmation and try again.",
                    "active_connections": active_connections,
                }, 409)
                return
            db.execute("DELETE FROM fuses WHERE id=?", (int(fuse_id),))
            connections.finish(db)

        self.send_json({
            "deleted": True,
            "id": int(fuse_id),
            "label": fuse["label"],
            "active_connections": active_connections,
        })

    @staticmethod
    def get_or_create_terminal(
        db: sqlite3.Connection,
        location_name: str,
        terminal_number: str,
        position: str = "",
        wire_color: str = "",
    ) -> int:
        terminal_label = make_terminal_label(terminal_number, position)
        location_id = location_id_for(db, location_name)
        db.execute(
            """INSERT INTO terminals(location_id, label, terminal_number, position, wire_color)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(location_id, label) DO UPDATE SET
                 terminal_number=excluded.terminal_number,
                 position=excluded.position,
                 wire_color=CASE WHEN excluded.wire_color <> '' THEN excluded.wire_color ELSE terminals.wire_color END""",
            (location_id, terminal_label, terminal_number, position, wire_color),
        )
        return int(db.execute(
            "SELECT id FROM terminals WHERE location_id=? AND label=?", (location_id, terminal_label)
        ).fetchone()[0])

    def extend_terminal_path(self, payload: dict[str, object]) -> None:
        """Add a Klemme, a device channel or a Verbraucher (existing or created on the fly) at the end of a Klemme's
        path, or as a branch below the connection node `node_id` (next to its existing followers)."""
        terminal_id = payload.get("terminal_id")
        node_id = payload.get("node_id")
        kind = str(payload.get("kind", "")).strip()
        if kind not in ("terminal", "channel", "load"):
            raise ValueError("Choose Klemme, Gerät-Kanal or Verbraucher.")
        with connect() as db:
            if payload.get("insert_node_id") not in (None, ""):
                # Into the wire above a node (Kanal, Klemme, Verbraucher): only a Klemme, new or existing.
                insert_id = payload_id(payload, "insert_node_id", "connection node")
                if kind != "terminal":
                    raise ValueError("Only a Klemme can be inserted into a connection.")
                if not db.execute("SELECT 1 FROM connection_nodes WHERE id=?", (insert_id,)).fetchone():
                    raise ValueError("Connection node not found.")
                self.send_json({"kind": kind, **self.extend_with_terminal(db, insert_id, payload, insert=True)})
                return
            if payload.get("fuse_id") not in (None, ""):
                # Below a Sicherung (Sicherung page): a Klemme (its node is created when it has none yet) or a
                # Verbraucher hanging directly on the Sicherung.
                fuse_id = payload_id(payload, "fuse_id", "Sicherung")
                if kind not in ("terminal", "load"):
                    raise ValueError("Only a Klemme or a Verbraucher can be connected directly to a Sicherung.")
                if not db.execute("SELECT 1 FROM fuses WHERE id=?", (fuse_id,)).fetchone():
                    raise ValueError("Sicherung not found.")
                if kind == "load":
                    load_id = str(payload.get("load_id", "")).strip()
                    name = clean_text(payload.get("load_name"), "The Verbraucher name")
                    result = connections.connect_load_to_fuse(db, fuse_id, int(load_id) if load_id.isdigit() else None, name)
                    self.send_json({"kind": kind, **result})
                    return
                node_id = connections.ensure_fuse_node(db, fuse_id)
            if str(node_id or "").isdigit():
                node = db.execute("SELECT terminal_id, node_type FROM connection_nodes WHERE id=? AND node_type<>'load'",
                                  (int(node_id),)).fetchone()
                if not node:
                    raise ValueError("Connection node not found.")
                node_id = int(node_id)
                if node[1] == "terminal" and kind == "load":
                    # A Verbraucher hangs on the Klemme (behind the last Klemme wired on behind it).
                    terminal_id, node_id = node[0], None
                elif kind == "load" and node[1] != "channel":
                    raise ValueError("A Verbraucher can only be connected to a Klemme or a Gerät-Kanal.")
            else:
                node_id = None
                if not str(terminal_id or "").isdigit():
                    raise ValueError("Invalid Klemme ID.")
            if node_id is None and not db.execute("SELECT 1 FROM terminals WHERE id=?", (int(terminal_id),)).fetchone():
                raise ValueError("Klemme not found.")
            extend = {"terminal": self.extend_with_terminal, "channel": self.extend_with_channel,
                      "load": self.extend_with_load}[kind]
            result = extend(db, node_id if node_id is not None else int(terminal_id), payload, branch=node_id is not None)
        self.send_json({"kind": kind, **result})

    def extend_with_terminal(self, db: sqlite3.Connection, source_id: int, payload: dict[str, object],
                             branch: bool = False, insert: bool = False) -> dict[str, object]:
        # `source_id` is a Klemme id (the new Klemme is wired on behind it) or, for a branch or an insert,
        # a connection node id (below it / into the wire above it).
        location = str(payload.get("location", "")).strip()
        number = str(payload.get("terminal_number", "")).strip()
        position = str(payload.get("position", "")).strip().lower()
        wire_color = str(payload.get("wire_color", "")).strip().lower()
        if not location or not number:
            raise ValueError("Enter the location and the number of the Klemme.")
        if position and not POSITION_CODE.fullmatch(position):
            raise ValueError("Position must be a short code using letters, numbers, dots, or hyphens.")
        if wire_color and wire_color not in WIRE_COLORS:
            raise ValueError("Choose a wire color from the available color chips.")
        existed = db.execute(
            "SELECT 1 FROM terminals t JOIN locations l ON l.id=t.location_id WHERE l.name=? AND t.label=?",
            (location, make_terminal_label(number, position)),
        ).fetchone() is not None
        target_id = self.get_or_create_terminal(db, location, number, position, wire_color)
        if insert:
            # A Klemme on N/PE put into a fused wire becomes the neutral/PE of that device and Verbraucher.
            parent = db.execute("SELECT parent_id FROM connection_nodes WHERE id=?", (source_id,)).fetchone()[0]
            conductor_link = bool(parent) and connections.never_fused(db, target_id) and connections.fused_above(db, parent)
            connections.insert_terminal(db, source_id, target_id)
            return {"terminal_id": target_id, "created": not existed, "already_exists": False, "inserted": True,
                    "conductor_link": conductor_link, "conductor": connections.terminal_wire_phase(db, target_id)}
        if branch:
            new_id = connections.add_branch(db, source_id, "terminal", {"terminal_id": target_id})
            # An N/PE Klemme below a fused Kanal stays at its conductor; the Kanal hangs below it instead.
            conductor_link = db.execute("SELECT node_type FROM connection_nodes WHERE id=?", (new_id,)).fetchone()[0] == "channel"
            return {"terminal_id": target_id, "created": not existed, "already_exists": False, "conductor_link": conductor_link}
        added = connections.add_follower(db, source_id, target_id)
        return {"terminal_id": target_id, "created": not existed, "already_exists": not added}

    def extend_with_load(self, db: sqlite3.Connection, target_id: int, payload: dict[str, object],
                         branch: bool = False) -> dict[str, object]:
        # `target_id` is the Klemme (the Verbraucher is the end point after the last Klemme wired on) or,
        # for a branch, a Gerät-Kanal node (the Klemme above that Kanal feeds the Verbraucher).
        load_id = str(payload.get("load_id", "")).strip()
        name = str(payload.get("load_name", ""))
        if len(name) > 500:
            raise ValueError("The Verbraucher name must be 500 characters or fewer.")
        picked = int(load_id) if load_id.isdigit() else None
        if branch:
            return connections.connect_load(db, None, picked, name, node_id=target_id)
        return connections.connect_load(db, target_id, picked, name)

    def extend_with_channel(self, db: sqlite3.Connection, terminal_id: int, payload: dict[str, object],
                            branch: bool = False) -> dict[str, object]:
        # `terminal_id` is a Klemme id (appended at its path end) or, for a branch, a connection node id.
        device_id = str(payload.get("device_id", "")).strip()
        object_number = str(payload.get("object_number", "")).strip()
        created = False
        if device_id or object_number:
            # An existing ETS or manual channel picked from the search.
            if not (DEVICE_ID.fullmatch(device_id) or device_id.startswith("manual:")):
                raise ValueError("Choose a valid device.")
            row = db.execute(
                "SELECT id FROM channels WHERE device_id=? AND object_number=?", (device_id, object_number)
            ).fetchone()
            if not row:
                raise ValueError("Channel not found.")
            channel_id = int(row[0])
        else:
            # A manual device and channel, reused by name or created on the fly.
            device_name = " ".join(str(payload.get("device_name", "")).split())
            channel_name = " ".join(str(payload.get("channel_name", "")).split())
            if not device_name or not channel_name:
                raise ValueError("Choose an existing channel or enter a device name and a channel name.")
            if len(device_name) > 500 or len(channel_name) > 100:
                raise ValueError("Device name must be 500 characters or fewer; channel name 100 or fewer.")
            device = db.execute(
                "SELECT device_id FROM devices WHERE COALESCE(is_manual, 0)=1 AND manual_name=? COLLATE NOCASE",
                (device_name,),
            ).fetchone()
            device_id = device[0] if device else self.insert_manual_device(db, device_name)
            channel = db.execute(
                "SELECT id, object_number FROM channels WHERE device_id=? AND physical_channel=?", (device_id, channel_name)
            ).fetchone()
            if channel:
                channel_id, object_number = int(channel[0]), channel[1]
            else:
                object_number, channel_id = self.insert_manual_channel(db, device_id, channel_name)
                created = True
        spec = self.channel_spec(db, channel_id)
        if branch:
            connections.add_branch(db, terminal_id, "channel", spec)
        else:
            connections.append_channel_link(db, terminal_id, spec)
        return {"device_id": device_id, "object_number": object_number, "created": created}

    def delete_assignment(self, payload: dict[str, object]) -> None:
        assignment_id = payload.get("assignment_id")
        if not str(assignment_id or "").isdigit():
            self.send_json({"error": "Invalid assignment ID"}, 400)
            return
        with connect() as db:
            connections.remove_assignment(db, int(assignment_id))
        self.send_json({"deleted": True})

    def delete_channel_link(self, payload: dict[str, object]) -> None:
        terminal_id = payload.get("terminal_id")
        device_id = str(payload.get("device_id", "")).strip()
        physical_channel = str(payload.get("physical_channel", "")).strip()
        valid_device = DEVICE_ID.fullmatch(device_id) or device_id.startswith("manual:")
        if not str(terminal_id or "").isdigit() or not valid_device or not physical_channel:
            self.send_json({"error": "A Klemme, device, and physical channel are required."}, 400)
            return
        with connect() as db:
            deleted_links, deleted_assignments = connections.remove_channel_link(
                db, int(terminal_id), device_id, physical_channel
            )
        if not deleted_links and not deleted_assignments:
            self.send_json({"error": "This Klemme is not connected to that physical channel."}, 404)
            return
        self.send_json({
            "deleted": True,
            "deleted_links": deleted_links,
            "deleted_assignments": deleted_assignments,
        })

    def preview_import(self, payload: dict[str, object]) -> None:
        filename = str(payload.get("filename", ""))
        if Path(filename).suffix.lower() != ".xlsx":
            self.send_json({"error": "Please use an .xlsx workbook. The template download uses this format."}, 400)
            return
        try:
            workbook_bytes = base64.b64decode(str(payload.get("file_base64", "")), validate=True)
        except ValueError as exc:
            self.send_json({"error": "Could not decode the uploaded workbook."}, 400)
            return
        if len(workbook_bytes) > MAX_FILE_BYTES:
            self.send_json({"error": "Workbook is larger than 10 MB."}, 400)
            return
        try:
            from openpyxl import load_workbook
        except ImportError:
            self.send_json({"error": "Excel import requires openpyxl. Run: python -m pip install -r requirements.txt"}, 500)
            return
        try:
            workbook = load_workbook(BytesIO(workbook_bytes), read_only=True, data_only=True)
            sheet = workbook.active
            header_cells = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), ())
            headers = {str(value or "").strip().lower(): index for index, value in enumerate(header_cells)}
            aliases = {
                "device_id": ("device_id", "geräteadresse", "geraeteadresse"),
                "object_number": ("object_number", "kanalnummer", "objektnummer"),
                "location": ("location", "schaltschrank location", "schaltschrank", "switchboard"),
                "terminal_number": ("terminal_number", "terminal number", "klemmenummer", "nummer", "terminal", "klemme"),
                "position": ("position", "lage"),
                "wire_color": ("wire_color", "wire color", "kabelfarbe", "farbe"),
                "fuse": ("fuse", "sicherung", "fuse designation", "sicherungsbezeichnung"),
                "fuse_rating": ("fuse_rating", "rating", "nennstrom"),
                "description": ("description", "beschreibung"),
            }
            columns = {}
            for field, names in aliases.items():
                for name in names:
                    if name in headers:
                        columns[field] = headers[name]
                        break
            missing = [field for field in ("device_id", "location", "terminal_number") if field not in columns]
            if "object_number" not in columns:
                missing.append("object_number")
            if missing:
                self.send_json({"error": "Missing required columns: " + ", ".join(missing)}, 400)
                return
            raw_rows = []
            for row_number, values in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
                if not any(value is not None and str(value).strip() for value in values):
                    continue
                item = {field: str(values[index] or "").strip() for field, index in columns.items()}
                if "fuse" in item:
                    item["fuse"] = normalize_fuse_label(item["fuse"])
                raw_rows.append({"row": row_number, **item})
            workbook.close()
        except Exception as exc:
            self.send_json({"error": f"Could not read workbook: {exc}"}, 400)
            return

        preview_rows = []
        with connect() as db:
            for item in raw_rows:
                errors = []
                device_id = str(item.get("device_id", ""))
                object_number = str(item.get("object_number", ""))
                if not DEVICE_ID.fullmatch(device_id):
                    errors.append("Device ID must have three numeric parts.")
                elif not db.execute("SELECT 1 FROM devices WHERE device_id=?", (device_id,)).fetchone():
                    errors.append("Device ID is not present in the imported ETS project.")
                if not object_number:
                    errors.append("ETS object number is required.")
                elif DEVICE_ID.fullmatch(device_id) and not db.execute(
                    "SELECT 1 FROM channels WHERE device_id=? AND object_number=?", (device_id, object_number)
                ).fetchone():
                    errors.append("Channel does not exist for this device.")
                if not item.get("location"):
                    errors.append("Schaltschrank location is required.")
                if not item.get("terminal_number"):
                    errors.append("Klemme is required.")
                if item.get("position") and not POSITION_CODE.fullmatch(item["position"].lower()):
                    errors.append("Position must be a short code using letters, numbers, dots, or hyphens.")
                if item.get("wire_color") and item["wire_color"].lower() not in WIRE_COLORS:
                    errors.append("Unknown wire color code.")
                if item.get("fuse"):
                    # The Sicherung must exist (any room; the one in the Klemme's room wins).
                    location = db.execute("SELECT id FROM locations WHERE name=?", (item.get("location", ""),)).fetchone()
                    try:
                        resolve_fuse_id(db, None, item["fuse"], location[0] if location else None)
                    except ValueError as exc:
                        errors.append(str(exc))
                preview_rows.append({**item, "errors": errors, "valid": not errors})
        token = uuid.uuid4().hex
        PENDING_IMPORTS[token] = preview_rows
        if len(PENDING_IMPORTS) > 10:
            PENDING_IMPORTS.pop(next(iter(PENDING_IMPORTS)))
        self.send_json({
            "token": token,
            "rows": preview_rows[:200],
            "valid_count": sum(row["valid"] for row in preview_rows),
            "invalid_count": sum(not row["valid"] for row in preview_rows),
            "truncated": len(preview_rows) > 200,
        })

    def apply_import(self, payload: dict[str, object]) -> None:
        token = str(payload.get("token", ""))
        rows = PENDING_IMPORTS.pop(token, None)
        if rows is None:
            self.send_json({"error": "Import preview expired. Upload the workbook again."}, 400)
            return
        imported = 0
        skipped = 0
        with connect() as db:
            for row in rows:
                if not row["valid"]:
                    skipped += 1
                    continue
                location_id = location_id_for(db, row["location"])
                # Only existing Sicherungen (checked in the preview); the import never creates or changes one.
                try:
                    fuse_id = resolve_fuse_id(db, None, row.get("fuse"), location_id)
                except ValueError:
                    skipped += 1
                    continue
                db.execute(
                    """INSERT INTO terminals(location_id, label, description, terminal_number, position, wire_color)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(location_id, label) DO UPDATE SET
                         description=CASE WHEN excluded.description <> '' THEN excluded.description ELSE terminals.description END,
                         terminal_number=excluded.terminal_number,
                         position=excluded.position,
                         wire_color=excluded.wire_color""",
                    (location_id, make_terminal_label(row["terminal_number"], row.get("position", "").lower()), row.get("description", ""), row["terminal_number"],
                     row.get("position", "").lower(), row.get("wire_color", "").lower()),
                )
                terminal_id = db.execute(
                    "SELECT id FROM terminals WHERE location_id=? AND label=?",
                    (location_id, make_terminal_label(row["terminal_number"], row.get("position", "").lower())),
                ).fetchone()[0]
                channel_id = db.execute(
                    "SELECT id FROM channels WHERE device_id=? AND object_number=?",
                    (row["device_id"], row["object_number"]),
                ).fetchone()
                if channel_id is None:
                    skipped += 1
                    continue
                existing = db.execute(
                    "SELECT 1 FROM channel_terminals WHERE channel_id=? AND terminal_id=?", (channel_id[0], terminal_id)
                ).fetchone()
                # N and PE Klemmen of a Sicherung row are not fused; they stay at their conductor.
                if fuse_id is not None and not connections.is_unfused_conductor(db, terminal_id):
                    connections.set_terminal_fuse(db, terminal_id, fuse_id)
                connections.add_channel_link(db, terminal_id, self.channel_spec(db, channel_id[0]))
                imported += existing is None
                skipped += existing is not None
        self.send_json({"imported": imported, "skipped": skipped})

    def template(self) -> None:
        try:
            from openpyxl import Workbook
        except ImportError:
            self.send_json({"error": "Excel template requires openpyxl. Run: python -m pip install -r requirements.txt"}, 500)
            return
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Klemmen"
        sheet.append(["device_id", "object_number", "location", "terminal_number", "position", "wire_color", "fuse", "description"])
        output = BytesIO()
        workbook.save(output)
        body = output.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header("Content-Disposition", 'attachment; filename="klemmen-import-template.xlsx"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def export_terminals(self) -> None:
        try:
            import xlsxwriter
        except ImportError:
            self.send_json({"error": "Excel export requires XlsxWriter. Run: python -m pip install -r requirements.txt"}, 500)
            return

        headers = [
            "Ort", "#", "", "", "Verw.", "Gerät", "Kanal",
            "FI", "FI-Bezeichnung", "Sa", "A", "Phase", "Beschreibung",
        ]
        with connect() as db:
            records = db.execute(
                f"""WITH linked_channels AS (
                  SELECT link.terminal_id, link.device_id, link.physical_channel
                       FROM terminal_channel_links link
                  GROUP BY link.terminal_id, link.device_id, link.physical_channel
                       UNION
                  SELECT assignment.terminal_id, channel.device_id, channel.physical_channel
                       FROM channel_terminals assignment
                       JOIN channels channel ON channel.id=assignment.channel_id
                  GROUP BY assignment.terminal_id, channel.device_id, channel.physical_channel
                   )
                     SELECT location.name AS location, terminal.terminal_number,
                         terminal.position, terminal.wire_color, terminal.used,
                     linked_channels.device_id, linked_channels.physical_channel,
                          fuse.label AS fuse_label, fuse.rating AS fuse_rating,
                          terminal.description,
                          fi.number AS fi_number,
                          COALESCE(fi.label, '') AS fi_label,
                          COALESCE({phases_display('fuse')}, '') AS phases
                   FROM terminals terminal
                   JOIN locations location ON location.id=terminal.location_id
                   LEFT JOIN linked_channels ON linked_channels.terminal_id=terminal.id
                   {connections.terminal_fuse_join('terminal', 'fuse')}
                   {connections.fuse_fi_join('fuse', 'fi')}
                   ORDER BY location.name COLLATE NATURAL_SORT,
                            terminal.terminal_number COLLATE NATURAL_SORT,
                            terminal.position COLLATE NATURAL_SORT DESC,
                            linked_channels.device_id COLLATE NATURAL_SORT,
                            linked_channels.physical_channel COLLATE NATURAL_SORT"""
            ).fetchall()

        output = BytesIO()
        workbook = xlsxwriter.Workbook(output, {"in_memory": True})
        sheet = workbook.add_worksheet("Klemmen")
        thin = 1
        row_formats = {}
        header_format = workbook.add_format({
            "bold": True,
            "font_color": "#FFFFFF",
            "bg_color": "#15372E",
            "pattern": 1,
            "valign": "vcenter",
            "text_wrap": False,
            "left": thin,
            "right": thin,
            "top": thin,
            "bottom": thin,
            "border_color": "#B8C2BA",
        })

        def row_format(top: bool, bottom: bool, color: str | None = None, centered: bool = False, box: bool = False):
            key = (top, bottom, color, centered, box)
            if key not in row_formats:
                options = {
                    "left": thin,
                    "right": thin,
                    "top": thin if top else 0,
                    "bottom": thin if bottom else 0,
                    "border_color": "#B8C2BA",
                    "valign": "vcenter",
                    "text_wrap": False,
                }
                if color:
                    options.update({"bg_color": f"#{color}", "pattern": 1})
                if centered:
                    options["align"] = "center"
                if box:
                    # 1/0 shown as ☑/☐: works in every Excel, unlike cell checkboxes (Microsoft 365 2407+ only).
                    options.update({"num_format": '"☑";"☑";"☐"', "font_size": 13})
                row_formats[key] = workbook.add_format(options)
            return row_formats[key]

        for column, header in enumerate(headers):
            sheet.write(0, column, header, header_format)
        sheet.set_row(0, 18)
        sheet.hide_gridlines(2)
        sheet.freeze_panes(1, 0)
        sheet.autofilter(0, 0, len(records), len(headers) - 1)

        rows = []
        for record in records:
            rows.append([
                record["location"], record["terminal_number"], record["position"],
                None, None, record["device_id"], record["physical_channel"],
                record["fi_number"], record["fi_label"], record["fuse_label"],
                record["fuse_rating"], record["phases"], record["description"],
            ])

        for index, (record, values) in enumerate(zip(records, rows)):
            current_group = (record["location"], record["terminal_number"])
            previous_group = (
                (records[index - 1]["location"], records[index - 1]["terminal_number"])
                if index > 0 else None
            )
            next_group = (
                (records[index + 1]["location"], records[index + 1]["terminal_number"])
                if index + 1 < len(records) else None
            )
            top = previous_group != current_group
            bottom = next_group != current_group
            row_number = index + 1
            for column, value in enumerate(values):
                if column == 4:
                    sheet.write_number(row_number, column, 1 if record["used"] else 0,
                                       row_format(top, bottom, centered=True, box=True))
                elif column == 3:
                    color = WIRE_COLOR_EXCEL_STYLES.get(str(record["wire_color"]).lower())
                    sheet.write_blank(row_number, column, None, row_format(top, bottom, color))
                elif value is None:
                    sheet.write_blank(row_number, column, None, row_format(top, bottom))
                else:
                    sheet.write(row_number, column, value, row_format(top, bottom))
            sheet.set_row(row_number, 18)
        # "Verw." stays editable: a dropdown with 1 (☑) and 0 (☐).
        if records:
            sheet.data_validation(1, 4, len(records), 4, {
                "validate": "list", "source": [1, 0],
                "input_title": "In Verwendung", "input_message": "1 = ☑ in Verwendung\n0 = ☐ frei",
            })

        for column in range(len(headers)):
            values = (row[column] for row in rows if row[column] is not None)
            content_width = max((len(str(value)) for value in values), default=0)
            width = 4 if column == 3 else 7 if column == 4 else max(4, content_width + 2)
            if column == 12:
                width = min(width, 60)
            sheet.set_column(column, column, width)

        workbook.close()
        body = output.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header("Content-Disposition", 'attachment; filename="Klemmleistenbelegung.xlsx"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, filename: str) -> None:
        path = WEB_DIR / filename
        if not path.is_file() or path.parent != WEB_DIR:
            self.send_json({"error": "Not found"}, 404)
            return
        body = path.read_bytes()
        if filename == "index.html":
            # The small line above "Schaltschrank" (add-on option `titel`), set before the page is shown.
            body = body.replace(b"__APP_TITLE__", html.escape(app_title()).encode("utf-8"))
        content_type = "text/html; charset=utf-8" if filename.endswith(".html") else (
            "text/javascript; charset=utf-8" if filename.endswith(".js") else "text/css; charset=utf-8"
        )
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Always fetch the current page, script and styles; a cached old app.js looks like an unapplied change.
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")


def main() -> None:
    global DB_PATH, ALLOWED_CLIENTS
    parser = argparse.ArgumentParser(description="Run the local Schaltschrank relationship browser.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--ingress", action="store_true",
                        help="Home Assistant add-on: listen in the container, answer only the ingress proxy.")
    parser.add_argument("--create-if-missing", action="store_true",
                        help="Start on a new empty database when none exists (the data comes with 'Datenbank importieren').")
    args = parser.parse_args()
    if args.ingress:
        # Only inside the add-on (the Supervisor hands it a token); Home Assistant does the login.
        if not home_assistant.in_addon():
            parser.error("--ingress runs only as Home Assistant add-on (SUPERVISOR_TOKEN missing).")
        args.host = "0.0.0.0"
        ALLOWED_CLIENTS = INGRESS_CLIENTS
    elif args.host not in {"127.0.0.1", "localhost"}:
        parser.error("The unauthenticated, write-capable server may only bind to localhost (127.0.0.1).")
    DB_PATH = args.db
    # Backups and older generations are history: never opened for writing (nor created anew). The generations
    # (schaltschrank.sqlite3 … schaltschrank7.sqlite3) live in the project folder; elsewhere, such as /data of the
    # Home Assistant add-on, "schaltschrank.sqlite3" is just the database's name.
    old_generation = DB_PATH.resolve().parent == APP_DIR and re.fullmatch(r"schaltschrank[2-7]?\.sqlite3", DB_PATH.name)
    if DB_PATH.resolve().parent.name == "backups" or old_generation:
        parser.error(f"{DB_PATH} is a backup or an older generation; open a copy instead.")
    if not DB_PATH.is_file() and args.create_if_missing:
        create_empty_database(DB_PATH)
        print(f"New empty database: {DB_PATH}")
    if not DB_PATH.is_file():
        parser.error(f"Database not found: {DB_PATH}. Run import_all_sources.py to build it from the ETS project and terminal workbook.")
    backup = backup_and_prune()
    if backup:
        print(f"Verified pre-start database backup: {backup}")
    initialize_database()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Schaltschrank browser: http://{args.host}:{args.port}")
    print(f"Database: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()
    # Restart requested in the settings (`POST /api/restart`): the same command again, so new code on disk is
    # loaded (with the usual verified backup before the start).
    if RESTART_REQUESTED.is_set():
        print("Restarting …", flush=True)
        os.execv(sys.executable, [sys.executable, *sys.argv])


if __name__ == "__main__":
    main()
