# eta6 v2 — static data pipeline

This document describes the current eta6 static dataset: sources, identifiers,
route merging, deprecation handling, the GitHub Actions schedule, and exactly
which files are uploaded to the `data` branch.

Live ETA endpoints and the `route2` / `route3` frontend migration are **not**
part of v2.

---

## 1. What changed from v1

| v1 | v2 |
|---|---|
| Committed `state/stop_codes.json` allocator | No committed state. Codes are rebuilt from the previous round's `__stops.json` (or `stops.json`) |
| Committed `state/variant_ids.json` | Variant ids are seeded from the previous round's `routeStop.json` |
| `variants/*.json` (3,468 files) | One `routeStop.json` |
| Deprecated stops silently dropped | Code stays reserved; new `deprecated_stop.txt` report |
| Duplicate route entries kept | `merge_routes.py` merges identical routes and logs them |

The workflow no longer checks out the `data` branch. Each script reads the
previous round directly from `raw.githubusercontent.com`; if a file is missing
(first deployment) it falls back, and if nothing exists it creates a fresh
mapper.

---

## 2. Files committed to the repository

Only code and configuration:

```
build_static.py
merge_routes.py
build_operator_map.py
ctb.py
util.py
requirements.txt
overrides/operator_stops.json
.github/workflows/onload.yml
.github/workflows/static.yml
.gitignore
eta6-v2.md
```

`static/` and `.cache/` are build outputs and are git-ignored. There is no
`state/` directory.

---

## 3. Files published to the `data` branch

`static.yml` passes these nine paths to
`test-room-7/action-update-file@v1`; they land on `data` with the same relative
path:

```
static/manifest.json
static/index.json
static/routeStop.json
static/stops.json
static/stop_routes.json
static/nearby.json
static/operator_map.json
static/__stops.json
static/deprecated_stop.txt
```

`onload.yml` continues to publish the CTB snapshot weekly:

```
_CTB_Stop.json
_CTB_rt.json
_CTB_Stop_Route.json
```

Raw URLs:

```
https://raw.githubusercontent.com/336699c/eta6/refs/heads/data/static/manifest.json
https://raw.githubusercontent.com/336699c/eta6/refs/heads/data/static/__stops.json
https://raw.githubusercontent.com/336699c/eta6/refs/heads/data/static/routeStop.json
```

---

## 4. Schedules

| Workflow | Cron (UTC) | Local time (HKT) | Purpose |
|---|---|---|---|
| `onload.yml` | `30 13 * * 6` | Saturday 21:30 | CTB `_CTB_*.json` snapshot |
| `static.yml` | `0 18 * * 6,2` | Sunday & Wednesday 02:00 | static dataset + operator map |

Both share the concurrency group `eta6-data`, so there is never more than one
writer on the `data` branch. Both also support `workflow_dispatch`.

---

## 5. Stop identity and the stop-code mapper

### 5.1 Rules

* One TD `stop_id` is one canonical stop. TD already merges jointly operated
  poles and carries per-operator names.
* Public codes use a 34-symbol alphabet (`A–Z` without `I,O`, plus `0–9`):
  3 characters for franchised bus (39,304 capacity), 4 for GMB.
* Codes are **never reused**. If a stop disappears, its code stays reserved in
  `__stops.json`; if the stop returns it gets the same code.
* Stops are never merged by distance; proximity is only expressed in
  `nearby.json`.

### 5.2 How a round finds its codes

`build_static.py` resolves the mapper in this order:

1. `--previous-dir` (local testing) if it contains `__stops.json`;
2. `static/__stops.json` from the `data` branch via raw GitHub;
3. otherwise, the previous `static/stops.json` (each record carries `td`, so the
   mapper is rebuilt from the published stops);
4. otherwise, a fresh mapper with codes allocated from index 0.

A new stop is assigned the next free index for its mode; existing codes are
never touched. The resulting map is published as `static/__stops.json`
(td stop id → code, ~145 KB for 9,205 stops).

### 5.3 Deprecated stops

