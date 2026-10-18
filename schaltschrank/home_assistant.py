"""Home Assistant: read-only link of the KNX entities to the cabinet.

Home Assistant is never changed. The KNX panel entities (with their group addresses) and the entity registry are
read from Home Assistant's `.storage` folder: in the add-on the config folder mounted read-only, locally the copies
in `from_HA/`. Live states come from the Home Assistant API, but only inside the add-on (Supervisor token, GET
only). Nothing of it is stored in the database: the mapping is worked out anew from the files and the tree.

Mapping of one entity (via its group addresses, never guessed):
  1. the channel must carry a command and a status address of the entity (an actuator reports its state,
     a push-button mostly only sends);
  2. a tie (a push-button with a status LED, a motion detector and the actuator it switches): the channel that
     sends the status address wins (ETS direction S), then the channel wired in the tree, then a real output
     channel (A, B, ...) over a bare ETS object, then the channel with most of the entity's addresses;
  3. still several channels: if they all are on one device, or one device has most of the addresses, the entity
     links to that device; else it is ambiguous and goes to the review list.
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
# Where `knx/config_store.json` and `core.entity_registry` are read from (the add-on sets it to the mounted
# `.storage`); the local fallback are the copies in `from_HA/`, where they may lie flat and with a `.txt` end.
STORAGE_DIR = Path(os.environ.get("SCHALTSCHRANK_HA_STORAGE", APP_DIR.parent / "from_HA"))
SOURCE_FILES = {
    "config_store": ("knx/config_store.json", "config_store.json"),
    "entity_registry": ("core.entity_registry", "core.entity_registry.txt"),
}
# Inside the add-on the Supervisor proxies the Home Assistant API for this token.
SUPERVISOR_API = "http://supervisor/core/api"
STATE_TIMEOUT = 4
STATUS_LABELS = {"channel": "Kanal", "device": "Gerät", "ambiguous": "mehrdeutig", "unknown": "unbekannt"}

_file_cache: dict[Path, tuple[float, object]] = {}


def source_path(kind: str) -> Path | None:
    """The first existing file of one source kind, else None."""
    for name in SOURCE_FILES[kind]:
        path = STORAGE_DIR / name
        if path.is_file():
            return path
    return None


def read_json(path: Path) -> object:
    """A JSON file, parsed once per change of the file."""
    mtime = path.stat().st_mtime
    cached = _file_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    data = json.loads(path.read_text(encoding="utf-8"))
    _file_cache[path] = (mtime, data)
    return data


def available() -> bool:
    return source_path("config_store") is not None


def in_addon() -> bool:
    """Runs as Home Assistant add-on (the Supervisor hands it an API token)."""
    return bool(os.environ.get("SUPERVISOR_TOKEN"))


def entity_addresses(config: object) -> tuple[set[str], set[str]]:
    """Command (`write`) and status (`state`, `passive`) addresses of one KNX panel entity."""
    command: set[str] = set()
    status: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, dict):
            if value.get("write"):
                command.add(str(value["write"]))
            if value.get("state"):
                status.add(str(value["state"]))
            status.update(str(item) for item in value.get("passive") or [])
            for item in value.values():
                walk(item)

    walk(config)
    return command, status


def panel_entities() -> list[dict[str, object]]:
    """The KNX panel entities with entity_id and name from the registry."""
    store_path = source_path("config_store")
    if not store_path:
        return []
    store = read_json(store_path).get("data", {}).get("entities", {})
    registry_path = source_path("entity_registry")
    registry = {}
    if registry_path:
        registry = {
            entry.get("unique_id"): entry
            for entry in read_json(registry_path).get("data", {}).get("entities", [])
            if entry.get("platform") == "knx"
        }
    entities = []
    for platform, items in store.items():
        for unique_id, item in items.items():
            entry = registry.get(unique_id, {})
            command, status = entity_addresses(item.get("knx", {}))
            entities.append({
                "entity_id": entry.get("entity_id") or "",
                "name": entry.get("name") or entry.get("original_name") or (item.get("entity") or {}).get("name") or unique_id,
                "platform": platform,
                "command": command,
                "status": status,
            })
    return entities


def live_states() -> dict[str, dict[str, object]]:
    """Current states by entity_id (add-on only, GET only); empty when Home Assistant does not answer."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return {}
    request = urllib.request.Request(f"{SUPERVISOR_API}/states", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=STATE_TIMEOUT) as response:
            states = json.load(response)
    except (OSError, ValueError):
        return {}
    return {
        item["entity_id"]: {"state": item.get("state"), "unit": (item.get("attributes") or {}).get("unit_of_measurement") or ""}
        for item in states if isinstance(item, dict) and item.get("entity_id")
    }


