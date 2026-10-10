# EarthMC town database

`earthmc_towns.py` keeps a local copy of every town from the EarthMC API (`https://api.earthmc.net/v4/towns`).

## Daily rotation

`.github/workflows/update-towns.yml` runs daily (06:17 UTC, or manually from the Actions tab).
Each run refreshes `ceil(total_towns / DAYS_PER_CYCLE)` towns, walking towns in UUID order from
where the last run stopped (`data/state.json`), so every town is refreshed exactly once per cycle
with no overlap. New towns are fetched immediately; deleted towns are removed.

Default cycle is 7 days (~830 towns, ~10 requests per run). Note the API limit is per *minute*,
so a full scan (`DAYS_PER_CYCLE=1`, ~59 requests) also fits easily; a longer cycle only means
staler data, not saved quota.

## Files

- `data/towns/<uuid>.json` – `{"fetched_at", "data"}`; `data` is the unmodified API response
  (spawn, home block, every town block)
- `data/outsider_spawn_towns.csv` – towns with `status.canOutsidersSpawn = true`, with spawn
  coordinates and when each was last fetched
- `data/state.json` – rotation cursor and cycle counter
- `data/towns.db` – SQLite (`towns`, `town_blocks`), gitignored; rebuild with
  `python3 earthmc_towns.py --export-only`

## Local use

    python3 earthmc_towns.py                      # one rotation step
    python3 earthmc_towns.py --days-per-cycle 1   # full refresh

## Map

`map/index.html` is an interactive map: every town's claims coloured by nation, towns with outsider
spawn marked, search and filters, and a `/t spawn` command for each town. `make_map.py` builds
`map/towns.json` from the stored town files (the daily workflow runs it too). `map/terrain.jpg` is a
one-time stitch of the 32 most zoomed-out tiles from map.earthmc.net. To view it locally:

    cd map && python3 -m http.server   # then open http://localhost:8000
