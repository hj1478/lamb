#!/usr/bin/env python3
"""Keep a local database of every EarthMC town (API v4), refreshed in rotating cycles.

Each run:
  1. GET /towns (1 request) to get the current list of towns.
  2. Fetch any town not yet in the database, plus the next slice of the rotation.
     Towns are walked in UUID order; the slice size is ceil(total / days_per_cycle),
     so every town is refreshed exactly once per cycle with no overlap. A cursor in
     data/state.json remembers where the previous run stopped.
  3. Delete towns that no longer exist, then rebuild the SQLite DB and the CSV.

Outputs (in ./data):
  towns/<uuid>.json        - {"fetched_at": ..., "data": <unmodified API response for the town>}
  state.json               - rotation cursor / cycle counter
  towns.db                 - SQLite: `towns` (flattened + raw JSON), `town_blocks` (gitignored)
  outsider_spawn_towns.csv - towns with status.canOutsidersSpawn = true

Rate limiting: up to 100 towns per POST, >= MIN_INTERVAL between requests,
exponential backoff (or Retry-After) on 429/5xx.
"""
import argparse
import bisect
import csv
import json
import math
import os
import sqlite3
import time
import urllib.error
import urllib.request

API = "https://api.earthmc.net/v4"
BATCH = 100
MIN_INTERVAL = 1.0  # seconds between requests (~60/min, well under the limit)
MAX_RETRIES = 6
UA = "lamb-earthmc-town-db/1.0"

_last = 0.0