def channel_index(db: sqlite3.Connection) -> tuple[dict[str, set[tuple[str, str]]], dict[str, set[tuple[str, str]]]]:
    """Channels (device, channel key) per group address, and the channels that send on it (ETS direction S:
    the object's sending address; a listening object has it as R)."""
    by_address: dict[str, set[tuple[str, str]]] = defaultdict(set)
    senders: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for address, device_id, key, direction in db.execute(
        """SELECT x.address, c.device_id,
                  COALESCE(NULLIF(c.physical_channel, ''), 'object:' || c.object_number), x.direction
           FROM channel_group_addresses x JOIN channels c ON c.id=x.channel_id"""
    ):
        by_address[address].add((device_id, key))
        if direction == "S":
            senders[address].add((device_id, key))
    return by_address, senders


def channel_tree(db: sqlite3.Connection) -> dict[tuple[str, str], dict[str, set[int]]]:
    """Per wired channel (device, channel key): the Sicherungen and FI above its nodes, the Verbraucher below,
    the Klemmen it feeds (`fed`) and the Klemme feeding it (`feeding`).

    The tree is read once and walked here (the recursive ancestry view per channel is far too slow)."""
    nodes = {row["id"]: dict(row) for row in db.execute(
        "SELECT id, parent_id, node_type, fuse_id, fi_id, load_id, terminal_id, device_id, channel_key FROM connection_nodes"
    )}
    tree: dict[tuple[str, str], dict[str, set[int]]] = defaultdict(lambda: {"fuses": set(), "fis": set(), "loads": set()})

    def above(node_id: int):
        parent = nodes[node_id]["parent_id"]
        while parent is not None:
            yield nodes[parent]
            parent = nodes[parent]["parent_id"]

    for node in nodes.values():
        channels = [item for item in above(node["id"]) if item["node_type"] == "channel"]
        if node["node_type"] == "channel":
            channels.append(node)
            entry = tree[(node["device_id"], node["channel_key"])]
            entry.setdefault("feeding", set())
            for item in above(node["id"]):
                if item["node_type"] == "fuse":
                    entry["fuses"].add(item["fuse_id"])
                elif item["node_type"] == "fi":
                    entry["fis"].add(item["fi_id"])
            # The nearest Klemme above the channel feeds it (an actuator in the wall box: Sa 3 -> 18/u -> L -> Kanal).
            feeding = next((item for item in above(node["id"]) if item["node_type"] == "terminal"), None)
            if feeding:
                entry["feeding"].add(feeding["terminal_id"])
        elif node["node_type"] == "load":
            for item in channels:
                tree[(item["device_id"], item["channel_key"])]["loads"].add(node["load_id"])
        elif node["node_type"] == "terminal":
            # A Klemme whose nearest Klemme or channel above is a channel is fed by it (Kanal -> 10/u); read from
            # the tree, so a channel wired as ETS object counts too (the link view leaves those out).
            nearest = next((item for item in above(node["id"]) if item["node_type"] in ("terminal", "channel")), None)
            if nearest and nearest["node_type"] == "channel":
                tree[(nearest["device_id"], nearest["channel_key"])].setdefault("fed", set()).add(node["terminal_id"])
    return tree


def match_entity(entity: dict[str, object], by_address, senders, wired) -> dict[str, object]:
    """Status and target (channel or device) of one entity, after the rules in the module text."""
    command, status = entity["command"], entity["status"]
    score: Counter = Counter()
    for address in command | status:
        score.update(by_address.get(address, ()))
    if not score:
        return {"status": "unknown", "note": "Gruppenadresse nicht in der Datenbank"}

    def carries(channel, addresses, index=by_address) -> bool:
        return any(channel in index.get(address, ()) for address in addresses)

    def rank(channel) -> tuple:
        actuator = carries(channel, status) and (not command or carries(channel, command))
        # The value comes from the channel that sends the status (a motion detector, not the actuator it switches).
        source = carries(channel, status, senders)
        return (actuator, source, channel in wired, not channel[1].startswith("object:"), score[channel])

    best_rank = max(rank(channel) for channel in score)
    best = [channel for channel in score if rank(channel) == best_rank]
    if len(best) == 1:
        return {"status": "channel", "device_id": best[0][0], "channel_key": best[0][1]}
    # Count the different addresses per device (not the channel hits): a central address on many channels of
    # one actuator must not win over a device that carries more of the entity's own addresses.
    candidates = {channel[0] for channel in best}
    devices = Counter({
        device_id: sum(1 for address in command | status if any(item[0] == device_id for item in by_address.get(address, ())))
        for device_id in candidates
    })
    ranked = devices.most_common(2)
    if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
        return {"status": "device", "device_id": ranked[0][0], "note": "mehrere Kanäle dieses Geräts"}
    return {"status": "ambiguous", "note": f"{len(best)} Kanäle auf {len(devices)} Geräten möglich"}


