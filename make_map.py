#!/usr/bin/env python3
"""Build map/towns.json for map/index.html from the stored town files.

towns.json layout (kept compact; ~5.8k towns, ~450k claimed chunks):
  nations: [name, ...]
  towns:   [[name, nationIdx|-1, mayor, residents, flags, spawnX, spawnY, spawnZ, fetchedAt, runs], ...]
  flags:   1 outsider spawn, 2 public, 4 open, 8 capital, 16 ruined, 32 neutral, 64 for sale
  runs:    claimed chunks as flat [z, x0, length, ...] horizontal runs (chunk coords)
"""
import itertools
import json
import os

from earthmc_towns import load_towns

FLAGS = [("canOutsidersSpawn", 1), ("isPublic", 2), ("isOpen", 4), ("isCapital", 8),
         ("isRuined", 16), ("isNeutral", 32), ("isForSale", 64)]


def runs(blocks):
    out = []
    for z, row in itertools.groupby(sorted((b[1], b[0]) for b in blocks), key=lambda p: p[0]):
        xs = [x for _, x in row]
        start = prev = xs[0]
        for x in xs[1:] + [None]:
            if x != prev + 1:
                out += [z, start, prev - start + 1]
                start = x
            prev = x
    return out


def main(out="map/towns.json"):
    recs = load_towns("data/towns")
    nations = sorted({n for r in recs.values() if (n := (r["data"].get("nation") or {}).get("name"))})
    nidx = {n: i for i, n in enumerate(nations)}
    towns = []
    for r in sorted(recs.values(), key=lambda r: r["data"]["name"].lower()):
        t = r["data"]
        st, sp = t.get("status") or {}, (t.get("coordinates") or {}).get("spawn") or {}
        flags = sum(bit for k, bit in FLAGS if st.get(k))
        towns.append([t["name"], nidx.get((t.get("nation") or {}).get("name"), -1),
                      (t.get("mayor") or {}).get("name"), (t.get("stats") or {}).get("numResidents"),
                      flags, round(sp.get("x", 0)), round(sp.get("y", 0)), round(sp.get("z", 0)),
                      r["fetched_at"], runs(t["coordinates"].get("townBlocks") or [])])
    with open(out, "w") as f:
        json.dump({"nations": nations, "towns": towns}, f, separators=(",", ":"), ensure_ascii=False)
    print(f"{out}: {len(towns)} towns, {os.path.getsize(out) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