def request(path, body=None):
    global _last
    for attempt in range(MAX_RETRIES):
        wait = MIN_INTERVAL - (time.monotonic() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.monotonic()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            API + path, data=data, method="POST" if data else "GET",
            headers={"Content-Type": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise
            retry_after = e.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else 2 ** (attempt + 1)
        except (urllib.error.URLError, TimeoutError):
            delay = 2 ** (attempt + 1)
        print(f"  retry {attempt + 1}/{MAX_RETRIES} in {delay:.0f}s")
        time.sleep(delay)
    raise RuntimeError(f"giving up on {path}")


def g(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def build_db(records, path):
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE towns (
      uuid TEXT PRIMARY KEY, name TEXT, board TEXT, founder TEXT, wiki TEXT, discord TEXT,
      mayor_name TEXT, mayor_uuid TEXT, nation_name TEXT, nation_uuid TEXT,
      registered INTEGER, joined_nation_at INTEGER, ruined_at INTEGER,
      is_public INT, is_open INT, is_neutral INT, is_capital INT, is_overclaimed INT,
      is_ruined INT, is_for_sale INT, has_nation INT, can_outsiders_spawn INT,
      can_passive_mobs_spawn INT, has_snow_accumulation INT, has_friendly_fire INT,
      num_town_blocks INT, max_town_blocks INT, bonus_blocks INT, nation_bonus INT,
      num_residents INT, num_trusted INT, num_outlaws INT, balance REAL, for_sale_price REAL,
      flag_pvp INT, flag_explosion INT, flag_fire INT, flag_mobs INT,
      spawn_world TEXT, spawn_x REAL, spawn_y REAL, spawn_z REAL, spawn_pitch REAL, spawn_yaw REAL,
      home_block_x INT, home_block_z INT,
      fetched_at TEXT, raw_json TEXT
    );
    CREATE TABLE town_blocks (town_uuid TEXT, x INT, z INT);
    CREATE INDEX idx_blocks_town ON town_blocks(town_uuid);
    CREATE INDEX idx_towns_name ON towns(name);
    """)
    for rec in records:
        t, fetched_at = rec["data"], rec["fetched_at"]
        sp = g(t, "coordinates", "spawn") or {}
        hb = g(t, "coordinates", "homeBlock") or [None, None]
        st, s, fl = t.get("status") or {}, t.get("stats") or {}, g(t, "perms", "flags") or {}
        db.execute(f"INSERT INTO towns VALUES ({','.join('?' * 48)})", (
            t["uuid"], t.get("name"), t.get("board"), t.get("founder"), t.get("wiki"), t.get("discord"),
            g(t, "mayor", "name"), g(t, "mayor", "uuid"), g(t, "nation", "name"), g(t, "nation", "uuid"),
            g(t, "timestamps", "registered"), g(t, "timestamps", "joinedNationAt"), g(t, "timestamps", "ruinedAt"),
            st.get("isPublic"), st.get("isOpen"), st.get("isNeutral"), st.get("isCapital"),
            st.get("isOverClaimed"), st.get("isRuined"), st.get("isForSale"), st.get("hasNation"),
            st.get("canOutsidersSpawn"), st.get("canPassiveMobsSpawn"), st.get("hasSnowAccumulation"),
            st.get("hasFriendlyFire"),
            s.get("numTownBlocks"), s.get("maxTownBlocks"), s.get("bonusBlocks"), s.get("nationBonus"),
            s.get("numResidents"), s.get("numTrusted"), s.get("numOutlaws"), s.get("balance"), s.get("forSalePrice"),
            fl.get("pvp"), fl.get("explosion"), fl.get("fire"), fl.get("mobs"),
            sp.get("world"), sp.get("x"), sp.get("y"), sp.get("z"), sp.get("pitch"), sp.get("yaw"),
            hb[0], hb[1], fetched_at, json.dumps(t, ensure_ascii=False)))
        db.executemany("INSERT INTO town_blocks VALUES (?,?,?)",
                       [(t["uuid"], b[0], b[1]) for b in g(t, "coordinates", "townBlocks") or []])
    db.commit()
    return db


def load_towns(towns_dir):
    towns = {}
    for fn in os.listdir(towns_dir):
        if fn.endswith(".json"):
            with open(os.path.join(towns_dir, fn)) as f:
                towns[fn[:-5]] = json.load(f)
    return towns


def save_town(towns_dir, town, fetched_at):
    path = os.path.join(towns_dir, town["uuid"] + ".json")
    with open(path, "w") as f:
        json.dump({"fetched_at": fetched_at, "data": town}, f, ensure_ascii=False, indent=1)


def pick_slice(uuids, cursor, per_run):
    """Next `per_run` UUIDs after `cursor` in sorted order, wrapping once. Returns (slice, wrapped)."""
    start = bisect.bisect_right(uuids, cursor) if cursor else 0
    chosen = uuids[start:start + per_run]
    wrapped = start + per_run >= len(uuids)
    if wrapped:
        chosen += uuids[:min(per_run - len(chosen), start)]
    return chosen, wrapped


def update(out, days_per_cycle):
    towns_dir = os.path.join(out, "towns")
    state_path = os.path.join(out, "state.json")
    os.makedirs(towns_dir, exist_ok=True)
    state = {"cursor": None, "cycle": 1, "days_per_cycle": days_per_cycle}
    if os.path.exists(state_path):
        with open(state_path) as f:
            state.update(json.load(f))

    uuids = sorted(t["uuid"] for t in request("/towns"))
    live = set(uuids)
    stored = {fn[:-5] for fn in os.listdir(towns_dir) if fn.endswith(".json")}

    per_run = math.ceil(len(uuids) / days_per_cycle)
    rotation, wrapped = pick_slice(uuids, state["cursor"], per_run)
    new = [u for u in uuids if u not in stored]
    targets = list(dict.fromkeys(new + rotation))
    print(f"{len(uuids)} towns live; fetching {len(targets)} "
          f"({len(new)} new, {len(rotation)} rotation, cycle {state['cycle']}, {days_per_cycle}-day cycle)")

    fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    got = set()
    for i in range(0, len(targets), BATCH):
        for t in request("/towns", {"query": targets[i:i + BATCH]}):
            save_town(towns_dir, t, fetched_at)
            got.add(t["uuid"])
    missing = set(targets) - got  # disappeared between the index call and the fetch
    gone = (stored - live) | missing
    for u in gone:
        p = os.path.join(towns_dir, u + ".json")
        if os.path.exists(p):
            os.remove(p)
    print(f"updated {len(got)}, removed {len(gone)} deleted towns")

    state.update(cursor=rotation[-1] if rotation else None, days_per_cycle=days_per_cycle,
                 last_run=fetched_at, last_run_fetched=len(got),
                 cycle=state["cycle"] + 1 if wrapped else state["cycle"])
    with open(state_path, "w") as f:
        json.dump(state, f, indent=1)


def export(out):
    towns = load_towns(os.path.join(out, "towns"))
    db = build_db(towns.values(), os.path.join(out, "towns.db"))
    rows = db.execute("""SELECT name, nation_name, mayor_name, num_residents, is_public, is_open,
                                spawn_world, spawn_x, spawn_y, spawn_z, fetched_at
                         FROM towns WHERE can_outsiders_spawn = 1 ORDER BY name COLLATE NOCASE""").fetchall()
    with open(os.path.join(out, "outsider_spawn_towns.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "nation", "mayor", "residents", "public", "open", "world", "x", "y", "z", "fetched_at"])
        for r in rows:
            w.writerow(list(r[:7]) + [round(v, 1) if v is not None else None for v in r[7:10]] + [r[10]])
    print(f"{len(towns)} towns in DB; {len(rows)} allow outsider spawn")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data")
    ap.add_argument("--days-per-cycle", type=int, default=int(os.environ.get("DAYS_PER_CYCLE", 7)),
                    help="refresh every town once per this many runs (1 = full scan each run)")
    ap.add_argument("--export-only", action="store_true", help="rebuild DB/CSV from stored files, no API calls")
    a = ap.parse_args()
    if not a.export_only:
        update(a.out, max(1, a.days_per_cycle))
    export(a.out)


if __name__ == "__main__":
    main()
