# EarthMC town database

`python3 earthmc_towns.py` pulls every town from the EarthMC API (`https://api.earthmc.net/v4/towns`),
100 towns per request, 1 request/sec, with backoff on 429/5xx.

- `data/towns_raw.json` – full unmodified API response for every town (spawn, home block, all town blocks)
- `data/outsider_spawn_towns.csv` – towns with `status.canOutsidersSpawn = true`, with spawn coordinates
- `data/towns.db` – SQLite (`towns`, `town_blocks`); not committed (75 MB). Rebuild offline with
  `python3 earthmc_towns.py --from-raw`.