A code is deprecated when it existed in the previous round's `stops.json` but
no longer appears in the current build.

* the code stays in `__stops.json` forever (reserved);
* the stop is written to `deprecated_stop.txt`, merged with earlier findings so
  the report is cumulative:

```
# eta6 deprecated stops -- codes stay reserved in __stops.json
# code | td stop id | name_en | name_zh | operator stop ids
ZZZ | 9999999 | TEST DEPOT | 測試車廠 | KMB:18492910339410B1,CTB:002403
```

Operator ids come from the previous round's `operator_map.json`, so the report
shows the company-native ids that used to serve the stop.

---

## 6. Routes and variants

* Patterns are built by grouping a TD `route_id`'s trips by ordered stop tuple.
* Direction (`bound`) comes from the `A - B` route long name. Unresolvable
  variants are emitted with `"directionGuessed": true` (98 today).
* Variant ids are seeded from the previous `routeStop.json`
  (`stableKey → id`), so a rebuild keeps existing public ids. New variants get
  the deterministic preferred id (`KMB_101_O1`, `GMB_<tdRouteId>_I1`, …).

### 6.1 Merging identical routes (`merge_routes.py`)

Two variants are merged **only when both** conditions hold:

1. the route number is exactly equal — different route numbers never merge,
   even with identical stops;
2. the ordered stop sequence is exactly equal — the same route number alone is
   not enough, because one number can cover several services.

The direction letter is ignored in the comparison: the same physical trip may
be `I` by KMB and `O` by CTB. The canonical variant is the one with the most
operators, then the lowest id. It keeps its own timetable and fares, while:

* `co` becomes the union of operators;
* `mergedFrom` lists the absorbed variant ids;
* `bounds` records per-operator labels when they disagree.

Every merge is printed, e.g.:

```
merged GMB_2010277_I1 <- GMB_2010278_O1 (route 805S, 15 stops)
  canonical timetable/fare kept; differs: GMB_2010278_O1 timetable
merged 1 groups, removed 1 duplicate variants (3467 left)
```

The script rewrites `routeStop.json`, `index.json`, `stop_routes.json` and the
manifest counts/hashes. It is idempotent.

---

## 7. Published file formats

### 7.1 `manifest.json`

```jsonc
{
  "version": "v2026-11-10",
  "builtAt": "2026-11-14T09:14:13+00:00",
  "source": { "dataset": "TD ... (GTFS)", "feed": "...", "validFrom": "2026-11-10" },
  "counts": { "routes": 2364, "variants": 3467, "stops": 9205,
              "busStops": 4433, "gmbStops": 4772, "nearbyPairs": 47760 },
  "files": { "index.json": { "bytes": 3009574, "sha256": "..." }, "...": {} },
  "routeStop": { "path": "routeStop.json", "count": 3467 },
  "build": { "durationSeconds": 11.1,
             "fareKinds": { "flat": 1015, "ladder": 55, "matrix": 2402 },
             "unresolvedDirections": 98, "newStopCodes": 0, "reservedCodes": 0 },
  "mergedRoutes": { "groups": 1, "variants": 1 },
  "deprecatedStops": { "path": "deprecated_stop.txt", "count": 0 },
  "operatorMap": {
    "path": "operator_map.json",
    "params": { "radiusM": 80, "autoAcceptM": 15, "nameMin": 0.34, "nameMinFar": 0.5 },
    "operators": { "KMB": { "count": 6757, "matched": 6605, "ratio": 0.978,
                            "within": { "15": 3332, "30": 5278, "60": 6460, "80": 6605 },
                            "methods": { "coord": 21, "coord+name": 6584 } }, "...": {} },
    "unmatchedCounts": { "KMB": 152, "CTB": 15, "NLB": 9, "MTR": 11 },
    "issues": {}
  },
  "warnings": []
}
```

### 7.2 `index.json`

