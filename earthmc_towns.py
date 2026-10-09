#!/usr/bin/env python3
"""Fetch every town from the EarthMC API (v4) and build a local database.

Outputs (in ./data by default):
  towns_raw.json        - full, unmodified API response for every town (incl. coordinates)
  towns.db              - SQLite DB: `towns` (flattened columns + raw JSON), `town_blocks`
  outsider_spawn_towns.csv - towns where status.canOutsidersSpawn is true, with spawn coords

Rate limiting: EarthMC allows ~180 req/min and 100 towns per POST. We batch 100
names per request, wait MIN_INTERVAL between requests, and back off on 429/5xx
(honouring Retry-After when present).
"""
import argparse
import csv
import json
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


def fetch_all():
    index = request("/towns")
    uuids = [t["uuid"] for t in index]
    print(f"{len(uuids)} towns listed; fetching in batches of {BATCH}")
    towns = []
    for i in range(0, len(uuids), BATCH):
        towns.extend(request("/towns", {"query": uuids[i:i + BATCH]}))
        print(f"  {min(i + BATCH, len(uuids))}/{len(uuids)}")
    return towns


def g(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def build_db(towns, path, fetched_at):
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
    for t in towns:
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data")
    ap.add_argument("--from-raw", action="store_true", help="rebuild DB from existing towns_raw.json without hitting the API")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    raw_path = os.path.join(a.out, "towns_raw.json")

    if a.from_raw:
        with open(raw_path) as f:
            payload = json.load(f)
    else:
        fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        payload = {"fetched_at": fetched_at, "source": API + "/towns", "towns": fetch_all()}
        with open(raw_path, "w") as f:
            json.dump(payload, f, ensure_ascii=False)

    db = build_db(payload["towns"], os.path.join(a.out, "towns.db"), payload["fetched_at"])
    rows = db.execute("""SELECT name, nation_name, mayor_name, num_residents, is_public, is_open,
                                spawn_world, spawn_x, spawn_y, spawn_z
                         FROM towns WHERE can_outsiders_spawn = 1 ORDER BY name COLLATE NOCASE""").fetchall()
    with open(os.path.join(a.out, "outsider_spawn_towns.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "nation", "mayor", "residents", "public", "open", "world", "x", "y", "z"])
        for r in rows:
            w.writerow(list(r[:7]) + [round(v, 1) if v is not None else None for v in r[7:]])
    total = db.execute("SELECT COUNT(*) FROM towns").fetchone()[0]
    print(f"{total} towns stored; {len(rows)} allow outsider spawn")


if __name__ == "__main__":
    main()