def ids_query(db: sqlite3.Connection, query: str, ids: set[int]) -> list[dict[str, object]]:
    """Rows of `query` (with `{ids}` for the placeholders) for a set of ids."""
    if not ids:
        return []
    return [dict(row) for row in db.execute(query.format(ids=",".join("?" * len(ids))), sorted(ids))]


def attach_cabinet(db: sqlite3.Connection, entity: dict[str, object], tree) -> None:
    """Device label, channel page id, and the Klemmen, Sicherung, FI and Verbraucher behind the channel."""
    device = db.execute(
        "SELECT COALESCE(NULLIF(manual_name, ''), device_id) AS label, product FROM devices WHERE device_id=?",
        (entity["device_id"],),
    ).fetchone()
    entity["device_label"] = device["label"] if device else entity["device_id"]
    entity["product"] = device["product"] if device else ""
    key = entity.get("channel_key")
    if not key:
        return
    if key.startswith("object:"):
        object_number = key.split(":", 1)[1]
        entity["channel_label"] = f"ETS-Objekt {object_number}"
    else:
        row = db.execute(
            """SELECT object_number FROM channels WHERE device_id=? AND physical_channel=?
               ORDER BY CAST(object_number AS INTEGER), object_number LIMIT 1""",
            (entity["device_id"], key),
        ).fetchone()
        object_number = row["object_number"] if row else ""
        entity["channel_label"] = f"Kanal {key}"
    entity["channel_id"] = f"{entity['device_id']}:{object_number}"
    channel = (entity["device_id"], key)
    # Klemmen of the channel, Sicherung and FI above its nodes, Verbraucher below them.
    place = tree.get(channel, {"fuses": set(), "fis": set(), "loads": set()})
    # The Klemmen the channel feeds (in the cabinet: Kanal -> 10/u); without one, the Klemme feeding it.
    entity["terminals"] = ids_query(
        db, """SELECT t.id, t.label, l.name AS location FROM terminals t JOIN locations l ON l.id=t.location_id
               WHERE t.id IN ({ids}) ORDER BY t.label COLLATE NATURAL_SORT""",
        place.get("fed") or place.get("feeding", set()))
    entity["fuses"] = ids_query(db, "SELECT id, label FROM fuses WHERE id IN ({ids}) ORDER BY label COLLATE NATURAL_SORT", place["fuses"])
    entity["fis"] = ids_query(db, "SELECT id, number, label FROM fi_devices WHERE id IN ({ids}) ORDER BY number", place["fis"])
    entity["loads"] = ids_query(db, "SELECT id, name FROM loads WHERE id IN ({ids}) ORDER BY name COLLATE NATURAL_SORT", place["loads"])


def target(item: dict[str, object]) -> tuple[str, str]:
    """The page an entity opens (symbol in Home Assistant, search, overview): its Klemmen path if the channel
    has a Klemme (the one it feeds, else the one feeding it; the first, naturally sorted), else its Verbraucher,
    else the channel, else the device;
    ("", "") when it has no clear place."""
    for key, page in (("terminals", "terminal"), ("loads", "load")):
        if item.get(key):
            return page, str(item[key][0]["id"])
    if item.get("channel_id"):
        return "channel", item["channel_id"]
    if item.get("device_id"):
        return "device", item["device_id"]
    return "", ""


def mapped_entities(db: sqlite3.Connection, states: bool = False, device_id: str | None = None) -> list[dict[str, object]]:
    """All KNX panel entities with their place in the cabinet (only those of one device with `device_id`)."""
    by_address, senders = channel_index(db)
    tree = channel_tree(db)
    wired = set(tree)
    current = live_states() if states else {}
    result = []
    for entity in panel_entities():
        match = match_entity(entity, by_address, senders, wired)
        if device_id is not None and match.get("device_id") != device_id:
            continue
        item = {
            "entity_id": entity["entity_id"],
            "name": entity["name"],
            "platform": entity["platform"],
            "addresses": sorted(entity["command"] | entity["status"]),
            **match,
        }
        if item.get("device_id"):
            attach_cabinet(db, item, tree)
        item["target_type"], item["target_id"] = target(item)
        if entity["entity_id"] in current:
            item["state"] = current[entity["entity_id"]]
        result.append(item)
    order = {"channel": 0, "device": 1, "ambiguous": 2, "unknown": 3}
    result.sort(key=lambda item: (order[item["status"]], item["platform"], item["name"].lower()))
    return result