```jsonc
{
  "version": "v2026-11-10",
  "routes": {
    "1000001": { "id": "KMB_43S", "co": ["KMB"], "route": "43S",
                 "orig": {}, "dest": {}, "variants": ["KMB_43S_O1", "KMB_43S_I1"] }
  },
  "variants": {
    "CTB_10_O1": { "co": ["CTB"], "route": "10", "bound": "O",
                   "orig": {}, "dest": {}, "circular": false,
                   "stops": 36, "services": ["287"], "freq": { "287": [...] } }
  },
  "services": { "287": { "days": ["MO", "..."], "start": "20200101", "end": "20991231" } },
  "holidays": ["20260101", "..."]
}
```

### 7.3 `routeStop.json`

Every variant keyed by variant id (one file instead of thousands).

```jsonc
{
  "CTB_10_O1": {
    "id": "CTB_10_O1",
    "co": ["CTB"],
    "route": "10",
    "bound": "O",
    "tdRouteId": "1000295",
    "stableKey": "CTB|10|O|002403|...",
    "orig": { "zh": "堅尼地城", "en": "KENNEDY TOWN" },
    "dest": { "zh": "北角碼頭(不經遮打道)", "en": "NORTH POINT FERRY PIER (...)" },
    "circular": false,
    "stops": ["AAA", "AAB", "..."],
    "freq": { "448": [["05:30", "08:00", 25]] },
    "fixedDepartures": { "448": ["06:00"] },
    "fares": { "currency": "HKD", "kind": "flat", "flat": 4.8 },
    "hasHolidayFare": false,
    "sources": { "static": "TD_GTFS", "validFrom": "2026-11-10" }
  }
}
```

Merged variants additionally carry `mergedFrom` and, when labels differ,
`bounds`.

Fare kinds:

| `kind` | Shape | Meaning |
|---|---|---|
| `flat` | `{"flat": 6.7}` | same price for every alighting stop |
| `ladder` | `{"prices": [...], "ladder": [[a,b,p], ...]}` | price depends on the alighting stop only |
| `matrix` | `{"prices": [...], "rows": [[[a,b,p], ...], ...]}` | `rows[i]` = fares from stop `i`; `-1` = unknown |

A run `[a,b,p]` means "alight at any stop index in `a..b` → `prices[p]`".

### 7.4 `stops.json`

```jsonc
{
  "A00": {
    "td": "1209",
    "mode": "bus",
    "en": "SAM SHING ESTATE, CASTLE PEAK ROAD",
    "zh": "三聖邨, 青山公路",
    "loc": [22.38113, 113.97909],
    "ops": { "KMB": "SAM SHING ESTATE", "CTB": "SAM SHING ESTATE, ..." }
  }
}
```

### 7.5 `__stops.json`

```jsonc
{ "1209": "A00", "1210": "A01", "20000123": "A000" }
```

The reserved mapper: TD stop id → code. Contains active and deprecated stops.
This is the only input that preserves codes between rounds.

### 7.6 `stop_routes.json`, `nearby.json`, `operator_map.json`

```jsonc
// stop_routes.json
{ "A00": ["KMB_61M_O1", "CTB_10_O1"] }

// nearby.json  [code, metres, walkingSeconds, flags]
{ "A00": [["A01", 18, 14, 3]] }

// operator_map.json
{ "CTB": { "count": 2594, "matched": 2579, "ratio": 0.994,
           "stops": { "002403": { "td": "121", "code": "A01", "d": 4.2,
                                  "c": 0.997, "m": "coord+name" } } },
  "unmatched": { "CTB": [ ... ] },
  "params": { "radiusM": 80, "autoAcceptM": 15, "nameMin": 0.34, "nameMinFar": 0.5 } }
```

`nearby` flags: 1 same normalised name, 2 opposite pole with a shared route,
4 different mode.

---

## 8. Methodology

### 8.1 Sources

| Data | Source |
|---|---|
| Stops, routes, trips, stop times, fares, frequencies, calendars | TD GTFS `https://static.data.gov.hk/td/pt-headway-en/` (+ `-tc`) |
| KMB + LWB stops | `https://data.etabus.gov.hk/v1/transport/kmb/stop/` (trailing slash required) |
| CTB stops | eta6 `data` branch `_CTB_Stop.json` |
| NLB stops | `route.php?action=list`, then `stop.php?action=list&routeId=` per route |
| MTR Bus / LRT feeder stops | `https://opendata.mtr.com.hk/data/mtr_bus_stops.csv` |

Agencies included: `KMB`, `LWB`, `CTB`, `NLB`, `LRTFeeder`, `GMB`,
`KMB+CTB`, `LWB+CTB`. Excluded: `PI`, `DB`, `XB`, `FERRY`, `PTRAM`, `TRAM`.

### 8.2 Fares

TD `fare_rules` is an origin→destination matrix. It is compressed into
run-length rows (`matrix`), collapsed to a `ladder` when only the alighting stop
matters, or to `flat` when constant. Unknown cells are omitted; `-1` marks an
explicit unknown price.

### 8.3 Frequency

`frequencies.txt` bands become `service_id → [["HH:MM","HH:MM",headwayMinutes]]`.
Trips without headway rows are listed in `fixedDepartures`. Services and
holiday exceptions are published in `index.json`.

### 8.4 Nearby stops

150 m radius, nearest 10 per stop, computed on a ~200 m grid. Informational
only; canonical stops are never merged by distance.

### 8.5 Operator stop matching

1. Index every bus stop on a ~200 m grid.
2. Collect TD candidates within 80 m.
3. Name score = token overlap against the operator's own TD name tag.
4. Score = `0.65 × name + 0.35 × (1 − distance / 80)`.
5. Accept at ≤15 m, or name ≥0.34 within 60 m, or name ≥0.5 between 60–80 m.
6. `overrides/operator_stops.json` is applied first (`"m": "override"`).

Many operator stops may map to one TD code: a TD stop is a location group,
while operator APIs expose one id per pole.

Current match rates: KMB 97.8 % (6,605/6,757), CTB 99.4 % (2,579/2,594),
NLB 97.3 % (323/332), MTR 98.4 % (664/675).

---

## 9. Operating the pipeline

### 9.1 Automatic

Nothing to do. `static.yml` runs Sunday and Wednesday at 02:00 HKT:

```
build_static.py  →  merge_routes.py  →  build_operator_map.py  →  upload
```

`onload.yml` refreshes the CTB snapshot every Saturday.

### 9.2 First deployment

1. Commit the files listed in §2 to the default branch.
2. Ensure the `data` branch exists (it holds `_CTB_*.json`).
3. Run **Build static data** manually once.

On the first run `static/__stops.json` does not exist, so codes are rebuilt
from the published `static/stops.json`; variant ids are seeded from the
published `static/routeStop.json`. Existing public codes and ids therefore do
not change.

### 9.3 Local testing

```bash
python build_static.py --out static --previous-dir /path/to/previous/static
python merge_routes.py --static static
python build_operator_map.py --static static --previous-dir /path/to/previous/static
```

`--offline` ignores the remote round entirely and starts from a fresh mapper.

### 9.4 Fixing a bad operator match

Add an entry to `overrides/operator_stops.json`, commit, run the workflow:

```json
{ "KMB": { "18492910339410B1": "1209" } }
```

### 9.5 Changing the data

* Edit scripts on the default branch; the next scheduled run publishes the new
  format.
* Never delete or rewrite `static/__stops.json` entries — that is what keeps
  codes stable and reserved.
* `build_static.py` compares counts with the previous `manifest.json` and adds a
  `largeDeltas` warning when a count moves by more than 10 % (minimum 5).

---

## 10. Known limitations (v2)

* 98 variants carry `directionGuessed` (mostly GMB routes without a resolvable
  `A - B` long name).
* TD coverage of racecourse/special routes is incomplete; they are absent
  rather than patched in.
* Route merging keeps the canonical timetable and fares; when two merged
  variants disagree, the difference is logged but the other values are not
  published.
* No holiday fares (`hasHolidayFare` is always `false`).
* No virtual stops for unmatched operator stops.
* No live ETA endpoints and no frontend migration to `routeStop.json` yet.
